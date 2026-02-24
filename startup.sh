#!/usr/bin/env bash   # ← This shebang is good practice

# Activate Oryx venv (once SCM_DO_BUILD_DURING_DEPLOYMENT=true works)
. antenv/bin/activate

python manage.py migrate --noinput
python manage.py collectstatic --noinput --clear

uvicorn resilienteco.asgi:application \
    --host 0.0.0.0 \
    --port $PORT \
    --workers 4