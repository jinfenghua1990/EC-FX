#!/bin/sh
set -eu

cd /app
python /app/scripts/migrate.py
cd /app/backend
exec uvicorn app.main:app --host 0.0.0.0 --port "${APP_PORT:-8010}"
