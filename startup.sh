#!/usr/bin/env bash

set -e

echo "Current directory: $(pwd)"
echo "PORT is: ${PORT}"

if [ -f "/antenv/bin/activate" ]; then
    source /antenv/bin/activate
fi

export DJANGO_SETTINGS_MODULE=resilienteco.settings

python manage.py migrate --noinput || echo "Migrations failed (non-fatal)"
python manage.py collectstatic --noinput --clear || echo "collectstatic failed (non-fatal)"

echo "Starting Uvicorn on port ${PORT}..."
exec /antenv/bin/uvicorn resilienteco.asgi:application \
    --host 0.0.0.0 \
    --port "${PORT}" \
    --workers 2