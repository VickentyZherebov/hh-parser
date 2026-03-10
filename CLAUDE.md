# HH-Parser

## Что делает проект

Инструмент для массового сбора базы компаний с HeadHunter (hh.ru) по индустриям. Три интерфейса:

- **Web** — FastAPI + лендинг с real-time логом парсинга (SSE). Задеплоен на Amvera: https://hh-parser.vickenty.amvera.io
- **Desktop GUI** — Flet-приложение (Windows + macOS)
- **CLI** — интерактивный терминал (`hh_companies_by_industry.py`)

Пользователь выбирает индустрии, настраивает фильтры (минимум вакансий, регион, только с открытыми вакансиями), парсер обходит все страницы и собирает компании. Результат — CSV и/или JSON.

## Стек и зависимости

- **Python 3.12**
- **FastAPI + Jinja2 + uvicorn** — веб-версия (`web/requirements.txt`)
- **Flet 0.28.3** — десктоп GUI (`requirements.txt`)
- **requests + BeautifulSoup4 + lxml** — HTTP и парсинг HTML
- **pytest** — юнит-тесты
- **PyInstaller** — сборка standalone
- **Inno Setup** — Windows-инсталлятор
- **Docker + Amvera** — деплой

## Версия

Единый источник: `__version__` в `hh_companies_by_industry.py`. Импортируется в `main.py` и `web/app.py`, отображается в UI. При релизе также обновить `installer/hh-parser.iss`.

## Структура проекта

```
hh_companies_by_industry.py    — Ядро парсинга + CLI. Модели (Industry, Company), HTTP с retry, парсинг, пагинация
main.py                        — Desktop GUI (Flet). UI, состояние, вызов парсинга и экспорта
web/
  app.py                       — FastAPI-бэкенд: API + SSE-стриминг прогресса
  templates/index.html         — Лендинг с гайдом, фильтрами, логом, таблицей результатов
  static/style.css             — Стили (тёмная тема, адаптив)
  requirements.txt             — Зависимости веб-версии
tests/
  test_parsing.py              — 18 юнит-тестов на функции парсинга
requirements.txt               — Зависимости десктоп-версии
Dockerfile                     — Контейнер для деплоя
amvera.yaml                    — Конфиг Amvera
.github/workflows/build.yml    — GitHub Actions: сборка PyInstaller (Windows/macOS) + Inno Setup
HH-Parser.spec                 — PyInstaller-спецификация для macOS .app
installer/hh-parser.iss        — Inno Setup скрипт для Windows
assets/                        — Иконки (icon.png, icon.ico, icon.icns)
DEVELOPMENT.md                 — Описание этапов разработки и архитектуры
```

## Запуск

```bash
# Web
pip install -r web/requirements.txt
uvicorn web.app:app --host 127.0.0.1 --port 8000

# Desktop GUI
pip install -r requirements.txt
python main.py

# CLI
python hh_companies_by_industry.py

# Тесты
pip install pytest
pytest tests/ -v
```

## Деплой

Amvera (Docker), два remote:
```bash
git push origin main                # GitHub
git push amvera main:master         # Amvera (ветка master!)
```

## Соглашения по коду

- **Язык**: весь UI-текст, комментарии, докстринги — на **русском**
- **Модели данных**: `@dataclass` для всех структур. Сериализация через `dataclasses.asdict()`
- **Типизация**: `list[T]`, `dict[K, V]`, `Optional[T]`, `Callable` — Python 3.10+ синтаксис
- **GUI без ООП**: одна функция `main(page)` с замыканиями
- **Web**: FastAPI + SSE для стриминга прогресса, кэш индустрий в памяти
- **Многопоточность**: парсинг в daemon-потоке (GUI) или `asyncio.to_thread` (Web)
- **Колбэки прогресса**: `scrape_companies()` принимает `progress_cb` для отчёта в UI
- **Дедупликация**: по URL через `dict[str, Company]`
- **Rate limiting**: `time.sleep(0.35)` между запросами к hh.ru
- **Retry**: `fetch_html()` — до 3 попыток с экспоненциальной задержкой, `HHBlockedError` при 403/429
- **Экспорт**: вынесен в `export_results()` в `main.py`

## Важные правила

- **Не коммитить** JSON-файлы, `.env`, `.vscode/` — в gitignore
- **Русский язык** во всех пользовательских строках и комментариях
- **Не уменьшать `time.sleep()`** между запросами — hh.ru блокирует
- **Три точки входа**: `main.py` (GUI), `hh_companies_by_industry.py` (CLI), `web/app.py` (Web) — все должны работать
- **Flet зафиксирован** на 0.28.3 — не обновлять без тестирования
- **Amvera ожидает ветку `master`** — пушить `git push amvera main:master`
- **amvera.yaml** (не .yml!) — Amvera не принимает .yml
- **Тесты**: при изменении парсинга — `pytest tests/ -v` и добавлять новые тесты
- **Bundle identifier**: `pro.jobru.hhparser`
