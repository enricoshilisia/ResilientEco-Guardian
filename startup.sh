#!/bin/bash
cd /home/site/wwwroot
python -m venv antenv
source antenv/bin/activate
pip install --pre -r requirements.txt
python manage.py migrate --noinput
uvicorn resilienteco.asgi:application --host 0.0.0.0 --port 8000 --workers 4