import json
import re
import time
from dataclasses import dataclass, asdict
from datetime import datetime, timezone
from urllib.parse import urljoin, urlparse, parse_qs, urlencode, urlunparse

import requests
from bs4 import BeautifulSoup


BASE = "https://hh.ru"
CATALOG_URL = f"{BASE}/employers_company"

UA = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/124.0.0.0 Safari/537.36"
)


@dataclass
class Industry:
    name: str
    slug_url: str  # абсолютная ссылка на индустрию (вида https://hh.ru/employers_company/<slug>)


@dataclass
class Company:
    name: str
    url: str
    vacancies: int
    industry_name: str
    industry_url: str


def fetch_html(url: str, session: requests.Session, timeout: int = 25) -> str:
    r = session.get(url, timeout=timeout)
    r.raise_for_status()
    return r.text


def soupify(html: str) -> BeautifulSoup:
    return BeautifulSoup(html, "lxml")


def parse_industries(catalog_html: str) -> list[Industry]:
    """
    На странице каталога индустрии представлены ссылками вида:
    /employers_company/<slug>
    """
    soup = soupify(catalog_html)

    industries: dict[str, str] = {}  # name -> abs_url
    for a in soup.select("a[href]"):
        href = a.get("href", "").strip()
        text = a.get_text(" ", strip=True)
        if not text:
            continue

        # Индустрии на главной: /employers_company/<slug>
        if href.startswith("/employers_company/") and href.count("/") == 2:
            abs_url = urljoin(BASE, href)
            industries[text] = abs_url

    # Если HH поменял разметку — попробуем более мягко: все ссылки где есть /employers_company/ и не /search/
    if not industries:
        for a in soup.find_all("a", href=True):
            href = a["href"].strip()
            text = a.get_text(" ", strip=True)
            if "/employers_company/" in href and "search" not in href and text:
                abs_url = urljoin(BASE, href)
                if abs_url.startswith(f"{BASE}/employers_company/"):
                    industries[text] = abs_url

    out = [Industry(name=k, slug_url=v) for k, v in industries.items()]
    out.sort(key=lambda x: x.name.lower())
    return out


def with_query(url: str, **params) -> str:
    """
    Добавляет/перезаписывает query-параметры.
    """
    p = urlparse(url)
    q = parse_qs(p.query)
    for k, v in params.items():
        q[k] = [str(v)]
    new_q = urlencode(q, doseq=True)
    return urlunparse((p.scheme, p.netloc, p.path, p.params, new_q, p.fragment))


def extract_max_page(soup: BeautifulSoup) -> int:
    """
    Внизу страницы есть пагинация со ссылками, где встречается page=<N>.
    Берём максимум.
    """
    max_page = 0
    for a in soup.select("a[href]"):
        href = a.get("href", "")
        if "page=" not in href:
            continue
        abs_url = urljoin(BASE, href)
        p = urlparse(abs_url)
        q = parse_qs(p.query)
        if "page" in q:
            try:
                max_page = max(max_page, int(q["page"][0]))
            except Exception:
                pass
    return max_page


def parse_companies_from_industry_page(html: str, industry_name: str, industry_url: str) -> list[Company]:
    """
    На странице индустрии компании идут списком: ссылка на компанию + рядом число вакансий.
    Мы ищем все ссылки /employer/<id> и пытаемся вытащить ближайшее число вакансий.
    """
    soup = soupify(html)
    companies: list[Company] = []

    for a in soup.select('a[href^="/employer/"]'):
        name = a.get_text(" ", strip=True)
        if not name:
            continue

        url = urljoin(BASE, a["href"].strip())

        # Пытаемся найти число вакансий рядом:
        # 1) следующий "текстовый" узел, который является числом
        vacancies = None
        nxt = a.find_next(string=re.compile(r"^\s*\d+\s*$"))
        if nxt:
            try:
                vacancies = int(nxt.strip())
            except Exception:
                vacancies = None

        # 2) если не нашли — берём текст родителя и ищем число "в конце"
        if vacancies is None:
            parent_text = a.parent.get_text(" ", strip=True) if a.parent else ""
            # часто выглядит как: "<CompanyName> 21"
            m = re.search(r"(\d+)\s*$", parent_text)
            if m:
                vacancies = int(m.group(1))

        if vacancies is None:
            # если совсем не удалось — пропускаем (лучше пропустить, чем записать мусор)
            continue

        companies.append(
            Company(
                name=name,
                url=url,
                vacancies=vacancies,
                industry_name=industry_name,
                industry_url=industry_url,
            )
        )

    return companies


def scrape_companies(industry: Industry, min_vacancies: int, area: int = 113, only_with_open_vacancies: bool = True) -> list[Company]:
    """
    Обходит все страницы выбранной индустрии и собирает компании.
    area=113 — Россия (можешь поменять на нужный area).
    """
    session = requests.Session()
    session.headers.update(
        {
            "User-Agent": UA,
            "Accept-Language": "ru-RU,ru;q=0.9,en;q=0.8",
        }
    )

    # На hh есть переключатель "компании с открытыми вакансиями"
    url0 = industry.slug_url
    url0 = with_query(url0, page=0)
    url0 = with_query(url0, area=area)
    if only_with_open_vacancies:
        url0 = with_query(url0, vacanciesRequired="true")

    html0 = fetch_html(url0, session)
    soup0 = soupify(html0)
    max_page = extract_max_page(soup0)

    out: dict[str, Company] = {}  # url -> Company (дедуп по url)

    for page in range(0, max_page + 1):
        url = with_query(url0, page=page)
        html = fetch_html(url, session)
        companies = parse_companies_from_industry_page(html, industry.name, industry.slug_url)

        for c in companies:
            if c.vacancies >= min_vacancies:
                out[c.url] = c

        # небольшая пауза, чтобы не долбить HH
        time.sleep(0.35)

    return list(out.values())


def main():
    session = requests.Session()
    session.headers.update({"User-Agent": UA, "Accept-Language": "ru-RU,ru;q=0.9,en;q=0.8"})

    print("Загружаю список индустрий...")
    catalog_html = fetch_html(CATALOG_URL, session)
    industries = parse_industries(catalog_html)

    if not industries:
        raise RuntimeError("Не удалось распарсить индустрии. Возможно, HH поменял разметку или блокирует запросы.")

    print("\nИндустрии:")
    for i, ind in enumerate(industries, start=1):
        print(f"{i:>3}. {ind.name}")

    idx = int(input("\nВыбери номер индустрии: ").strip())
    industry = industries[idx - 1]

    min_vac = int(input("Минимум вакансий у компании (например 10): ").strip())

    # можно оставить 113 (Россия) — как дефолт. Если хочешь, легко спросить у пользователя.
    area = input("Area (ENTER = 113 Россия): ").strip()
    area = int(area) if area else 113

    print(f"\nСобираю компании: индустрия='{industry.name}', min_vacancies={min_vac}, area={area} ...")
    companies = scrape_companies(industry, min_vacancies=min_vac, area=area, only_with_open_vacancies=True)

    payload = {
        "scraped_at_utc": datetime.now(timezone.utc).isoformat(),
        "industry": asdict(industry),
        "filters": {
            "min_vacancies": min_vac,
            "area": area,
            "only_with_open_vacancies": True,
        },
        "companies_count": len(companies),
        "companies": [asdict(c) for c in sorted(companies, key=lambda x: (-x.vacancies, x.name.lower()))],
    }

    out_file = f"hh_companies_{re.sub(r'\\W+', '_', industry.name.lower()).strip('_')}_min{min_vac}_area{area}.json"
    with open(out_file, "w", encoding="utf-8") as f:
        json.dump(payload, f, ensure_ascii=False, indent=2)

    print(f"\nГотово ✅ Найдено компаний: {len(companies)}")
    print(f"Файл: {out_file}")


if __name__ == "__main__":
    main()
