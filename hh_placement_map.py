"""Карта активных HH-вакансий, привязанных к рекламным кампаниям Avileads."""

from __future__ import annotations

import asyncio
import os
import re
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from typing import Callable, Iterable, Optional

from bs4 import BeautifulSoup
from playwright.async_api import BrowserContext, async_playwright

from hh_browser_fetcher import PAGE_TIMEOUT_MS, ProxyManager, USER_AGENT


BASE_URL = "https://hh.ru"
DETAIL_CONCURRENCY = int(os.environ.get("HH_PLACEMENT_CONCURRENCY", "4"))
MAX_RKS_PER_REQUEST = int(os.environ.get("HH_PLACEMENT_MAX_RKS", "20"))
MAX_VACANCIES_PER_REQUEST = int(os.environ.get("HH_PLACEMENT_MAX_VACANCIES", "1500"))

COORDS_RE = re.compile(
    r'"lat"\s*:\s*(-?\d+(?:\.\d+)?)[\s\S]{0,120}?'
    r'"lng"\s*:\s*(-?\d+(?:\.\d+)?)'
)
EMPLOYER_RE = re.compile(r"/employer/(\d+)")
ARCHIVE_MARKERS = (
    "вакансия в архиве",
    "вакансия закрыта",
    "такой страницы нет",
    "вакансия уже не доступна",
)


@dataclass(frozen=True)
class VacancyRef:
    hh_id: str
    rk: int
    rk_name: str
    url: str


@dataclass
class VacancyPlacement:
    hh_id: str
    rk: int
    rk_name: str
    url: str
    title: str = ""
    employer: str = ""
    employer_id: str = ""
    address: str = ""
    city: str = ""
    latitude: Optional[float] = None
    longitude: Optional[float] = None
    status: str = "active"
    error: str = ""


def parse_rk_ids(values: Iterable[int]) -> list[int]:
    result: list[int] = []
    seen: set[int] = set()
    for raw_value in values:
        try:
            value = int(raw_value)
        except (TypeError, ValueError) as exc:
            raise ValueError(f"Некорректный номер РК: {raw_value}") from exc
        if value <= 0:
            raise ValueError(f"Некорректный номер РК: {raw_value}")
        if value not in seen:
            seen.add(value)
            result.append(value)
    if not result:
        raise ValueError("Выбери хотя бы одну РК")
    if len(result) > MAX_RKS_PER_REQUEST:
        raise ValueError(f"За один раз можно выбрать не больше {MAX_RKS_PER_REQUEST} РК")
    return result


def _dsn() -> str:
    dsn = os.environ.get("PLACEMENT_MAP_PG_DSN", "").strip()
    if not dsn:
        raise RuntimeError(
            "Для карты не настроен отдельный read-only доступ к Avileads PostgreSQL "
            "(PLACEMENT_MAP_PG_DSN)"
        )
    return dsn


def list_hh_campaigns() -> list[dict]:
    """Возвращает небольшой справочник HH-кампаний без сканирования таблицы ID."""
    import psycopg

    sql = """
        SELECT id, COALESCE(name, ''), COALESCE(load_to_zp, false)
        FROM public.vacancy_avito
        WHERE traffic_source_id = 3
        ORDER BY load_to_zp DESC, id DESC
    """
    with psycopg.connect(_dsn()) as conn:
        conn.execute("SET TRANSACTION READ ONLY")
        rows = conn.execute(sql).fetchall()
    return [
        {"id": int(rk), "name": name, "enabled": bool(enabled)}
        for rk, name, enabled in rows
    ]


def load_vacancy_refs(rk_values: Iterable[int]) -> tuple[list[int], list[VacancyRef]]:
    """Read-only получает HH ID, уже привязанные к выбранным РК."""
    import psycopg

    rk_ids = parse_rk_ids(rk_values)
    sql = """
        SELECT a.avito_id, v.id, COALESCE(v.name, '')
        FROM public.vacancy_avito v
        JOIN public.avito_vacancy_ids a ON a.vacancy_avito_id = v.id
        WHERE v.id = ANY(%s)
          AND v.traffic_source_id = 3
        ORDER BY v.id, a.avito_id
    """
    with psycopg.connect(_dsn()) as conn:
        conn.execute("SET TRANSACTION READ ONLY")
        rows = conn.execute(sql, (rk_ids,)).fetchall()

    refs = [
        VacancyRef(str(hh_id), int(rk), rk_name, f"{BASE_URL}/vacancy/{hh_id}")
        for hh_id, rk, rk_name in rows
    ]
    if len(refs) > MAX_VACANCIES_PER_REQUEST:
        raise ValueError(
            f"К выбранным РК привязано {len(refs)} вакансий; лимит одной проверки — "
            f"{MAX_VACANCIES_PER_REQUEST}. Выбери меньше РК."
        )
    return rk_ids, refs


def parse_vacancy_page(html: str, ref: VacancyRef) -> VacancyPlacement:
    """Извлекает статус, адрес и координаты из публичной карточки HH."""
    soup = BeautifulSoup(html, "lxml")
    page_text = soup.get_text(" ", strip=True).lower()
    archived = any(marker in page_text for marker in ARCHIVE_MARKERS)
    title_node = soup.select_one('h1[data-qa="vacancy-title"]') or soup.select_one("h1")
    employer_node = soup.select_one('a[href*="/employer/"]')
    address_node = soup.select_one('[data-qa="vacancy-view-raw-address"]')
    address = address_node.get_text(" ", strip=True) if address_node else ""
    coords = COORDS_RE.search(html)
    employer_href = employer_node.get("href", "") if employer_node else ""
    employer_match = EMPLOYER_RE.search(employer_href)

    if archived:
        status = "closed"
        error = "вакансия закрыта или в архиве"
    elif not title_node:
        status = "error"
        error = "HH не вернул карточку вакансии"
    else:
        status = "active"
        error = ""

    return VacancyPlacement(
        hh_id=ref.hh_id,
        rk=ref.rk,
        rk_name=ref.rk_name,
        url=ref.url,
        title=title_node.get_text(" ", strip=True) if title_node else "",
        employer=employer_node.get_text(" ", strip=True) if employer_node else "",
        employer_id=employer_match.group(1) if employer_match else "",
        address=address,
        city=address.split(",", 1)[0].strip() if address else "",
        latitude=float(coords.group(1)) if coords else None,
        longitude=float(coords.group(2)) if coords else None,
        status=status,
        error=error,
    )


async def _collect_detail(
    context: BrowserContext,
    ref: VacancyRef,
    semaphore: asyncio.Semaphore,
) -> VacancyPlacement:
    async with semaphore:
        page = await context.new_page()
        try:
            response = await page.goto(ref.url, wait_until="domcontentloaded", timeout=PAGE_TIMEOUT_MS)
            if response and response.status in (404, 410):
                return VacancyPlacement(**asdict(ref), status="closed", error="вакансия закрыта")
            if response and response.status >= 400:
                return VacancyPlacement(
                    **asdict(ref), status="error", error=f"HH вернул HTTP {response.status}"
                )
            await page.wait_for_timeout(350)
            return parse_vacancy_page(await page.content(), ref)
        except Exception as exc:
            return VacancyPlacement(
                **asdict(ref), status="error", error=f"{type(exc).__name__}: {exc}"
            )
        finally:
            await page.close()


async def collect_vacancies_async(
    refs: list[VacancyRef],
    progress_cb: Optional[Callable[[dict], None]] = None,
) -> list[VacancyPlacement]:
    if not refs:
        return []
    proxy_manager = None if os.environ.get("HH_PLACEMENT_NO_PROXY") == "1" else ProxyManager()
    proxy = await proxy_manager.acquire() if proxy_manager else None
    async with async_playwright() as playwright:
        browser = await playwright.chromium.launch(
            headless=True,
            args=["--disable-dev-shm-usage", "--no-sandbox", "--disable-gpu"],
        )
        context_kwargs = {
            "user_agent": USER_AGENT,
            "locale": "ru-RU",
            "viewport": {"width": 1366, "height": 900},
        }
        if proxy:
            context_kwargs["proxy"] = {
                "server": proxy.server,
                "username": proxy.username,
                "password": proxy.password,
            }
        context = await browser.new_context(**context_kwargs)
        try:
            semaphore = asyncio.Semaphore(DETAIL_CONCURRENCY)
            tasks = [_collect_detail(context, ref, semaphore) for ref in refs]
            result: list[VacancyPlacement] = []
            for index, future in enumerate(asyncio.as_completed(tasks), start=1):
                result.append(await future)
                if progress_cb:
                    progress_cb({"stage": "details", "done": index, "total": len(tasks)})
            return result
        finally:
            await context.close()
            await browser.close()


def collect_vacancies(refs: list[VacancyRef]) -> list[VacancyPlacement]:
    return asyncio.run(collect_vacancies_async(refs))


def build_snapshot(name: str, rk_values: Iterable[int]) -> dict:
    """Строит сохраняемый снимок карты; свежий SQLite-кэш не проверяет повторно."""
    from web.db import get_cached_placements, save_cached_placements

    rk_ids, refs = load_vacancy_refs(rk_values)
    cached = get_cached_placements([ref.hh_id for ref in refs])
    placements: list[VacancyPlacement] = []
    missing: list[VacancyRef] = []
    refs_by_id = {ref.hh_id: ref for ref in refs}

    for ref in refs:
        payload = cached.get(ref.hh_id)
        if payload:
            payload.update({"rk": ref.rk, "rk_name": ref.rk_name, "url": ref.url})
            placements.append(VacancyPlacement(**payload))
        else:
            missing.append(ref)

    fresh = collect_vacancies(missing)
    if fresh:
        save_cached_placements([asdict(item) for item in fresh])
        placements.extend(fresh)

    # Асинхронная загрузка меняет порядок; возвращаем порядок связок из БД.
    placements_by_id = {item.hh_id: item for item in placements}
    placements = [placements_by_id[hh_id] for hh_id in refs_by_id if hh_id in placements_by_id]

    markers = [
        asdict(item)
        for item in placements
        if item.status == "active"
        and item.address
        and item.latitude is not None
        and item.longitude is not None
    ]
    issues = []
    for item in placements:
        reasons = []
        if item.error:
            reasons.append(item.error)
        if item.status == "active" and not item.address:
            reasons.append("нет адреса")
        if item.status == "active" and (item.latitude is None or item.longitude is None):
            reasons.append("нет координат")
        if reasons:
            issues.append({**asdict(item), "reasons": reasons})

    active = sum(item.status == "active" for item in placements)
    closed = sum(item.status == "closed" for item in placements)
    return {
        "name": name.strip() or "Карта размещений HH",
        "created_at": datetime.now(timezone.utc).isoformat(),
        "rk_ids": rk_ids,
        "markers": markers,
        "issues": issues,
        "summary": {
            "linked": len(refs),
            "active": active,
            "closed": closed,
            "markers": len(markers),
            "issues": len(issues),
            "checked": len(missing),
            "cached": len(refs) - len(missing),
        },
    }
