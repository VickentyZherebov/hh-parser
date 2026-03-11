FROM python:3.12-slim

WORKDIR /app

# Зависимости
COPY web/requirements.txt requirements.txt
RUN pip install --no-cache-dir --root-user-action=ignore -r requirements.txt

# Код
COPY hh_companies_by_industry.py .
COPY web/ web/

EXPOSE 80

CMD ["uvicorn", "web.app:app", "--host", "0.0.0.0", "--port", "80"]
