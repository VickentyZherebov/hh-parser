"""
Парсинг вакансий HH.ru через Playwright + ротацию прокси.

Заменяет прежний путь через api.hh.ru/vacancies, который HH закрыл в апреле 2026.
Теперь ходим на публичную HTML-страницу /search/vacancy, проходя DDoS-Guard
через настоящий Chromium headless.

Архитектура:
- ProxyManager — пул прокси из data/proxies.txt с cooldown и карантином
- BrowserPool — N асинхронных воркеров, каждый держит контекст со своим прокси
                (sticky), ротирует контекст каждые 30 задач или 5 минут
- fetch_cell — открывает все страницы одного поиска в одном контексте,
               возвращает (total_vacancies, unique_employers)
- scrape_via_browser — обёртка, синхронная как старая fetch_vacancies()
"""

from __future__ import annotations

import asyncio
import logging
import os
import random
import re
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable, Optional
from urllib.parse import urlencode

from playwright.async_api import (
    Browser,
    BrowserContext,
    async_playwright,
)

logger = logging.getLogger(__name__)

# ── Конфигурация ──────────────────────────────────────────────────────────────

PROXIES_FILE = Path(__file__).parent / "data" / "proxies.txt"
PROXY_RE = re.compile(r"^http://([^:]+):([^@]+)@([^:]+):(\d+)$")

USER_AGENT = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
    "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36"
)

DEFAULT_POOL_SIZE = int(os.environ.get("HH_POOL_SIZE", "10"))
DEFAULT_MAX_TASKS_PER_CONTEXT = 30
DEFAULT_MAX_MINUTES_PER_CONTEXT = 5.0
DEFAULT_PROXY_COOLDOWN_S = 60.0
DEFAULT_QUARANTINE_S = 300.0

PAGE_TIMEOUT_MS = 45_000
SELECTOR_TIMEOUT_MS = 10_000
MAX_PAGES = 20  # HH отдаёт максимум 2000 результатов = 20 страниц по 100

BASE_SEARCH_URL = "https://hh.ru/search/vacancy"


# ── Прокси ────────────────────────────────────────────────────────────────────

@dataclass
class Proxy:
    username: str
    password: str
    host: str
    port: str
    last_used_at: float = 0.0
    quarantine_until: float = 0.0

    @property
    def server(self) -> str:
        return f"http://{self.host}:{self.port}"

    @property
    def label(self) -> str:
        return f"{self.username}@{self.host}:{self.port}"


class ProxyManager:
    def __init__(
        self,
        path: Path = PROXIES_FILE,
        cooldown_s: float = DEFAULT_PROXY_COOLDOWN_S,
    ):
        self.proxies: list[Proxy] = self._load(path)
        self.cooldown_s = cooldown_s
        self._lock = asyncio.Lock()
        if not self.proxies:
            raise RuntimeError(
                f"Нет прокси: проверь {path} или env-переменную HH_PROXIES"
            )
        logger.info("ProxyManager: загружено %d прокси", len(self.proxies))

    @staticmethod
    def _load(path: Path) -> list[Proxy]:
        """
        Источник прокси (по приоритету):
        1. Env-переменная HH_PROXIES — список строк через перевод строки
           или через `;`. Удобно на Amvera (без волюма).
        2. Файл path (по умолчанию data/proxies.txt) — для локальной разработки.
        """
        env = os.environ.get("HH_PROXIES", "").strip()
        if env:
            # Поддерживаем оба разделителя: \n и ; (на случай однострочного env)
            raw_lines = re.split(r"[\n;]+", env)
        elif path.exists():
            raw_lines = path.read_text().splitlines()
        else:
            return []

        out: list[Proxy] = []
        for line in raw_lines:
            line = line.strip()
            if not line or line.startswith("#"):
                continue
            m = PROXY_RE.match(line)
            if m:
                u, p, h, port = m.groups()
                out.append(Proxy(u, p, h, port))
        return out

    async def acquire(self) -> Proxy:
        """
        Берёт случайный прокси, прошедший cooldown и не в карантине.
        Если все на cooldown — берёт самый давно использовавшийся (не в карантине).
        Если все в карантине — RuntimeError.
        """
        async with self._lock:
            now = time.time()
            available_active = [
                p for p in self.proxies
                if p.quarantine_until < now
                and (now - p.last_used_at) >= self.cooldown_s
            ]
            if available_active:
                proxy = random.choice(available_active)
            else:
                non_quarantined = [
                    p for p in self.proxies if p.quarantine_until < now
                ]
                if not non_quarantined:
                    raise RuntimeError(
                        "Все прокси в карантине — попробуй позже"
                    )
                proxy = min(non_quarantined, key=lambda p: p.last_used_at)
            proxy.last_used_at = now
            return proxy

    async def quarantine(
        self, proxy: Proxy, duration_s: float = DEFAULT_QUARANTINE_S
    ) -> None:
        async with self._lock:
            proxy.quarantine_until = time.time() + duration_s
            logger.warning("Прокси %s в карантине на %.0fс", proxy.label, duration_s)


# ── Задачи и результаты ───────────────────────────────────────────────────────

@dataclass
class CellTask:
    """Одна ячейка таблицы: город × запрос. Запрос — text ИЛИ professional_role."""
    city: str
    area_id: int
    text: Optional[str] = None
    professional_role: Optional[int] = None
    role_name: Optional[str] = None
    filter_text: Optional[str] = None      # пост-фильтр по названию
    filter_exact: bool = False             # True = точное совпадение, False = вхождение

    @property
    def query_label(self) -> str:
        if self.text:
            return self.text
        if self.role_name:
            return self.role_name
        if self.professional_role is not None:
            return f"role={self.professional_role}"
        return "?"


@dataclass
class CellResult:
    task: CellTask
    total_vacancies: int = 0      # сколько HH написал «Найдено N вакансий»
    matched_vacancies: int = 0    # после filter_text (если задан); иначе == total_vacancies
    unique_employers: int = 0     # дедуп по employer name
    pages_processed: int = 0
    url: str = ""
    duration_s: float = 0.0
    error: Optional[str] = None


# ── Парсинг одной ячейки ──────────────────────────────────────────────────────

async def fetch_cell(context: BrowserContext, task: CellTask) -> CellResult:
    """
    Открывает все страницы поиска одного запроса в данном контексте.
    Один контекст = один прокси = один cookie jar = HH воспринимает как
    один пользователь, листающий выдачу.
    """
    params: dict = {"area": task.area_id, "items_on_page": 100, "page": 0}
    if task.text:
        params["text"] = task.text
        params["search_field"] = "name"
    elif task.professional_role is not None:
        params["professional_role"] = task.professional_role
    else:
        raise ValueError("CellTask требует text или professional_role")

    employers: set[str] = set()
    total_vacancies = 0
    matched = 0
    pages_done = 0
    first_url = ""
    t0 = time.time()

    page = await context.new_page()
    try:
        for page_idx in range(MAX_PAGES):
            params["page"] = page_idx
            url = f"{BASE_SEARCH_URL}?{urlencode(params)}"
            if page_idx == 0:
                first_url = url

            response = await page.goto(
                url, wait_until="domcontentloaded", timeout=PAGE_TIMEOUT_MS
            )
            if response and response.status >= 400:
                raise RuntimeError(f"HTTP {response.status} on {url}")

            # Ждём H1 с числом «Найдено N вакансий» — иначе можем словить
            # placeholder-H1 пока JS дорендеривает счётчик.
            try:
                await page.wait_for_function(
                    """() => {
                      const h1 = document.querySelector('h1');
                      return h1 && /Найдено/.test(h1.innerText || '');
                    }""",
                    timeout=SELECTOR_TIMEOUT_MS,
                )
            except Exception:
                pass

            # Скролл для триггера lazy-load всех 100 карточек
            await page.evaluate("""
                async () => {
                  await new Promise(resolve => {
                    let total = 0;
                    const step = 2000;
                    const timer = setInterval(() => {
                      window.scrollBy(0, step);
                      total += step;
                      if (total >= document.body.scrollHeight + 1500) {
                        clearInterval(timer);
                        resolve();
                      }
                    }, 80);
                  });
                }
            """)

            data = await page.evaluate("""
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
                  const h1 = document.querySelector('h1')?.innerText || '';
                  const pagerNums = Array.from(document.querySelectorAll(
                    '[data-qa="pager-page"]'
                  )).map(a => parseInt(a.innerText, 10)).filter(n => !isNaN(n));
                  const lastPage = pagerNums.length ? Math.max(...pagerNums) : 1;
                  return { items, h1, lastPage };
                }
            """)

            if page_idx == 0:
                m = re.search(r"Найден[оаы]?\s+([\d\s  ]+)\s+ваканс", data["h1"])
                if m:
                    total_vacancies = int(re.sub(r"\D", "", m.group(1)))
                else:
                    logger.warning(
                        "Не удалось распарсить H1 для %s × %s. H1 = %r",
                        task.city, task.query_label, data["h1"][:200],
                    )

            need_filter = task.filter_text is not None
            filter_lower = (task.filter_text or "").lower()

            for item in data["items"]:
                name = (item.get("name") or "").lower()
                if need_filter:
                    if task.filter_exact:
                        if name != filter_lower:
                            continue
                    else:
                        if filter_lower not in name:
                            continue
                matched += 1
                if item.get("employer"):
                    employers.add(item["employer"])

            pages_done += 1

            # Конец пагинации
            last_page = data.get("lastPage", 1) or 1
            if not data["items"] or page_idx + 1 >= last_page:
                break
    finally:
        await page.close()

    return CellResult(
        task=task,
        total_vacancies=total_vacancies,
        matched_vacancies=matched if task.filter_text else total_vacancies,
        unique_employers=len(employers),
        pages_processed=pages_done,
        url=first_url,
        duration_s=round(time.time() - t0, 2),
    )


# ── Пул контекстов ────────────────────────────────────────────────────────────

class BrowserPool:
    """
    Один Chromium browser + N асинхронных воркеров.
    Каждый воркер держит контекст со своим прокси (sticky).
    Контекст ротируется после max_tasks или max_minutes.
    """

    def __init__(
        self,
        *,
        pool_size: int = DEFAULT_POOL_SIZE,
        max_tasks_per_context: int = DEFAULT_MAX_TASKS_PER_CONTEXT,
        max_minutes_per_context: float = DEFAULT_MAX_MINUTES_PER_CONTEXT,
        proxy_manager: Optional[ProxyManager] = None,
        progress_cb: Optional[Callable[[dict], None]] = None,
    ):
        self.pool_size = pool_size
        self.max_tasks = max_tasks_per_context
        self.max_seconds = max_minutes_per_context * 60
        self.pm = proxy_manager or ProxyManager()
        self.progress_cb = progress_cb
        self._browser: Optional[Browser] = None
        self._playwright = None

    def _emit(self, **kw) -> None:
        if self.progress_cb:
            try:
                self.progress_cb(kw)
            except Exception:
                logger.exception("progress_cb упал")

    async def __aenter__(self) -> "BrowserPool":
        self._playwright = await async_playwright().start()
        # --disable-dev-shm-usage: в Docker /dev/shm мал (64 МБ) → Page crashed /
        # TimeoutError на больших прогонах. --no-sandbox/--disable-gpu — для headless
        # в контейнере без GPU (ome).
        self._browser = await self._playwright.chromium.launch(
            headless=True,
            args=["--disable-dev-shm-usage", "--no-sandbox", "--disable-gpu"],
        )
        return self

    async def __aexit__(self, *args) -> None:
        try:
            if self._browser:
                await self._browser.close()
        finally:
            if self._playwright:
                await self._playwright.stop()

    async def fetch_many(self, tasks: list[CellTask]) -> list[CellResult]:
        if not tasks:
            return []
        if not self._browser:
            raise RuntimeError("Используй BrowserPool как async context manager")

        queue: asyncio.Queue[CellTask] = asyncio.Queue()
        for t in tasks:
            queue.put_nowait(t)

        results: list[CellResult] = []
        results_lock = asyncio.Lock()
        total = len(tasks)
        done = 0
        done_lock = asyncio.Lock()

        async def worker(worker_id: int) -> None:
            nonlocal done
            context: Optional[BrowserContext] = None
            current_proxy: Optional[Proxy] = None
            tasks_in_ctx = 0
            ctx_started_at = 0.0

            async def open_new_context() -> None:
                nonlocal context, current_proxy, tasks_in_ctx, ctx_started_at
                if context:
                    try:
                        await context.close()
                    except Exception:
                        pass
                current_proxy = await self.pm.acquire()
                context = await self._browser.new_context(
                    proxy={
                        "server": current_proxy.server,
                        "username": current_proxy.username,
                        "password": current_proxy.password,
                    },
                    user_agent=USER_AGENT,
                    locale="ru-RU",
                    viewport={"width": 1366, "height": 900},
                )
                tasks_in_ctx = 0
                ctx_started_at = time.time()
                self._emit(
                    type="context_open",
                    worker=worker_id,
                    proxy=current_proxy.label,
                )

            try:
                while True:
                    try:
                        task = queue.get_nowait()
                    except asyncio.QueueEmpty:
                        break

                    need_new = (
                        context is None
                        or tasks_in_ctx >= self.max_tasks
                        or (time.time() - ctx_started_at) > self.max_seconds
                    )
                    if need_new:
                        await open_new_context()

                    try:
                        res = await fetch_cell(context, task)
                        async with results_lock:
                            results.append(res)
                        async with done_lock:
                            done += 1
                            self._emit(
                                type="cell_done",
                                worker=worker_id,
                                proxy=current_proxy.label if current_proxy else None,
                                done=done,
                                total=total,
                                city=task.city,
                                query=task.query_label,
                                total_vacancies=res.total_vacancies,
                                unique_employers=res.unique_employers,
                                duration_s=res.duration_s,
                            )
                        tasks_in_ctx += 1
                    except Exception as e:
                        err = f"{type(e).__name__}: {e}"
                        logger.warning("Ячейка %s × %s упала: %s", task.city, task.query_label, err)
                        async with results_lock:
                            results.append(CellResult(task=task, error=err))
                        async with done_lock:
                            done += 1
                            self._emit(
                                type="cell_error",
                                worker=worker_id,
                                done=done, total=total,
                                city=task.city, query=task.query_label,
                                error=err,
                            )
                        # Карантиним прокси и заставляем новый контекст
                        if current_proxy:
                            await self.pm.quarantine(current_proxy)
                        if context:
                            try:
                                await context.close()
                            except Exception:
                                pass
                            context = None
                            current_proxy = None
            finally:
                if context:
                    try:
                        await context.close()
                    except Exception:
                        pass

        await asyncio.gather(*[worker(i) for i in range(self.pool_size)])
        return results


# ── Синхронная обёртка для вызова из существующего кода ───────────────────────

def scrape_via_browser(
    tasks: list[CellTask],
    *,
    pool_size: int = DEFAULT_POOL_SIZE,
    progress_cb: Optional[Callable[[dict], None]] = None,
) -> list[CellResult]:
    """
    Синхронная обёртка: создаёт event-loop, гоняет пул, возвращает результаты.
    Используй из синхронного кода (CLI, существующий scrape_vacancies_geo).
    Из async-кода — используй BrowserPool напрямую.
    """
    async def _run() -> list[CellResult]:
        async with BrowserPool(pool_size=pool_size, progress_cb=progress_cb) as pool:
            return await pool.fetch_many(tasks)

    return asyncio.run(_run())
