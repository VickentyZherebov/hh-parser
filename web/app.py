"""
FastAPI-приложение: веб-версия HH-парсера.
"""

import asyncio
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
    fetch_html,
    parse_industries,
    scrape_companies,
    CATALOG_URL,
    UA,
    HHBlockedError,
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


@app.post("/api/scrape")
async def scrape(req: ScrapeRequest):
    """Запускает парсинг и возвращает результат JSON."""
    try:
        industries = await asyncio.to_thread(_load_industries)
    except Exception as e:
        return {"ok": False, "error": f"Не удалось загрузить индустрии: {e}"}

    slug_set = set(req.industry_slugs)
    chosen = [i for i in industries if i.slug_url in slug_set]

    if not chosen:
        return {"ok": False, "error": "Не выбрано ни одной индустрии."}

    all_companies = []

    try:
        for ind in chosen:
            companies = await asyncio.to_thread(
                scrape_companies,
                ind,
                req.min_vacancies,
                req.area,
                req.only_open,
            )
            all_companies.extend(companies)
    except HHBlockedError as e:
        return {"ok": False, "error": str(e)}
    except Exception as e:
        logger.exception("Ошибка парсинга")
        return {"ok": False, "error": f"Ошибка парсинга: {e}"}

    return {
        "ok": True,
        "companies_count": len(all_companies),
        "companies": [
            {
                "name": c.name,
                "url": c.url,
                "vacancies": c.vacancies,
                "industry_name": c.industry_name,
            }
            for c in all_companies
        ],
    }


@app.post("/api/scrape/csv")
async def scrape_csv(req: ScrapeRequest):
    """Запускает парсинг и отдаёт результат как CSV-файл."""
    result = await scrape(req)

    if not result["ok"]:
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
