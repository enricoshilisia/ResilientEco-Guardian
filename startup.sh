python manage.py migrate --noinput
python manage.py collectstatic --noinput
uvicorn resilienteco.asgi:application --host 0.0.0.0 --port 8000 --workers 4