<h1 align="center">
    <br>
    HH-Parser
    <br>
</h1>

<p align="center">
    <b>Собирай базу компаний с HeadHunter по индустриям — через браузер или десктоп</b>
</p>

<p align="center">
    <img src="https://img.shields.io/badge/python-3.12-blue?style=flat-square&logo=python&logoColor=white" alt="Python">
    <img src="https://img.shields.io/badge/FastAPI-0.115-009688?style=flat-square&logo=fastapi&logoColor=white" alt="FastAPI">
    <img src="https://img.shields.io/badge/Flet-0.28.3-02569B?style=flat-square&logo=flutter&logoColor=white" alt="Flet">
    <img src="https://img.shields.io/badge/license-MIT-green?style=flat-square" alt="License">
</p>

---

## Что это

HH-Parser — инструмент для массового сбора компаний с [hh.ru](https://hh.ru) по индустриям. Выбираешь нужные отрасли, задаёшь фильтры — получаешь базу компаний с количеством вакансий в CSV или JSON.

**Три способа использования:**

| Способ | Описание |
|--------|----------|
| **Web** | Красивый лендинг с real-time логом парсинга (FastAPI + SSE) |
| **Desktop GUI** | Кроссплатформенное приложение на Flet (Windows + macOS) |
| **CLI** | Интерактивный терминал для быстрого запуска |

## Возможности

- Парсинг компаний по выбранным индустриям с hh.ru
- Фильтрация: минимум вакансий, регион, только с открытыми вакансиями
- Экспорт в CSV и JSON
- Real-time лог парсинга в веб-версии (SSE)
- Автоматический retry при блокировках HH (403/429)
- Дедупликация компаний по URL
- Кэширование списка индустрий

---

## Быстрый старт

### Установка

```bash
git clone https://github.com/VickentyZherebov/hh-parser.git
cd hh-parser
python -m venv .venv
source .venv/bin/activate  # Windows: .venv\Scripts\activate
```

### Web-версия

```bash
pip install -r web/requirements.txt
uvicorn web.app:app --host 127.0.0.1 --port 8000
```

Открой [http://127.0.0.1:8000](http://127.0.0.1:8000) в браузере.

### Desktop GUI

```bash
pip install -r requirements.txt
python main.py
```

### CLI

```bash
pip install -r requirements.txt
python hh_companies_by_industry.py
```

---

## Деплой на Amvera

Проект готов к деплою на [Amvera](https://amvera.ru) — есть `Dockerfile` и `amvera.yml`.

```bash
git remote add amvera https://git.amvera.ru/<логин>/<проект>.git
git push amvera main
```

Через пару минут сайт будет доступен по адресу `https://<проект>.amvera.io`.

---

## Тесты

```bash
pip install pytest
pytest tests/ -v
```

## Сборка standalone

```bash
pip install pyinstaller
pyinstaller --onefile --noconsole --name HH-Parser main.py
```

Готовый файл появится в `dist/`.

---

## Структура проекта

```
main.py                     — Desktop GUI (Flet)
hh_companies_by_industry.py — Ядро парсинга + CLI
web/
  app.py                    — FastAPI-бэкенд (API + SSE-стриминг прогресса)
  templates/index.html      — Лендинг
  static/style.css          — Стили (тёмная тема)
  requirements.txt          — Зависимости веб-версии
tests/
  test_parsing.py           — Юнит-тесты парсинга
Dockerfile                  — Контейнер для деплоя
amvera.yml                  — Конфиг Amvera
installer/hh-parser.iss     — Windows-инсталлятор (Inno Setup)
assets/                     — Иконки приложения
```

## Технологии

| Компонент | Технология |
|-----------|-----------|
| Ядро парсинга | Python, requests, BeautifulSoup4, lxml |
| Web | FastAPI, Jinja2, SSE (Server-Sent Events) |
| Desktop GUI | Flet 0.28.3 |
| База | PostgreSQL-ready (экспорт в CSV/JSON) |
| CI/CD | GitHub Actions, PyInstaller, Inno Setup |
| Деплой | Docker, Amvera |
