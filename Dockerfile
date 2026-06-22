# Официальный образ Playwright: Chromium + системные зависимости уже внутри
# (/ms-playwright), версия тега = версии pip-пакета playwright (1.59.0). Так не
# качаем Chromium с Google CDN при сборке (режется с РФ); mcr.microsoft.com
# доступен. Образ ~2 ГБ — тянется один раз, кэшируется на ome.
FROM mcr.microsoft.com/playwright/python:v1.59.0-noble

WORKDIR /app

# Python-зависимости (playwright==1.59.0 уже в образе под bundled-браузеры)
COPY web/requirements.txt requirements.txt
RUN pip install --no-cache-dir --root-user-action=ignore -r requirements.txt

# Код
COPY hh_companies_by_industry.py .
COPY hh_vacancies_by_geo.py .
COPY hh_browser_fetcher.py .
COPY hh_market_stats.py .
COPY hh_market_index.py .
COPY hh_market_salary.py .
COPY manage.py .
COPY web/ web/

EXPOSE 80

CMD ["uvicorn", "web.app:app", "--host", "0.0.0.0", "--port", "80"]
