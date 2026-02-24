#!/usr/bin/env bash

set -e  # exit on error

cd /home/site/wwwroot || { echo "Cannot cd to wwwroot"; exit 1; }

echo "Checking virtual environment antenv..."

# If venv is missing, incomplete, or doesn't have Django → rebuild it
if [ ! -d "antenv/bin" ] || [ ! -f "antenv/bin/pip" ] || [ ! -d "antenv/lib/python3.12/site-packages/django" ]; then
    echo "Rebuilding antenv (empty or missing Django)..."
    rm -rf antenv
    python3 -m venv antenv
fi

# Activate venv
source antenv/bin/activate || { echo "Failed to activate antenv"; exit 1; }

echo "Upgrading pip..."
pip install --upgrade pip --no-cache-dir

echo "Installing dependencies..."
pip install --no-cache-dir --pre -r requirements.txt || { echo "pip install failed - check requirements.txt"; exit 1; }

echo "Verifying Django installation..."
python -c "import django; print('Django version:', django.__version__)" || { echo "Django not installed"; exit 1; }

echo "Running Django commands..."
export DJANGO_SETTINGS_MODULE=resilienteco.settings
python manage.py migrate --noinput || echo "Migrations failed (non-fatal)"
python manage.py collectstatic --noinput --clear || echo "collectstatic failed (non-fatal)"

echo "Starting Uvicorn..."
exec uvicorn resilienteco.asgi:application \
    --host 0.0.0.0 \
    --port "$PORT" \
    --workers 4