FROM python:3.12-slim

WORKDIR /app

# Python-зависимости (включая playwright-пакет; сам Chromium НЕ ставим — см. ниже)
COPY web/requirements.txt requirements.txt
RUN pip install --no-cache-dir --root-user-action=ignore -r requirements.txt

# Chromium на ome НЕ устанавливаем намеренно: прямое скачивание с Google CDN
# режется с РФ-сети, а официальный образ Playwright (~2 ГБ) на текущем WiFi-линке
# ome (~1.6 Мбит/с) тянется часами. Вкладки «Индекс ХХ»/«Зарплаты» (stats.hh.ru)
# браузер не используют. Вкладки «Гео»/«Компании» пока живут на Amvera; Chromium
# на ome добавим отдельным слоем (через прокси), когда линк будет здоров.

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
