#!/usr/bin/env bash
set -e

echo "Current directory: $(pwd)"
echo "PORT is: ${PORT:-8000}"

export DJANGO_SETTINGS_MODULE=resilienteco.settings

# Install if antenv missing (Azure didn't build it)
if [ ! -f "/antenv/bin/activate" ]; then
    echo "antenv not found — installing to /antenv..."
    pip install --target /antenv/lib/python3.12/site-packages -r requirements.txt
    export PYTHONPATH="/antenv/lib/python3.12/site-packages:$PYTHONPATH"
    pip install uvicorn gunicorn  # ensure these are on PATH
else
    source /antenv/bin/activate
fi

python manage.py migrate --noinput || echo "Migrations failed (non-fatal)"
python manage.py collectstatic --noinput --clear || echo "collectstatic failed (non-fatal)"

echo "Starting Uvicorn on port ${PORT:-8000}..."
exec uvicorn resilienteco.asgi:application \
    --host 0.0.0.0 \
    --port "${PORT:-8000}" \
    --workers 2