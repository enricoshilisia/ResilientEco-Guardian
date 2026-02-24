#!/usr/bin/env bash

set -e

echo "Current directory: $(pwd)"

# Oryx extracts antenv to /antenv
if [ -f "/antenv/bin/activate" ]; then
    source /antenv/bin/activate
    echo "Activated /antenv"
elif [ -f "antenv/bin/activate" ]; then
    source antenv/bin/activate
    echo "Activated ./antenv"
else
    echo "ERROR: No antenv found!"
    exit 1
fi

echo "Running Django commands..."
export DJANGO_SETTINGS_MODULE=resilienteco.settings
python manage.py migrate --noinput || echo "Migrations failed (non-fatal)"
python manage.py collectstatic --noinput --clear || echo "collectstatic failed (non-fatal)"

echo "Starting Uvicorn on port ${PORT:-8000}..."
exec uvicorn resilienteco.asgi:application \
    --host 0.0.0.0 \
    --port "${PORT:-8000}" \
    --workers 2