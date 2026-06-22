"""
FastAPI-приложение: веб-версия HH-парсера.
"""

import asyncio
import queue
import tempfile
import uuid
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Optional

from fastapi import FastAPI, Request, HTTPException, Depends
from fastapi.responses import HTMLResponse, StreamingResponse, JSONResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from pydantic import BaseModel

import sys
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from hh_companies_by_industry import (
    __version__,
    Industry,
    Company,
    fetch_html,
    parse_industries,
    scrape_companies,
    scrape_companies_parallel,
    CATALOG_URL,
    UA,
    HHBlockedError,
    _make_session,
)

import requests as req_lib
import csv
import io
import json
import logging

from web.auth import (
    hash_password,
    verify_password,
    create_token,
    decode_token,
    get_current_user,
    require_auth,
    require_admin,
    is_public_path,
    COOKIE_NAME,
)
from web.db import (
    init_db,
    get_user_by_login,
    get_user_by_id,
    list_users,
    create_user,
    delete_user,
    list_presets,
    create_preset,
    delete_preset,
)

logger = logging.getLogger(__name__)

@asynccontextmanager
async def lifespan(app):
    await asyncio.to_thread(init_db)
    yield

app = FastAPI(title="HH-Parser Web", version=__version__, lifespan=lifespan)

BASE_DIR = Path(__file__).resolve().parent
app.mount("/static", StaticFiles(directory=BASE_DIR / "static"), name="static")
templates = Jinja2Templates(directory=BASE_DIR / "templates")

# Кэш индустрий (загружается один раз)
_industries_cache: list[Industry] = []

# Кэш последнего результата парсинга — чтобы «Скачать CSV» не парсил заново
_last_scrape_result: Optional[dict] = None

# Кэш последнего результата гео-парсинга — чтобы «Скачать Excel» не парсил заново
_last_geo_result: Optional[dict] = None

# Кэши последних результатов рыночной статистики (индекс / зарплаты) — для «Скачать Excel»
_last_index_result: Optional[dict] = None
_last_salary_result: Optional[dict] = None

# Путь к дисковому кэшу датасета stats.hh.ru (общий с CLI-модулями)
STATS_CACHE_PATH = str(Path(__file__).resolve().parent.parent / "data" / "stats_hh_cache.json")


# ---------- Auth middleware ----------

@app.middleware("http")
async def auth_middleware(request: Request, call_next):
    """
    Проверяет JWT cookie для всех не-публичных путей.
    HTML-запросы → редирект на /login.
    API-запросы → 401 JSON.
    """
    path = request.url.path

    if is_public_path(path):
        return await call_next(request)

    token = request.cookies.get(COOKIE_NAME)
    user = decode_token(token) if token else None

    if not user:
        accept = request.headers.get("accept", "")
        if "text/html" in accept:
            return RedirectResponse(url="/login", status_code=302)
        return JSONResponse(status_code=401, content={"detail": "Не авторизован"})

    return await call_next(request)


def _load_industries() -> list[Industry]:
    global _industries_cache
    if _industries_cache:
        return _industries_cache
    session = req_lib.Session()
    session.headers.update({"User-Agent": UA, "Accept-Language": "ru-RU,ru;q=0.9,en;q=0.8"})
    html = fetch_html(CATALOG_URL, session)
    _industries_cache = parse_industries(html)
    return _industries_cache


# ---------- Модели ----------

class ScrapeRequest(BaseModel):
    industry_slugs: list[str]  # список slug_url выбранных индустрий
    min_vacancies: int = 10
    area: int = 113
    only_open: bool = True


class LoginRequest(BaseModel):
    login: str
    password: str


class CreateUserRequest(BaseModel):
    login: str
    password: str
    display_name: str = ""
    role: str = "user"


class CreatePresetRequest(BaseModel):
    name: str
    tab: str = "geo"
    config: dict = {}


class GeoScrapeRequest(BaseModel):
    city_names: list[str] = []         # названия городов
    role_ids: list[int] = []           # ID профессиональных ролей HH.ru
    search_texts: list = []            # строки или {"text": str, "exact": bool}


class MarketRequest(BaseModel):
    city_names: list[str] = []         # названия городов (пусто → дефолтный набор модуля)
    prof_area_ids: list = []           # id профобластей + опц. "all" (пусто → все)


# ---------- Страницы ----------

@app.get("/", response_class=HTMLResponse)
async def index(request: Request):
    return templates.TemplateResponse("index.html", {"request": request, "version": __version__})


@app.get("/login", response_class=HTMLResponse)
async def login_page(request: Request):
    """Страница входа."""
    return templates.TemplateResponse("login.html", {"request": request})


# ---------- Health ----------

@app.get("/health")
async def health():
    return {"status": "ok", "version": __version__}


# ---------- Auth endpoints ----------

@app.post("/api/auth/login")
async def auth_login(data: LoginRequest):
    """Аутентификация. Возвращает JWT в httpOnly cookie."""
    user = await asyncio.to_thread(get_user_by_login, data.login)
    if not user or not verify_password(data.password, user["password_hash"]):
        raise HTTPException(status_code=401, detail="Неверный логин или пароль")

    token = create_token(user["id"], user["login"], user["role"])
    response = JSONResponse(content={
        "ok": True,
        "user": {
            "id": user["id"],
            "login": user["login"],
            "display_name": user["display_name"],
            "role": user["role"],
        },
    })
    response.set_cookie(
        key=COOKIE_NAME,
        value=token,
        httponly=True,
        max_age=30 * 24 * 3600,
        samesite="lax",
    )
    return response


@app.post("/api/auth/logout")
async def auth_logout():
    """Выход: удалить JWT cookie."""
    response = JSONResponse(content={"ok": True})
    response.delete_cookie(key=COOKIE_NAME)
    return response


@app.get("/api/auth/me")
async def auth_me(request: Request):
    """Текущий пользователь из JWT."""
    user = get_current_user(request)
    if not user:
        raise HTTPException(status_code=401, detail="Не авторизован")
    db_user = await asyncio.to_thread(get_user_by_id, int(user["sub"]))
    if not db_user:
        raise HTTPException(status_code=401, detail="Пользователь не найден")
    return {
        "id": db_user["id"],
        "login": db_user["login"],
        "display_name": db_user["display_name"],
        "role": db_user["role"],
    }


# ---------- Admin endpoints ----------

@app.get("/api/admin/users")
async def admin_list_users(request: Request):
    """Список всех пользователей (только для админа)."""
    require_admin(request)
    users = await asyncio.to_thread(list_users)
    return {"ok": True, "users": users}


@app.post("/api/admin/users", status_code=201)
async def admin_create_user(data: CreateUserRequest, request: Request):
    """Создать пользователя (только для админа)."""
    require_admin(request)
    existing = await asyncio.to_thread(get_user_by_login, data.login)
    if existing:
        raise HTTPException(status_code=409, detail="Логин уже занят")
    pw_hash = hash_password(data.password)
    user_id = await asyncio.to_thread(
        create_user, data.login, pw_hash, data.display_name, data.role
    )
    return {"ok": True, "id": user_id}


@app.delete("/api/admin/users/{user_id}")
async def admin_delete_user(user_id: int, request: Request):
    """Удалить пользователя (только для админа)."""
    current = require_admin(request)
    if str(user_id) == current.get("sub"):
        raise HTTPException(status_code=400, detail="Нельзя удалить самого себя")
    await asyncio.to_thread(delete_user, user_id)
    return {"ok": True}


# ---------- Preset endpoints ----------

@app.get("/api/presets")
async def get_presets(request: Request, tab: Optional[str] = None):
    """Список пресетов текущего пользователя."""
    user = require_auth(request)
    user_id = int(user["sub"])
    presets = await asyncio.to_thread(list_presets, user_id, tab)
    return {"ok": True, "presets": presets}


@app.post("/api/presets", status_code=201)
async def add_preset(data: CreatePresetRequest, request: Request):
    """Создать пресет."""
    user = require_auth(request)
    user_id = int(user["sub"])
    preset_id = await asyncio.to_thread(
        create_preset, user_id, data.name, data.tab, data.config
    )
    return {"ok": True, "id": preset_id}


@app.delete("/api/presets/{preset_id}")
async def remove_preset(preset_id: int, request: Request):
    """Удалить пресет (только свой)."""
    user = require_auth(request)
    user_id = int(user["sub"])
    deleted = await asyncio.to_thread(delete_preset, preset_id, user_id)
    if not deleted:
        raise HTTPException(status_code=404, detail="Пресет не найден")
    return {"ok": True}


# ---------- Geo endpoints ----------

@app.get("/api/geo/cities")
async def geo_cities(q: Optional[str] = None):
    """
    Возвращает дерево регионов/городов HH.
    Если передан параметр q — фильтрует по подстроке (case-insensitive).
    """
    try:
        from hh_vacancies_by_geo import load_area_tree
        tree = await asyncio.to_thread(load_area_tree)
        # tree = {name.lower(): (area_id, canonical_name), ...}
        cities = [{"id": v[0], "name": v[1]} for v in tree.values()]
        cities.sort(key=lambda c: c["name"])
        if q:
            q_lower = q.lower()
            cities = [c for c in cities if q_lower in c["name"].lower()]
        return {"ok": True, "cities": cities}
    except Exception as e:
        logger.exception("Ошибка загрузки дерева регионов")
        return {"ok": False, "error": str(e)}


@app.get("/api/geo/roles")
async def geo_roles(q: Optional[str] = None):
    """
    Возвращает справочник профессиональных ролей HH.
    Если передан параметр q — фильтрует по подстроке (case-insensitive).
    """
    try:
        from hh_vacancies_by_geo import load_professional_roles
        roles = await asyncio.to_thread(load_professional_roles)
        if q:
            q_lower = q.lower()
            roles = [r for r in roles if q_lower in r.get("name", "").lower()]
        return {"ok": True, "roles": roles}
    except Exception as e:
        logger.exception("Ошибка загрузки ролей")
        return {"ok": False, "error": str(e)}


@app.post("/api/geo/scrape/stream")
async def geo_scrape_stream(req: GeoScrapeRequest, request: Request):
    """
    SSE-эндпоинт: парсит вакансии по городам/ролям с прогрессом в реальном времени.
    Отправляет события:
      - type=progress  — прогресс (город, страница, найдено вакансий)
      - type=result    — финальный результат (список вакансий)
      - type=error     — ошибка
    """
    require_auth(request)

    q: queue.Queue = queue.Queue()

    def worker():
        try:
            from hh_vacancies_by_geo import resolve_city_ids, scrape_vacancies_geo

            q.put({"type": "progress", "status_text": "Определяю area_id городов...", "done": 0, "total": 0})

            city_ids = resolve_city_ids(req.city_names)

            def on_progress(p: dict):
                q.put({"type": "progress", **p})

            result = scrape_vacancies_geo(
                city_ids=city_ids,
                role_ids=req.role_ids,
                search_texts=req.search_texts,
                progress_cb=on_progress,
            )
            global _last_geo_result
            _last_geo_result = result
            q.put({
                "type": "result",
                "roles": result.get("roles", []),
                "texts": result.get("texts", []),
            })
        except Exception as e:
            logger.exception("Ошибка geo-парсинга")
            q.put({"type": "error", "error": str(e)})
        finally:
            q.put(None)

    async def event_generator():
        asyncio.get_running_loop().run_in_executor(None, worker)

        while True:
            try:
                event = await asyncio.to_thread(q.get, timeout=300)
            except Exception:
                break

            if event is None:
                break

            yield f"data: {json.dumps(event, ensure_ascii=False)}\n\n"

    return StreamingResponse(event_generator(), media_type="text/event-stream")


@app.post("/api/geo/scrape/xlsx")
async def geo_scrape_xlsx(req: GeoScrapeRequest, request: Request):
    """
    Запускает парсинг вакансий по городам/ролям и отдаёт результат как XLSX-файл.
    """
    require_auth(request)

    try:
        from hh_vacancies_by_geo import export_to_xlsx
    except ImportError as e:
        raise HTTPException(status_code=500, detail=f"Модуль hh_vacancies_by_geo не найден: {e}")

    # Используем кэш последнего парсинга (не парсим заново)
    if _last_geo_result and _last_geo_result.get("roles") is not None:
        result = _last_geo_result
    else:
        raise HTTPException(status_code=400, detail="Сначала запустите парсинг через «Собрать данные»")

    with tempfile.NamedTemporaryFile(suffix=".xlsx", delete=False) as tmp:
        tmp_path = tmp.name

    try:
        await asyncio.to_thread(export_to_xlsx, result, tmp_path)
        xlsx_bytes = Path(tmp_path).read_bytes()
    finally:
        Path(tmp_path).unlink(missing_ok=True)

    return StreamingResponse(
        iter([xlsx_bytes]),
        media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        headers={"Content-Disposition": "attachment; filename=hh_vacancies.xlsx"},
    )


# ---------- Market stats endpoints (hh.индекс + зарплаты, источник stats.hh.ru) ----------

@app.get("/api/market/prof-areas")
async def market_prof_areas():
    """Справочник профобластей HH (для селектора столбцов индекса/зарплат)."""
    try:
        import hh_market_stats as stats
        areas = await asyncio.to_thread(stats.load_prof_areas, include_all=True)
        return {"ok": True, "prof_areas": areas}
    except Exception as e:
        logger.exception("Ошибка загрузки профобластей")
        return {"ok": False, "error": str(e)}


def _market_stream(req: MarketRequest, build_fn, kind: str) -> StreamingResponse:
    """
    Общий SSE-генератор для индекса/зарплат: гоняет синхронную build_fn в потоке,
    шлёт progress/result/error. build_fn(cities, prof_area_ids, *, cache_path,
    progress_cb) -> dict. Результат кэшируется в _last_index/salary_result.
    """
    q: queue.Queue = queue.Queue()

    cities = req.city_names or None          # пусто → дефолтный набор модуля
    prof_area_ids = req.prof_area_ids or None  # пусто → все профобласти

    def worker():
        try:
            q.put({"type": "progress", "status_text": "Загружаю статистику stats.hh.ru...",
                   "done": 0, "total": 0})

            def on_progress(p: dict):
                q.put({"type": "progress", **p})

            result = build_fn(
                cities, prof_area_ids,
                cache_path=STATS_CACHE_PATH,
                progress_cb=on_progress,
            )

            global _last_index_result, _last_salary_result
            if kind == "index":
                _last_index_result = result
            else:
                _last_salary_result = result

            q.put({
                "type": "result",
                "kind": kind,
                "as_of": result.get("as_of"),
                "cities": result.get("cities", []),
                "prof_areas": result.get("prof_areas", []),
                "rows": result.get("rows", []),
                "region_fallback": result.get("region_fallback", []),
                "missing_cities": result.get("missing_cities", []),
                "missing_in_stats": result.get("missing_in_stats", []),
            })
        except Exception as e:
            logger.exception("Ошибка market-парсинга (%s)", kind)
            q.put({"type": "error", "error": str(e)})
        finally:
            q.put(None)

    async def event_generator():
        asyncio.get_running_loop().run_in_executor(None, worker)
        while True:
            try:
                event = await asyncio.to_thread(q.get, timeout=300)
            except Exception:
                break
            if event is None:
                break
            yield f"data: {json.dumps(event, ensure_ascii=False)}\n\n"

    return StreamingResponse(event_generator(), media_type="text/event-stream")


async def _market_xlsx(cached: Optional[dict], export_fn, filename: str) -> StreamingResponse:
    """Отдаёт закэшированный результат как XLSX через export_fn(result, path)."""
    if not cached or cached.get("rows") is None:
        raise HTTPException(status_code=400, detail="Сначала запустите сбор данных")

    with tempfile.NamedTemporaryFile(suffix=".xlsx", delete=False) as tmp:
        tmp_path = tmp.name
    try:
        await asyncio.to_thread(export_fn, cached, tmp_path)
        xlsx_bytes = Path(tmp_path).read_bytes()
    finally:
        Path(tmp_path).unlink(missing_ok=True)

    return StreamingResponse(
        iter([xlsx_bytes]),
        media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        headers={"Content-Disposition": f"attachment; filename={filename}"},
    )


@app.post("/api/market/index/stream")
async def market_index_stream(req: MarketRequest, request: Request):
    """SSE: hh.индекс «город × профобласть» из stats.hh.ru."""
    require_auth(request)
    from hh_market_index import build_index_table
    return _market_stream(req, build_index_table, "index")


@app.post("/api/market/index/xlsx")
async def market_index_xlsx(request: Request):
    """XLSX последнего результата индекса."""
    require_auth(request)
    from hh_market_index import export_index_to_xlsx
    return await _market_xlsx(_last_index_result, export_index_to_xlsx, "hh_index.xlsx")


@app.post("/api/market/salary/stream")
async def market_salary_stream(req: MarketRequest, request: Request):
    """SSE: средние зарплаты (предлагаемая + ожидаемая) «город × профобласть»."""
    require_auth(request)
    from hh_market_salary import build_salary_table
    return _market_stream(req, build_salary_table, "salary")


@app.post("/api/market/salary/xlsx")
async def market_salary_xlsx(request: Request):
    """XLSX последнего результата зарплат (2 листа: предлагаемая / ожидаемая)."""
    require_auth(request)
    from hh_market_salary import export_salary_to_xlsx
    return await _market_xlsx(_last_salary_result, export_salary_to_xlsx, "hh_salary.xlsx")


# ---------- Существующие роуты (companies by industry) ----------

@app.get("/api/industries")
async def get_industries():
    """Возвращает список индустрий."""
    try:
        industries = await asyncio.to_thread(_load_industries)
        return {"ok": True, "industries": [{"name": i.name, "slug_url": i.slug_url} for i in industries]}
    except HHBlockedError as e:
        return {"ok": False, "error": str(e)}
    except Exception as e:
        logger.exception("Ошибка загрузки индустрий")
        return {"ok": False, "error": f"Ошибка загрузки индустрий: {e}"}


def _build_result_dict(companies: list[Company]) -> dict:
    """Формирует словарь результата из списка компаний."""
    return {
        "ok": True,
        "companies_count": len(companies),
        "companies": [
            {
                "name": c.name,
                "url": c.url,
                "vacancies": c.vacancies,
                "industry_name": c.industry_name,
            }
            for c in companies
        ],
    }


def _save_to_cache(result: dict) -> None:
    """Сохраняет результат парсинга в кэш."""
    global _last_scrape_result
    _last_scrape_result = result


@app.post("/api/scrape")
async def scrape(req: ScrapeRequest, request: Request):
    """Запускает парсинг и возвращает результат JSON (без прогресса).
    Использует параллельный парсинг индустрий для ускорения."""
    require_auth(request)
    try:
        industries = await asyncio.to_thread(_load_industries)
    except Exception as e:
        return {"ok": False, "error": f"Не удалось загрузить индустрии: {e}"}

    slug_set = set(req.industry_slugs)
    chosen = [i for i in industries if i.slug_url in slug_set]

    if not chosen:
        return {"ok": False, "error": "Не выбрано ни одной индустрии."}

    try:
        all_companies = await asyncio.to_thread(
            scrape_companies_parallel,
            chosen,
            req.min_vacancies,
            req.area,
            req.only_open,
        )
    except HHBlockedError as e:
        return {"ok": False, "error": str(e)}
    except Exception as e:
        logger.exception("Ошибка парсинга")
        return {"ok": False, "error": f"Ошибка парсинга: {e}"}

    result = _build_result_dict(all_companies)
    _save_to_cache(result)
    return result


@app.post("/api/scrape/stream")
async def scrape_stream(req: ScrapeRequest, request: Request):
    """
    SSE-эндпоинт: парсит с прогрессом в реальном времени.
    Несколько индустрий парсятся параллельно в разных потоках (каждый со своей сессией).
    Отправляет события:
      - type=progress  — прогресс по страницам/индустриям
      - type=result    — финальный результат
      - type=error     — ошибка
    """
    require_auth(request)
    try:
        industries = await asyncio.to_thread(_load_industries)
    except Exception as e:
        async def error_gen():
            yield f"data: {json.dumps({'type': 'error', 'error': str(e)}, ensure_ascii=False)}\n\n"
        return StreamingResponse(error_gen(), media_type="text/event-stream")

    slug_set = set(req.industry_slugs)
    chosen = [i for i in industries if i.slug_url in slug_set]

    if not chosen:
        async def error_gen():
            yield f"data: {json.dumps({'type': 'error', 'error': 'Не выбрано ни одной индустрии.'}, ensure_ascii=False)}\n\n"
        return StreamingResponse(error_gen(), media_type="text/event-stream")

    # Очередь для передачи событий из синхронных потоков в async-генератор
    q: queue.Queue = queue.Queue()

    def worker():
        """
        Запускает параллельный парсинг индустрий.
        Каждая индустрия парсится в своём потоке со своей сессией.
        Прогресс со всех потоков идёт в общую очередь.
        """
        import threading
        from concurrent.futures import ThreadPoolExecutor, as_completed

        total_industries = len(chosen)
        all_companies: dict[str, Company] = {}  # url -> Company (дедуп)
        lock = threading.Lock()

        # Счётчик завершённых индустрий для отслеживания общего прогресса
        completed_count = [0]

        def scrape_one(idx: int, ind: Industry) -> list[Company]:
            """Парсит одну индустрию в отдельном потоке."""
            sess = _make_session()

            q.put({
                "type": "progress",
                "industry_idx": idx,
                "industries_total": total_industries,
                "industry_name": ind.name,
                "page": 0,
                "pages_total": 0,
                "companies_found": 0,
                "companies_added": 0,
                "status": f"Индустрия {idx}/{total_industries}: {ind.name} — начало",
            })

            def on_progress(p):
                q.put({
                    "type": "progress",
                    "industry_idx": idx,
                    "industries_total": total_industries,
                    "industry_name": p.get("industry_name", ind.name),
                    "page": p.get("page", 0),
                    "pages_total": p.get("pages_total", 0),
                    "companies_found": p.get("companies_found_total", 0),
                    "companies_added": p.get("companies_added_total", 0),
                    "status": (
                        f"Индустрия {idx}/{total_industries}: {ind.name} | "
                        f"Страница {p.get('page', '?')}/{p.get('pages_total', '?')} | "
                        f"Найдено: {p.get('companies_found_total', 0)} | "
                        f"В итог: {p.get('companies_added_total', 0)}"
                    ),
                })

            companies = scrape_companies(
                ind,
                min_vacancies=req.min_vacancies,
                area=req.area,
                only_with_open_vacancies=req.only_open,
                progress_cb=on_progress,
                session=sess,
            )

            with lock:
                completed_count[0] += 1
                q.put({
                    "type": "progress",
                    "industry_idx": idx,
                    "industries_total": total_industries,
                    "industry_name": ind.name,
                    "page": 0,
                    "pages_total": 0,
                    "companies_found": len(companies),
                    "companies_added": len(companies),
                    "status": (
                        f"Индустрия {idx}/{total_industries}: {ind.name} — готово "
                        f"({completed_count[0]}/{total_industries} завершено)"
                    ),
                })

            return companies

        try:
            # Параллельный парсинг: до 4 индустрий одновременно
            max_workers = min(4, total_industries)
            with ThreadPoolExecutor(max_workers=max_workers) as executor:
                futures = {
                    executor.submit(scrape_one, idx, ind): ind
                    for idx, ind in enumerate(chosen, start=1)
                }
                for future in as_completed(futures):
                    companies = future.result()
                    with lock:
                        for c in companies:
                            all_companies[c.url] = c

            # Финальный результат
            result_companies = list(all_companies.values())
            result = _build_result_dict(result_companies)
            _save_to_cache(result)
            q.put({
                "type": "result",
                "companies_count": result["companies_count"],
                "companies": result["companies"],
            })
        except HHBlockedError as e:
            q.put({"type": "error", "error": str(e)})
        except Exception as e:
            logger.exception("Ошибка парсинга")
            q.put({"type": "error", "error": f"Ошибка парсинга: {e}"})
        finally:
            q.put(None)  # сигнал завершения

    async def event_generator():
        # Запускаем воркер в отдельном потоке
        asyncio.get_running_loop().run_in_executor(None, worker)

        while True:
            # Ждём событие из очереди без блокировки event loop
            try:
                event = await asyncio.to_thread(q.get, timeout=120)
            except Exception:
                break

            if event is None:
                break

            yield f"data: {json.dumps(event, ensure_ascii=False)}\n\n"

    return StreamingResponse(event_generator(), media_type="text/event-stream")


@app.post("/api/scrape/csv")
async def scrape_csv(req: ScrapeRequest, request: Request):
    """
    Отдаёт результат как CSV-файл.
    Если есть закэшированный результат последнего парсинга — отдаёт его без повторного парсинга.
    Иначе запускает парсинг заново.
    """
    require_auth(request)
    # Используем кэш, если он есть и содержит данные
    if _last_scrape_result and _last_scrape_result.get("ok"):
        result = _last_scrape_result
    else:
        result = await scrape(req, request)

    if not result.get("ok"):
        return result

    output = io.StringIO()
    writer = csv.writer(output)
    writer.writerow(["company_name", "vacancies", "company_url", "industry_name"])
    for c in result["companies"]:
        writer.writerow([c["name"], c["vacancies"], c["url"], c["industry_name"]])

    output.seek(0)
    return StreamingResponse(
        iter([output.getvalue()]),
        media_type="text/csv",
        headers={"Content-Disposition": "attachment; filename=hh_companies.csv"},
    )
