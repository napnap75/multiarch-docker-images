FROM python:3.14-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    APP_HOME=/app \
    PORT=8000

WORKDIR ${APP_HOME}

COPY . /app

RUN python -m pip install --upgrade pip && \
    python -m pip install --no-cache-dir \
        fastapi \
        uvicorn \
        boto3 \
        pymupdf \
        python-dateutil \
        PyYAML

EXPOSE 8000

CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000"]
