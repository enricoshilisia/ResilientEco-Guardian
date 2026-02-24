#!/usr/bin/env bash

set -e

echo "Current directory: $(pwd)"
echo "Looking for antenv..."

# Oryx extracts antenv to /antenv at runtime
if [ -f "/antenv/bin/activate" ]; then
    source /antenv/bin/activate
    echo "Activated /antenv"
elif [ -f "/home/site/wwwroot/antenv/bin/activate" ]; then
    source /home/site/wwwroot/antenv/bin/activate
    echo "Activated /home/site/wwwroot/antenv"
else
    echo "ERROR: No antenv found at /antenv or /home/site/wwwroot/antenv"
    echo "Listing /antenv: $(ls /antenv 2>/dev/null || echo 'does not exist')"
    exit 1
fi

echo "Python: $(which python) $(python --version)"

echo "Running Django commands..."
export DJANGO_SETTINGS_MODULE=resilienteco.settings
python manage.py migrate --noinput || echo "Migrations failed (non-fatal)"
python manage.py collectstatic --noinput --clear || echo "collectstatic failed (non-fatal)"

echo "Starting Uvicorn on port ${PORT}..."
exec uvicorn resilienteco.asgi:application \
    --host 0.0.0.0 \
    --port "${PORT}" \
    --workers 2