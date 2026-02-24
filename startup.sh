cd /home/site/wwwroot && \
. antenv/bin/activate && \
export DJANGO_SETTINGS_MODULE=resilienteco.settings && \
python manage.py migrate --noinput && \
python manage.py collectstatic --noinput --clear && \
uvicorn resilienteco.asgi:application --host 0.0.0.0 --port $PORT --workers 4