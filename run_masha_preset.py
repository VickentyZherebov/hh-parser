"""
Локальный прогон пресета Маши «Профессии НН 2026»: 35 городов × (10 ролей + 2 текста).

Зачем: на Amvera 04.05 контейнер падал на больших прогонах (Page crashed / Timeout) —
нужны Docker-aware Chromium флаги (--disable-dev-shm-usage). Пока чиним инфру —
прогоняем локально (RAM/CPU мака с запасом), отдаём Машу Excel из TG.

Источник конфига: GET /api/presets с Amvera (логин masha, seed-пароль).
Сохранён в /tmp/masha_presets.json.
"""

import time
from datetime import datetime
from pathlib import Path

from hh_vacancies_by_geo import (
    export_to_xlsx,
    resolve_city_ids,
    scrape_vacancies_geo,
)

# Конфиг из пресета (id=2 «Профессии НН 2026», 35 городов / 10 ролей / 2 текста)
CITY_NAMES = [
    "Архангельск", "Белгород", "Брянск", "Владивосток", "Воронеж",
    "Елец", "Кемерово", "Комсомольск-на-Амуре", "Курск", "Липецк",
    "Махачкала", "Междуреченск", "Назрань", "Находка", "Новокузнецк",
    "Омск", "Оренбург", "Орск", "Петропавловск-Камчатский", "Прокопьевск",
    "Севастополь", "Северодвинск", "Симферополь", "Старый Оскол", "Томск",
    "Улан-Удэ", "Уссурийск", "Хабаровск", "Черкесск", "Чита",
    "Южно-Сахалинск", "Якутск", "Горно-Алтайск", "Абакан", "Сургут",
]
ROLE_IDS = [9, 21, 31, 35, 97, 102, 109, 115, 131, 143]
SEARCH_TEXTS = ["Тракторист", "монтер пути"]
POOL_SIZE = 10


def progress_cb(event: dict) -> None:
    print(f"  {event.get('status_text', '')}")


def main() -> None:
    print(f"📋 Пресет «Профессии НН 2026»")
    print(f"   {len(CITY_NAMES)} городов × ({len(ROLE_IDS)} ролей + {len(SEARCH_TEXTS)} текстов)")
    print(f"   = {len(CITY_NAMES) * (len(ROLE_IDS) + len(SEARCH_TEXTS))} ячеек")
    print(f"   Пул: {POOL_SIZE} контекстов")
    print()

    print("⏳ Резолв area_id городов...")
    city_ids = resolve_city_ids(CITY_NAMES)
    print(f"   найдено {len(city_ids)}/{len(CITY_NAMES)}")
    if len(city_ids) < len(CITY_NAMES):
        not_found = set(CITY_NAMES) - set(city_ids)
        print(f"   не найдены: {not_found}")
    print()

    t0 = time.time()
    result = scrape_vacancies_geo(
        city_ids=city_ids,
        role_ids=ROLE_IDS,
        search_texts=SEARCH_TEXTS,
        progress_cb=progress_cb,
        pool_size=POOL_SIZE,
    )
    elapsed = time.time() - t0

    print()
    print("=" * 70)
    print(f"⏱  Готово за {elapsed/60:.1f} мин ({elapsed:.0f} сек)")
    print(f"   Roles: {len(result['roles'])}, Texts: {len(result['texts'])}")

    # Сохранение Excel
    stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    out_path = Path(__file__).parent / "data" / f"masha_preset_{stamp}.xlsx"
    out_path.parent.mkdir(parents=True, exist_ok=True)
    export_to_xlsx(result, str(out_path))
    print(f"   Excel: {out_path}")
    print(f"   Размер: {out_path.stat().st_size / 1024:.1f} KB")


if __name__ == "__main__":
    main()
