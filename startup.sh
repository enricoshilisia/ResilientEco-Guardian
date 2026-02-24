#!/usr/bin/env bash

set -e

echo "Current directory: $(pwd)"
echo "PORT is: ${PORT}"

export DJANGO_SETTINGS_MODULE=resilienteco.settings

echo "Running Django commands..."
python manage.py migrate --noinput || echo "Migrations failed (non-fatal)"
python manage.py collectstatic --noinput --clear || echo "collectstatic failed (non-fatal)"

echo "Starting Uvicorn on port ${PORT}..."
exec uvicorn resilienteco.asgi:application \
    --host 0.0.0.0 \
    --port "${PORT}" \
    --workers 2