FROM python:3.12-slim

ARG PIP_INDEX_URL=https://pypi.tuna.tsinghua.edu.cn/simple
ARG PIP_DEFAULT_TIMEOUT=120
ARG PIP_RETRIES=10

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PYTHONPATH=/app/backend \
    PIP_INDEX_URL=${PIP_INDEX_URL} \
    PIP_DEFAULT_TIMEOUT=${PIP_DEFAULT_TIMEOUT} \
    PIP_RETRIES=${PIP_RETRIES}

WORKDIR /app

COPY backend/requirements.txt /app/backend/requirements.txt
RUN pip install --no-cache-dir -r /app/backend/requirements.txt

COPY backend /app/backend
COPY migrations /app/migrations
COPY alembic.ini /app/alembic.ini
COPY scripts/container-start.sh /app/scripts/container-start.sh
COPY scripts/migrate.py /app/scripts/migrate.py
COPY VERSION /app/VERSION

RUN chmod +x /app/scripts/container-start.sh

WORKDIR /app/backend
EXPOSE 8010

CMD ["/app/scripts/container-start.sh"]
