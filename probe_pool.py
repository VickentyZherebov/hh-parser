"""
Тест пула BrowserPool на 5 разных ячейках с одновременным
запуском нескольких контекстов через разные прокси.

Запуск:
    .venv/bin/python probe_pool.py
"""

import time

from hh_browser_fetcher import CellTask, scrape_via_browser

# 5 ячеек в одном Саранске — небольшие выдачи (1-3 страницы каждая),
# тест должен пройти за ~1 минуту. Все 5 идут параллельно через разные прокси.
TASKS = [
    CellTask(city="Саранск", area_id=63, text="менеджер"),
    CellTask(city="Саранск", area_id=63, text="бухгалтер"),
    CellTask(city="Саранск", area_id=63, text="водитель"),
    CellTask(city="Саранск", area_id=63, text="продавец"),
    CellTask(city="Саранск", area_id=63, text="инженер"),
]


def cb(event: dict) -> None:
    et = event.get("type")
    if et == "context_open":
        print(f"  🔌 worker={event['worker']} взял прокси {event['proxy']}")
    elif et == "cell_done":
        print(
            f"  ✓ {event['done']}/{event['total']} "
            f"[w{event['worker']}] {event['city']} × «{event['query']}»: "
            f"{event['total_vacancies']} вак, {event['unique_employers']} комп "
            f"({event['duration_s']}с)"
        )
    elif et == "cell_error":
        print(
            f"  ✗ {event['done']}/{event['total']} "
            f"[w{event['worker']}] {event['city']} × «{event['query']}»: {event['error']}"
        )


def main() -> None:
    print(f"📋 Задач: {len(TASKS)}, пул: 5 (= размер задач для максимума параллели)")
    print()

    t0 = time.time()
    results = scrape_via_browser(TASKS, pool_size=5, progress_cb=cb)
    elapsed = time.time() - t0

    print()
    print("═" * 70)
    print(f"⏱  Всего: {elapsed:.1f}с  (среднее: {elapsed / len(TASKS):.1f}с/ячейка)")
    print("═" * 70)
    for r in results:
        if r.error:
            print(f"❌ {r.task.city} × «{r.task.query_label}»: {r.error}")
        else:
            print(
                f"✅ {r.task.city:20s} × «{r.task.query_label:25s}» — "
                f"{r.total_vacancies:>6d} вак / {r.unique_employers:>4d} комп  "
                f"({r.pages_processed} стр, {r.duration_s}с)"
            )


if __name__ == "__main__":
    main()
