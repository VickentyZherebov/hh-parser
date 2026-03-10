"""
FastAPI-приложение: веб-версия HH-парсера.
"""

import asyncio
import queue
import uuid
from pathlib import Path
from typing import Optional

from fastapi import FastAPI, Request
from fastapi.responses import HTMLResponse, StreamingResponse
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

logger = logging.getLogger(__name__)

app = FastAPI(title="HH-Parser Web", version=__version__)

BASE_DIR = Path(__file__).resolve().parent
app.mount("/static", StaticFiles(directory=BASE_DIR / "static"), name="static")
templates = Jinja2Templates(directory=BASE_DIR / "templates")

# Кэш индустрий (загружается один раз)
_industries_cache: list[Industry] = []

# Кэш последнего результата парсинга — чтобы «Скачать CSV» не парсил заново
_last_scrape_result: Optional[dict] = None


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


# ---------- Роуты ----------

@app.get("/", response_class=HTMLResponse)
async def index(request: Request):
    return templates.TemplateResponse("index.html", {"request": request, "version": __version__})


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
async def scrape(req: ScrapeRequest):
    """Запускает парсинг и возвращает результат JSON (без прогресса).
    Использует параллельный парсинг индустрий для ускорения."""
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
async def scrape_stream(req: ScrapeRequest):
    """
    SSE-эндпоинт: парсит с прогрессом в реальном времени.
    Несколько индустрий парсятся параллельно в разных потоках (каждый со своей сессией).
    Отправляет события:
      - type=progress  — прогресс по страницам/индустриям
      - type=result    — финальный результат
      - type=error     — ошибка
    """
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
        loop = asyncio.get_event_loop()
        loop.run_in_executor(None, worker)

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
async def scrape_csv(req: ScrapeRequest):
    """
    Отдаёт результат как CSV-файл.
    Если есть закэшированный результат последнего парсинга — отдаёт его без повторного парсинга.
    Иначе запускает парсинг заново.
    """
    # Используем кэш, если он есть и содержит данные
    if _last_scrape_result and _last_scrape_result.get("ok"):
        result = _last_scrape_result
    else:
        result = await scrape(req)

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
