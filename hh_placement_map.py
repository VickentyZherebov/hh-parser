"""Карта активных HH-вакансий, привязанных к рекламным кампаниям Avileads."""

from __future__ import annotations

import asyncio
import logging
import os
import re
import time
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from typing import Callable, Iterable, Optional

from bs4 import BeautifulSoup
from playwright.async_api import BrowserContext, async_playwright

from hh_browser_fetcher import PAGE_TIMEOUT_MS, ProxyManager, USER_AGENT

logger = logging.getLogger(__name__)


BASE_URL = "https://hh.ru"
DETAIL_CONCURRENCY = int(os.environ.get("HH_PLACEMENT_CONCURRENCY", "4"))
DETAIL_ATTEMPTS = int(os.environ.get("HH_PLACEMENT_ATTEMPTS", "3"))
MAX_RKS_PER_REQUEST = int(os.environ.get("HH_PLACEMENT_MAX_RKS", "20"))
MAX_VACANCIES_PER_REQUEST = int(os.environ.get("HH_PLACEMENT_MAX_VACANCIES", "1500"))
GEOCODER_URL = os.environ.get(
    "HH_PLACEMENT_GEOCODER_URL", "https://nominatim.openstreetmap.org/search"
)
GEOCODER_USER_AGENT = os.environ.get(
    "HH_PLACEMENT_GEOCODER_USER_AGENT",
    "Avileads-HH-Placement-Map/1.0 (support@avileads.ru)",
)

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
CHALLENGE_MARKERS = (
    "подтвердите, что вы не робот",
    "проверка, что вы не робот",
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
    location_precision: str = "exact"
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
    challenged = (
        any(marker in page_text for marker in CHALLENGE_MARKERS)
        or soup.select_one('[data-qa*="captcha"], form[action*="captcha"]') is not None
    )
    title_node = soup.select_one('h1[data-qa="vacancy-title"]') or soup.select_one("h1")
    employer_node = soup.select_one('a[href*="/employer/"]')
    address_node = soup.select_one('[data-qa="vacancy-view-raw-address"]')
    location_node = soup.select_one('[data-qa="vacancy-address-with-map"]')
    address = address_node.get_text(" ", strip=True) if address_node else ""
    location = location_node.get_text(" ", strip=True) if location_node else ""
    coords = COORDS_RE.search(html)
    employer_href = employer_node.get("href", "") if employer_node else ""
    employer_match = EMPLOYER_RE.search(employer_href)

    if challenged:
        status = "error"
        error = "HH временно запросил проверку — карточка будет загружена повторно"
    elif archived:
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
        title=title_node.get_text(" ", strip=True) if title_node and not challenged else "",
        employer=employer_node.get_text(" ", strip=True) if employer_node else "",
        employer_id=employer_match.group(1) if employer_match else "",
        address=address,
        city=address.split(",", 1)[0].strip() if address else location,
        latitude=float(coords.group(1)) if coords else None,
        longitude=float(coords.group(2)) if coords else None,
        location_precision="exact" if address else ("city" if location else ""),
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
    async with async_playwright() as playwright:
        browser = await playwright.chromium.launch(
            headless=True,
            args=["--disable-dev-shm-usage", "--no-sandbox", "--disable-gpu"],
        )
        try:
            pending = list(refs)
            result_by_id: dict[str, VacancyPlacement] = {}
            attempts = DETAIL_ATTEMPTS if proxy_manager else 1
            for attempt in range(1, attempts + 1):
                proxy = await proxy_manager.acquire() if proxy_manager else None
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
                    tasks = [_collect_detail(context, ref, semaphore) for ref in pending]
                    retry_refs: list[VacancyRef] = []
                    refs_by_id = {ref.hh_id: ref for ref in pending}
                    for index, future in enumerate(asyncio.as_completed(tasks), start=1):
                        item = await future
                        if item.status == "error" and attempt < attempts:
                            retry_refs.append(refs_by_id[item.hh_id])
                        else:
                            result_by_id[item.hh_id] = item
                        if progress_cb:
                            progress_cb({
                                "stage": "details", "done": index, "total": len(tasks),
                                "attempt": attempt,
                            })
                finally:
                    await context.close()

                if not retry_refs:
                    break
                if proxy_manager and proxy:
                    await proxy_manager.quarantine(proxy)
                pending = retry_refs

            return [result_by_id[ref.hh_id] for ref in refs if ref.hh_id in result_by_id]
        finally:
            await browser.close()


def collect_vacancies(refs: list[VacancyRef]) -> list[VacancyPlacement]:
    return asyncio.run(collect_vacancies_async(refs))


def is_transient_payload(payload: dict) -> bool:
    """Отбрасывает ошибки и старые ошибочно закэшированные CAPTCHA-страницы."""
    title = str(payload.get("title", "")).lower()
    needs_location_upgrade = (
        payload.get("status") == "active"
        and not payload.get("address")
        and "location_precision" not in payload
    )
    return (
        payload.get("status") == "error"
        or any(marker in title for marker in CHALLENGE_MARKERS)
        or needs_location_upgrade
    )


def geocode_city_centers(items: list[VacancyPlacement]) -> None:
    """Дополняет city-only карточки центром города и сохраняет результат навсегда."""
    import requests
    from web.db import get_cached_geocodes, save_cached_geocode

    targets = [
        item for item in items
        if item.status == "active"
        and not item.address
        and item.city
        and (item.latitude is None or item.longitude is None)
    ]
    if not targets:
        return

    queries = {item.city: f"{item.city}, Россия" for item in targets}
    cached = get_cached_geocodes(list(queries.values()))
    last_request_at = 0.0
    for city, query in queries.items():
        if query in cached:
            continue
        wait_for = 1.05 - (time.monotonic() - last_request_at)
        if wait_for > 0:
            time.sleep(wait_for)
        try:
            response = requests.get(
                GEOCODER_URL,
                params={"q": query, "format": "jsonv2", "limit": 1, "countrycodes": "ru"},
                headers={"User-Agent": GEOCODER_USER_AGENT},
                timeout=20,
            )
            last_request_at = time.monotonic()
            response.raise_for_status()
            rows = response.json()
        except (requests.RequestException, ValueError) as exc:
            logger.warning("Не удалось найти центр города %s: %s", city, exc)
            continue
        if rows:
            row = rows[0]
            cached[query] = {
                "latitude": float(row["lat"]),
                "longitude": float(row["lon"]),
                "display_name": row.get("display_name", city),
            }
            save_cached_geocode(query, cached[query])

    for item in targets:
        match = cached.get(queries[item.city])
        if match:
            item.latitude = match["latitude"]
            item.longitude = match["longitude"]
            item.location_precision = "city"


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
        if payload and not is_transient_payload(payload):
            payload.update({"rk": ref.rk, "rk_name": ref.rk_name, "url": ref.url})
            placements.append(VacancyPlacement(**payload))
        else:
            missing.append(ref)

    fresh = collect_vacancies(missing)
    geocode_city_centers(fresh)
    stable = [
        item for item in fresh
        if item.status == "closed"
        or (
            item.status == "active"
            and (item.address or (item.latitude is not None and item.longitude is not None))
        )
    ]
    if stable:
        save_cached_placements([asdict(item) for item in stable])
        placements.extend(fresh)

    # Асинхронная загрузка меняет порядок; возвращаем порядок связок из БД.
    placements_by_id = {item.hh_id: item for item in placements}
    placements = [placements_by_id[hh_id] for hh_id in refs_by_id if hh_id in placements_by_id]

    markers = [
        asdict(item)
        for item in placements
        if item.status == "active"
        and item.latitude is not None
        and item.longitude is not None
    ]
    issues = []
    for item in placements:
        reasons = []
        if item.error:
            reasons.append(item.error)
        if item.status == "active" and not item.address and not item.city:
            reasons.append("на HH не указан адрес или город")
        elif item.status == "active" and not item.address and (
            item.latitude is None or item.longitude is None
        ):
            reasons.append(f"на HH указан только город: {item.city}; центр города не найден")
        elif item.status == "active" and item.address and (
            item.latitude is None or item.longitude is None
        ):
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
            "city_centers": sum(
                item.location_precision == "city"
                and item.latitude is not None
                and item.longitude is not None
                for item in placements
            ),
            "issues": len(issues),
            "checked": len(missing),
            "cached": len(refs) - len(missing),
        },
    }
