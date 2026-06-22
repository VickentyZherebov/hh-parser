"""
Общий слой доступа к данным открытой статистики HeadHunter (stats.hh.ru).

Источник данных
---------------
Весь датасет по стране отдаётся одним запросом:

    GET https://stats.hh.ru/api/v1/data/RU
    (обязателен заголовок Referer: https://stats.hh.ru/..., иначе 403)

Ответ — ~21 МБ JSON, структура:

    {
      "<area_id>": {                      # id региона/города = area_id из api.hh.ru
        "hhindex":            {"<profAreaId>": <матрица>, "all": <матрица>},
        "averageCompensation":{...},      # средняя ПРЕДЛАГАЕМАЯ зарплата (оффер)
        "averageExpected":    {...},      # средняя ОЖИДАЕМАЯ зарплата (резюме)
        "numberVacancies":    {...},
        "numberResumes":      {...},
        ... (разбивки по форматам работы и пр.)
      },
      ...
    }

Матрица временного ряда:

    [["", "2025", "2026"],     # шапка: метка + годы
     [0,  "5.2",  "11.3"],     # строка месяца: индекс месяца (0=январь) + значения по годам
     [1,  "5.5",  "11"],
     ...
     [11, "10.7", None]]       # None — данных ещё нет

profAreaId ("1".."27") совпадает с id категорий справочника
api.hh.ru/professional_roles. Ключ "all" — агрегат по всем профобластям.

Этот модуль не зависит от веб-части и используется CLI-скриптами
hh_market_index.py (Блок 1) и hh_market_salary.py (Блок 2).
"""

from __future__ import annotations

import json
import logging
from pathlib import Path
from typing import Optional

import requests

logger = logging.getLogger(__name__)

STATS_BASE = "https://stats.hh.ru"
STATS_DATA_URL = f"{STATS_BASE}/api/v1/data/RU"
HH_API_BASE = "https://api.hh.ru"

# Заголовки: Referer обязателен (без него бэкенд stats отдаёт 403).
STATS_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
        "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0 Safari/537.36"
    ),
    "Referer": f"{STATS_BASE}/",
    "Accept": "application/json, text/plain, */*",
    "Accept-Language": "ru-RU,ru;q=0.9,en;q=0.8",
    "X-Requested-With": "XMLHttpRequest",
}

# Названия месяцев для отображения (индекс 0 = январь)
MONTHS_RU = [
    "январь", "февраль", "март", "апрель", "май", "июнь",
    "июль", "август", "сентябрь", "октябрь", "ноябрь", "декабрь",
]

# Метрики stats.hh.ru, которые мы используем
METRIC_HH_INDEX = "hhindex"
METRIC_AVG_OFFERED = "averageCompensation"   # предлагаемая (в вакансиях)
METRIC_AVG_EXPECTED = "averageExpected"      # ожидаемая (в резюме)
METRIC_NUM_VACANCIES = "numberVacancies"
METRIC_NUM_RESUMES = "numberResumes"

# id «Россия» в дереве регионов HH — служит корнем, не годится как fallback города.
COUNTRY_AREA_ID = 113

# ── Кэши процесса ─────────────────────────────────────────────────────────────
_stats_data_cache: Optional[dict] = None
_prof_areas_cache: Optional[list[dict]] = None
_areas_full_cache: Optional[dict] = None  # {"name_to_id": {...}, "id_to": {id: {"name","parent"}}}


# ── Загрузка датасета stats.hh.ru ─────────────────────────────────────────────

def fetch_stats_data(
    *,
    force: bool = False,
    cache_path: Optional[str | Path] = None,
    cache_max_age_hours: float = 12.0,
    timeout: int = 60,
) -> dict:
    """
    Загружает (с кэшированием) полный датасет статистики HH по стране.

    Кэш в памяти процесса + опциональный кэш на диске (cache_path). Дисковый кэш
    используется, если файл свежее cache_max_age_hours. Данные обновляются ~раз в
    месяц, поэтому частые перезагрузки 21 МБ не нужны.

    Аргументы:
        force              — игнорировать кэши и скачать заново.
        cache_path         — путь к файлу дискового кэша (если задан).
        cache_max_age_hours — допустимый возраст дискового кэша в часах.
        timeout            — таймаут HTTP-запроса, сек.

    Возвращает dict: {area_id(str) -> {metric -> {profAreaId(str) -> матрица}}}.
    """
    global _stats_data_cache
    if _stats_data_cache is not None and not force:
        return _stats_data_cache

    cache_file = Path(cache_path) if cache_path else None

    if cache_file and cache_file.exists() and not force:
        import time
        age_h = (time.time() - cache_file.stat().st_mtime) / 3600.0
        if age_h <= cache_max_age_hours:
            logger.info("Читаю кэш статистики HH с диска: %s (возраст %.1f ч)", cache_file, age_h)
            with cache_file.open("r", encoding="utf-8") as fh:
                _stats_data_cache = json.load(fh)
            return _stats_data_cache

    logger.info("Скачиваю датасет статистики HH: %s", STATS_DATA_URL)
    resp = requests.get(STATS_DATA_URL, headers=STATS_HEADERS, timeout=timeout)
    resp.raise_for_status()
    data = resp.json()
    logger.info("Загружено регионов: %d", len(data))

    if cache_file:
        cache_file.parent.mkdir(parents=True, exist_ok=True)
        with cache_file.open("w", encoding="utf-8") as fh:
            json.dump(data, fh, ensure_ascii=False)
        logger.info("Датасет сохранён в кэш: %s", cache_file)

    _stats_data_cache = data
    return data


# ── Справочник профобластей ───────────────────────────────────────────────────

def load_prof_areas(*, include_all: bool = True, timeout: int = 30) -> list[dict]:
    """
    Справочник профобластей HH: [{"id": int, "name": str}, ...].

    Берётся из категорий api.hh.ru/professional_roles — их id совпадают с
    profAreaId в датасете stats.hh.ru. При include_all=True первой строкой
    добавляется псевдо-профобласть {"id": "all", "name": "Все профобласти"}.
    """
    global _prof_areas_cache
    if _prof_areas_cache is None:
        logger.info("Загружаю справочник профобластей HH...")
        resp = requests.get(f"{HH_API_BASE}/professional_roles", timeout=timeout)
        resp.raise_for_status()
        data = resp.json()
        areas = [
            {"id": int(c["id"]), "name": c.get("name", "").strip()}
            for c in data.get("categories", [])
            if c.get("id") is not None
        ]
        areas.sort(key=lambda a: a["name"].lower())
        _prof_areas_cache = areas

    result = list(_prof_areas_cache)
    if include_all:
        result.insert(0, {"id": "all", "name": "Все профобласти"})
    return result


def prof_area_name_map(*, include_all: bool = True) -> dict:
    """Карта profAreaId -> название (ключи str, чтобы совпадать с ключами датасета)."""
    return {str(pa["id"]): pa["name"] for pa in load_prof_areas(include_all=include_all)}


# ── Извлечение значений из матриц ─────────────────────────────────────────────

def get_series(data: dict, area_id: int | str, metric: str, prof_area_id: int | str) -> Optional[list]:
    """
    Возвращает матрицу временного ряда для (регион, метрика, профобласть) или None,
    если такого среза нет в датасете.
    """
    region = data.get(str(area_id))
    if not region:
        return None
    metric_block = region.get(metric)
    if not metric_block:
        return None
    return metric_block.get(str(prof_area_id))


def last_value(series: Optional[list]) -> Optional[dict]:
    """
    Возвращает последнее доступное (не-null) значение временного ряда.

    Ряд: [["", "2025", "2026"], [monthIdx, v2025, v2026], ...]. Ищем самый поздний
    месяц с данными: идём по колонкам годов справа налево, внутри колонки — по
    месяцам снизу вверх (от декабря к январю).

    Возвращает {"value": float, "year": str, "month": int, "month_name": str}
    или None, если данных нет.
    """
    if not series or len(series) < 2:
        return None

    header = series[0]
    rows = series[1:]
    # Колонки годов идут с индекса 1 (индекс 0 — номер месяца)
    n_year_cols = len(header) - 1
    if n_year_cols < 1:
        return None

    for col in range(n_year_cols, 0, -1):  # справа налево
        year_label = str(header[col]) if col < len(header) else ""
        for row in reversed(rows):          # снизу вверх по месяцам
            if col >= len(row):
                continue
            raw = row[col]
            if raw is None or raw == "":
                continue
            try:
                value = float(raw)
            except (TypeError, ValueError):
                continue
            month_idx = int(row[0])
            return {
                "value": value,
                "year": year_label,
                "month": month_idx,
                "month_name": MONTHS_RU[month_idx] if 0 <= month_idx < 12 else str(month_idx),
            }
    return None


def get_last(data: dict, area_id: int | str, metric: str, prof_area_id: int | str) -> Optional[dict]:
    """Удобная обёртка: последнее значение метрики для (регион, профобласть)."""
    return last_value(get_series(data, area_id, metric, prof_area_id))


# ── Состав датасета ───────────────────────────────────────────────────────────

def available_region_ids(data: dict) -> set[str]:
    """Множество area_id (str), присутствующих в датасете статистики."""
    return set(data.keys())


# ── Дерево регионов с родителями (для fallback города → регион) ────────────────

def load_areas_full(*, timeout: int = 30) -> dict:
    """
    Загружает дерево регионов HH (api.hh.ru/areas) с информацией о родителях.

    Возвращает {"name_to_id": {name.lower(): area_id},
                "id_to": {area_id: {"name": str, "parent": Optional[int]}}}.
    """
    global _areas_full_cache
    if _areas_full_cache is not None:
        return _areas_full_cache

    logger.info("Загружаю дерево регионов HH (с родителями)...")
    resp = requests.get(f"{HH_API_BASE}/areas", timeout=timeout)
    resp.raise_for_status()
    data = resp.json()

    name_to_id: dict[str, int] = {}
    id_to: dict[int, dict] = {}

    def _walk(node: dict, parent: Optional[int]) -> None:
        area_id = int(node["id"])
        name = (node.get("name") or "").strip()
        id_to[area_id] = {"name": name, "parent": parent}
        if name:
            name_to_id.setdefault(name.lower(), area_id)
        for child in node.get("areas", []):
            _walk(child, area_id)

    for country in data:
        _walk(country, None)

    _areas_full_cache = {"name_to_id": name_to_id, "id_to": id_to}
    return _areas_full_cache


def resolve_to_stats_region(
    area_id: int | str,
    region_ids: set[str],
    *,
    areas_full: Optional[dict] = None,
) -> Optional[dict]:
    """
    Подбирает area_id, по которому в датасете stats есть данные.

    Если сам город присутствует в датасете — возвращает его (level="city").
    Иначе поднимается по дереву к родительскому региону (край/область); первый
    предок, присутствующий в датасете и не являющийся «Россией», возвращается с
    level="region". Если данных нет ни на каком уровне (кроме страны) — None.

    Возвращает {"id": int, "name": str, "level": "city"|"region"} или None.
    """
    af = areas_full or load_areas_full()
    id_to = af["id_to"]

    cur: Optional[int] = int(area_id)
    level = "city"
    while cur is not None and cur != COUNTRY_AREA_ID:
        if str(cur) in region_ids:
            return {"id": cur, "name": id_to.get(cur, {}).get("name", str(cur)), "level": level}
        cur = id_to.get(cur, {}).get("parent")
        level = "region"
    return None
