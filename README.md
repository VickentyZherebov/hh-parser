# HH-Parser

Десктопное приложение для сбора базы компаний с [HeadHunter](https://hh.ru) по индустриям.

Выбираешь индустрии, настраиваешь фильтры (минимум вакансий, регион, только с открытыми вакансиями) — приложение обходит все страницы и собирает компании с количеством вакансий. Результат экспортируется в CSV и/или JSON.

## Возможности

- GUI на базе Flet (кроссплатформенный, Windows + macOS)
- CLI-режим для работы из терминала
- Фильтрация по минимуму вакансий, региону, наличию открытых вакансий
- Экспорт в CSV и JSON
- Автоматический retry при блокировках HH (403/429)
- Дедупликация компаний по URL
- Выбор папки сохранения (по умолчанию — Рабочий стол)

## Быстрый старт

### Установка

```bash
python -m venv .venv
source .venv/bin/activate  # Windows: .venv\Scripts\activate
pip install -r requirements.txt
```

### Запуск GUI

```bash
python main.py
```

### Запуск CLI

```bash
python hh_companies_by_industry.py
```

## Тесты

```bash
pip install pytest
pytest tests/ -v
```

## Сборка

Standalone-исполняемый файл через PyInstaller:

```bash
pip install pyinstaller
pyinstaller --onefile --noconsole --name HH-Parser main.py
```

Готовый файл появится в `dist/`.

## Технологии

- Python 3.12
- [Flet](https://flet.dev) 0.28.3 — GUI
- requests + BeautifulSoup4 + lxml — HTTP и парсинг HTML
- PyInstaller — сборка в исполняемый файл
- Inno Setup — Windows-инсталлятор (`installer/hh-parser.iss`)
