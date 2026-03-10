# HH-Parser

## Что делает проект

Десктопное GUI-приложение для сбора базы компаний с HeadHunter (hh.ru) по индустриям. Пользователь выбирает индустрии из автоматически загружаемого каталога, настраивает фильтры (минимум вакансий, регион, только с открытыми вакансиями), после чего приложение обходит все страницы пагинации и собирает названия компаний, ссылки и количество вакансий. Результат экспортируется в CSV и/или JSON.

Также есть CLI-режим в `hh_companies_by_industry.py` — делает то же самое через интерактивный терминал.

## Стек и зависимости

- **Python 3.12** (CI собирает на 3.12; локальный venv может отличаться)
- **Flet 0.28.3** — кроссплатформенный GUI-фреймворк (на базе Flutter)
- **requests** — HTTP-клиент для запросов к hh.ru
- **BeautifulSoup4 + lxml** — парсинг HTML
- **PyInstaller** — сборка в standalone-исполняемые файлы
- **Inno Setup** — Windows-инсталлятор (скрипт `installer/hh-parser.iss`)
- **pytest** — юнит-тесты

Зависимости описаны в `requirements.txt` (pyproject.toml нет).

## Версия

Единый источник версии: `__version__` в `hh_companies_by_industry.py`. Импортируется в `main.py` и отображается в заголовке окна. При релизе также обновить `installer/hh-parser.iss`.

## Структура проекта

```
main.py                        — Точка входа GUI (Flet). UI, состояние, вызов парсинга и экспорта
hh_companies_by_industry.py    — Ядро парсинга + CLI-точка входа. Модели данных (Industry, Company), HTTP с retry, парсинг HTML, пагинация
requirements.txt               — Зависимости Python
README.md                      — Краткая инструкция по запуску
HH-Parser.spec                 — PyInstaller-спецификация для сборки macOS .app
.github/workflows/build.yml    — GitHub Actions CI: сборка PyInstaller для Windows/macOS + Inno Setup инсталлятор
assets/                        — Иконки приложения (icon.png, icon.ico, icon.icns)
installer/hh-parser.iss        — Скрипт Inno Setup для Windows-инсталлятора
build/                         — Артефакты сборки PyInstaller
dist/                          — Выходные файлы PyInstaller (исполняемые файлы, .app, .zip)
tests/                         — Юнит-тесты (pytest)
  test_parsing.py              — Тесты парсинга: with_query, extract_max_page, parse_industries, parse_companies_from_industry_page
.gitignore                     — Игнорирует .venv, __pycache__, .env, scrapped_data/, *.json, .vscode/, .pytest_cache/
```

## Запуск тестов

```bash
.venv/bin/python -m pytest tests/ -v
```

## Соглашения по коду

- **Язык**: весь UI-текст, комментарии, докстринги — на **русском языке**. Сохраняй эту конвенцию.
- **Модели данных**: `@dataclass` для всех структур (`Industry`, `Company`, `AppState`). Сериализация через `dataclasses.asdict()`.
- **Типизация**: используется везде — `list[T]`, `dict[K, V]`, `Optional[T]`, `Callable`. Синтаксис Python 3.10+ (lowercase generics).
- **Без классов для логики приложения**: GUI — одна функция `main(page)` с вложенными хелперами и замыканиями, без ООП для UI-слоя.
- **Многопоточность**: долгий парсинг запускается в daemon-потоке `threading.Thread`, чтобы Flet UI не зависал.
- **Колбэки прогресса**: `scrape_companies()` принимает `progress_cb: Optional[Callable[[dict], None]]` для отчёта о прогрессе в UI.
- **Дедупликация**: компании дедуплицируются по URL через `dict[str, Company]`.
- **Ограничение частоты запросов**: `time.sleep(0.35)` между запросами страниц, чтобы не перегружать hh.ru.
- **Retry с экспоненциальной задержкой**: `fetch_html()` автоматически повторяет запросы при ошибках и блокировках (403/429), до `MAX_RETRIES` попыток.
- **Обработка ошибок**: исключения всплывают и ловятся на верхнем уровне в потоке. `HHBlockedError` бросается при блокировке со стороны HH. Колбэки прогресса обёрнуты в try/except.
- **Экспорт**: вынесен в отдельную функцию `export_results()` в `main.py`.

## Важные правила

- **Не коммитить JSON-файлы с результатами** — они в gitignore (`*.json`)
- **Не коммитить `.env` и `.vscode/`** — в gitignore
- **Сохранять русский язык** во всех пользовательских строках, комментариях и докстрингах
- **Соблюдать лимиты запросов к hh.ru** — не убирать и не уменьшать `time.sleep()` между запросами
- **Bundle identifier**: `pro.jobru.hhparser` (задан в HH-Parser.spec)
- **Две точки входа**: `main.py` (GUI) и `hh_companies_by_industry.py` (CLI) — обе должны работать
- **Версия Flet зафиксирована** на 0.28.3 — API Flet сильно меняется между версиями, не обновлять без тестирования
- **Экспорт по умолчанию на Рабочий стол** — приложение автоматически определяет путь к Desktop, включая русскую локаль (`Рабочий стол`) и варианты OneDrive
- **Тесты**: при изменении парсинга — запускать `pytest tests/ -v` и добавлять новые тесты
