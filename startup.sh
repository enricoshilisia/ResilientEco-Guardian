#!/usr/bin/env bash

set -e

echo "Current directory: $(pwd)"
echo "Looking for antenv..."

# Activate whichever antenv Oryx found (it sets PYTHONPATH already)
# Just install deps and run — Oryx handles the venv path
if [ -f "antenv/bin/activate" ]; then
    source antenv/bin/activate
elif [ -f "/home/site/wwwroot/antenv/bin/activate" ]; then
    source /home/site/wwwroot/antenv/bin/activate
else
    echo "No antenv found, installing to system pip..."
fi

echo "Installing dependencies..."
pip install --no-cache-dir --pre -r requirements.txt || echo "pip install failed"

echo "Running Django commands..."
export DJANGO_SETTINGS_MODULE=resilienteco.settings
python manage.py migrate --noinput || echo "Migrations failed (non-fatal)"
python manage.py collectstatic --noinput --clear || echo "collectstatic failed (non-fatal)"

echo "Starting Uvicorn..."
exec uvicorn resilienteco.asgi:application \
    --host 0.0.0.0 \
    --port "${PORT:-8000}" \
    --workers 4