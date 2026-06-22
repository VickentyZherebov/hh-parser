"""
Прогон пресета Маши «Профессии НН 2026» на НОВОМ списке городов (ITD-1245, июнь 2026).

Профессии = тот же пресет (10 ролей + текст «Тракторист»).
Гео = 43 города из таблицы Маши (вкладка «Гео»).
Метрика = кол-во вакансий + уникальных работодателей по «город × профессия».

Локальный прогон через браузерный пул (Amvera падает на /dev/shm).
Результат — Excel в data/.
"""

import time
from datetime import datetime
from pathlib import Path

from hh_vacancies_by_geo import export_to_xlsx, resolve_city_ids, scrape_vacancies_geo

CITY_NAMES = [
    "Архангельск", "Астрахань", "Барнаул", "Белгород", "Брянск", "Владивосток",
    "Волгоград", "Воронеж", "Грозный", "Екатеринбург", "Иваново", "Ижевск",
    "Иркутск", "Казань", "Калининград", "Кемерово", "Краснодар", "Красноярск",
    "Курган", "Курск", "Липецк", "Магнитогорск", "Махачкала", "Москва",
    "Набережные Челны", "Нижний Тагил", "Новосибирск", "Омск", "Оренбург",
    "Ростов-на-Дону", "Самара", "Санкт-Петербург", "Симферополь", "Ставрополь",
    "Сургут", "Томск", "Тюмень", "Улан-Удэ", "Хабаровск", "Чебоксары",
    "Челябинск", "Чита", "Якутск",
]
# Слесарь115, Упаковщик131, Водитель21, Грузчик31, Разнорабочий102,
# Продавец97, Сварщик109, Директор маг35, Администратор маг9, Электромонтажник143
ROLE_IDS = [9, 21, 31, 35, 97, 102, 109, 115, 131, 143]
SEARCH_TEXTS = ["Тракторист"]
POOL_SIZE = 10


def main() -> None:
    print(f"📋 Профессии НН 2026 · новый гео-список")
    print(f"   {len(CITY_NAMES)} городов × ({len(ROLE_IDS)} ролей + {len(SEARCH_TEXTS)} текст)")
    city_ids = resolve_city_ids(CITY_NAMES)
    print(f"   резолв: {len(city_ids)}/{len(CITY_NAMES)}")
    not_found = [c for c in CITY_NAMES if c not in city_ids]
    if not_found:
        print(f"   не найдены: {not_found}")
    print(f"   ячеек: {len(city_ids) * (len(ROLE_IDS) + len(SEARCH_TEXTS))}, пул {POOL_SIZE}")

    t0 = time.time()
    result = scrape_vacancies_geo(
        city_ids=city_ids,
        role_ids=ROLE_IDS,
        search_texts=SEARCH_TEXTS,
        progress_cb=lambda e: print("  " + e.get("status_text", ""), flush=True),
        pool_size=POOL_SIZE,
    )
    elapsed = time.time() - t0

    stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    out_path = Path(__file__).parent / "data" / f"masha_geo_prof_{stamp}.xlsx"
    out_path.parent.mkdir(parents=True, exist_ok=True)
    export_to_xlsx(result, str(out_path))
    print("=" * 60)
    print(f"⏱ Готово за {elapsed/60:.1f} мин")
    print(f"   Excel: {out_path}")
    print(f"RESULT_FILE={out_path}")


if __name__ == "__main__":
    main()
