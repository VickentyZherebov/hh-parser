"""
Тест одного прокси из data/proxies.txt: открывает HH.ru через Playwright
и парсит выдачу по Питеру × «менеджер по продажам».

Цель: убедиться, что прокси из пула Викентия проходит DDoS-Guard на hh.ru.
Если ок — масштабируем на пул из 10 контекстов.

Запуск:
    .venv/bin/python probe_proxy.py            # случайный прокси
    .venv/bin/python probe_proxy.py tiny5      # конкретный прокси по username
"""

import random
import re
import sys
import time
from pathlib import Path
from urllib.parse import quote_plus

from playwright.sync_api import sync_playwright

PROXIES_FILE = Path(__file__).parent / "data" / "proxies.txt"
ARTIFACTS_DIR = Path(__file__).parent / "data" / "probe_artifacts"
ARTIFACTS_DIR.mkdir(parents=True, exist_ok=True)

PROXY_RE = re.compile(r"^http://([^:]+):([^@]+)@([^:]+):(\d+)$")

# Тестовый запрос
TEST_QUERY = "менеджер по продажам"
TEST_AREA = 2          # Санкт-Петербург
USER_AGENT = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
    "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36"
)


def parse_proxies(path: Path) -> list[dict]:
    """Читает строки вида http://user:pass@host:port → список dict для Playwright."""
    proxies = []
    for line in path.read_text().splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        m = PROXY_RE.match(line)
        if not m:
            print(f"⚠️  Не распарсилось: {line}")
            continue
        user, password, host, port = m.groups()
        proxies.append({
            "username": user,
            "password": password,
            "server": f"http://{host}:{port}",
            "host": host,
            "port": port,
        })
    return proxies


def probe(proxy: dict) -> dict:
    """Открывает Питер × «менеджер по продажам» через прокси и парсит выдачу."""
    url = (
        f"https://hh.ru/search/vacancy?"
        f"text={quote_plus(TEST_QUERY)}&area={TEST_AREA}"
        f"&search_field=name&items_on_page=100"
    )

    result = {
        "proxy": f"{proxy['username']}@{proxy['host']}:{proxy['port']}",
        "url": url,
        "ok": False,
        "duration_s": None,
        "status": None,
        "h1": None,
        "vacancies_found": None,
        "companies_found": None,
        "cards_on_page": None,
        "unique_employers_on_page": None,
        "error": None,
    }

    t0 = time.time()
    try:
        with sync_playwright() as p:
            browser = p.chromium.launch(
                headless=True,
                proxy={
                    "server": proxy["server"],
                    "username": proxy["username"],
                    "password": proxy["password"],
                },
            )
            context = browser.new_context(user_agent=USER_AGENT, locale="ru-RU")
            page = context.new_page()

            response = page.goto(url, wait_until="domcontentloaded", timeout=45_000)
            result["status"] = response.status if response else None

            # Ждём H1 (число вакансий рендерится сразу) и хотя бы одну карточку
            try:
                page.wait_for_selector('h1', timeout=15_000)
                page.wait_for_selector('[data-qa="vacancy-serp__vacancy"]', timeout=15_000)
            except Exception:
                pass

            # Скроллим вниз для триггера lazy-load остальных карточек
            page.evaluate("""
                async () => {
                  await new Promise(resolve => {
                    let total = 0;
                    const step = 800;
                    const timer = setInterval(() => {
                      window.scrollBy(0, step);
                      total += step;
                      if (total >= document.body.scrollHeight + 2000) {
                        clearInterval(timer);
                        resolve();
                      }
                    }, 200);
                  });
                }
            """)
            # Дать сети успокоиться после скролла
            try:
                page.wait_for_load_state("networkidle", timeout=10_000)
            except Exception:
                pass

            data = page.evaluate(
                """
                () => {
                  const cards = Array.from(document.querySelectorAll(
                    '[data-qa="vacancy-serp__vacancy"]'
                  ));
                  const items = cards.map(c => ({
                    name: c.querySelector(
                      '[data-qa="serp-item__title"]'
                    )?.innerText?.trim() || null,
                    employer: c.querySelector(
                      '[data-qa="vacancy-serp__vacancy-employer"]'
                    )?.innerText?.trim() || null,
                  }));
                  return {
                    title: document.title,
                    h1: document.querySelector('h1')?.innerText || null,
                    bodyHead: (document.body?.innerText || '').slice(0, 800),
                    items,
                  };
                }
                """
            )

            # Парсим число из H1: «Найдено 1 234 вакансии» → 1234
            h1 = data.get("h1") or ""
            m_vac = re.search(r"Найдено\s+([\d\s ]+)\s+ваканс", h1)
            vacancies = int(re.sub(r"\D", "", m_vac.group(1))) if m_vac else None

            body = data.get("bodyHead") or ""
            m_comp = re.search(r"Найдено\s+([\d\s ]+)\s+компани", body)
            companies = int(re.sub(r"\D", "", m_comp.group(1))) if m_comp else None

            items = data.get("items") or []
            employers = {it["employer"] for it in items if it["employer"]}

            result["ok"] = True
            result["h1"] = h1
            result["vacancies_found"] = vacancies
            result["companies_found"] = companies
            result["cards_on_page"] = len(items)
            result["unique_employers_on_page"] = len(employers)

            # Артефакты на случай отладки
            stamp = time.strftime("%Y%m%d_%H%M%S")
            slug = proxy["username"]
            html_path = ARTIFACTS_DIR / f"{stamp}_{slug}.html"
            png_path = ARTIFACTS_DIR / f"{stamp}_{slug}.png"
            html_path.write_text(page.content(), encoding="utf-8")
            page.screenshot(path=str(png_path), full_page=False)
            result["artifact_html"] = str(html_path.relative_to(Path(__file__).parent))
            result["artifact_png"] = str(png_path.relative_to(Path(__file__).parent))

            context.close()
            browser.close()
    except Exception as e:
        result["error"] = f"{type(e).__name__}: {e}"

    result["duration_s"] = round(time.time() - t0, 2)
    return result


def main() -> None:
    proxies = parse_proxies(PROXIES_FILE)
    if not proxies:
        sys.exit("Нет прокси в data/proxies.txt")

    if len(sys.argv) > 1:
        wanted = sys.argv[1]
        proxy = next((p for p in proxies if p["username"] == wanted), None)
        if not proxy:
            sys.exit(f"Прокси {wanted} не найден в пуле")
    else:
        proxy = random.choice(proxies)

    print(f"🔌 Прокси: {proxy['username']}@{proxy['host']}:{proxy['port']}")
    print(f"🔍 Запрос:  «{TEST_QUERY}», area={TEST_AREA} (СПб)")
    print()

    res = probe(proxy)

    print("═" * 60)
    if res["ok"]:
        print(f"✅ OK за {res['duration_s']}с (HTTP {res['status']})")
        print(f"   H1:                    {res['h1']}")
        print(f"   Найдено вакансий:      {res['vacancies_found']}")
        print(f"   Найдено компаний (HH): {res['companies_found']}")
        print(f"   Карточек на странице:  {res['cards_on_page']}")
        print(f"   Уникальных компаний:   {res['unique_employers_on_page']}")
    else:
        print(f"❌ FAIL за {res['duration_s']}с (HTTP {res['status']})")
        print(f"   {res['error']}")
    if res.get("artifact_html"):
        print(f"   HTML:       {res['artifact_html']}")
        print(f"   Screenshot: {res['artifact_png']}")
    print("═" * 60)


if __name__ == "__main__":
    main()
