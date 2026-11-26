FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1

WORKDIR /app

COPY requirements.txt ./
RUN pip install --no-cache-dir -r requirements.txt

COPY app ./app

EXPOSE 8000

# Defaults are overridden by docker-compose / environment.
ENV DB_HOST=mysql \
    DB_PORT=3306 \
    DB_NAME=account_audit \
    DB_USER=audit \
    DB_PASSWORD=auditpw \
    ACCOUNT_SCHEMA=account_audit \
    ACCOUNT_TABLE=account \
    ID_COLUMN=id \
    LOGIN_COLUMN=login_name \
    TARGET_COLLATION=utf8mb4_0900_ai_ci

CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000"]
