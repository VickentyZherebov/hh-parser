FROM python:3.12-slim

WORKDIR /app

# Python-зависимости
COPY web/requirements.txt requirements.txt
RUN pip install --no-cache-dir --root-user-action=ignore -r requirements.txt

# Системные зависимости Chromium + сам Chromium для Playwright
# install-deps подтягивает libnss, libdrm, libxkbcommon и пр. (~150 МБ)
# install chromium — сам бинарник (~170 МБ). Итого образ растёт примерно на 320 МБ.
RUN playwright install-deps chromium && playwright install chromium

# Код
COPY hh_companies_by_industry.py .
COPY hh_vacancies_by_geo.py .
COPY hh_browser_fetcher.py .
COPY manage.py .
COPY web/ web/

EXPOSE 80

CMD ["uvicorn", "web.app:app", "--host", "0.0.0.0", "--port", "80"]
