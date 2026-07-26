#!/usr/bin/env sh
set -e

# Миграции — на СТАРТЕ контейнера, а не в билде: на Render билд не достаёт до БД.
python manage.py migrate --noinput

# $PORT задаёт Render (иначе health check падает); Dokploy его не ставит → 8000.
# Shell-форма (не exec-JSON) обязательна, чтобы ${PORT} действительно раскрылся.
# collectstatic здесь НЕ вызываем — он уже сделан на этапе сборки.
# seed НЕ вызываем — демо-данные не должны создаваться на каждый деплой.
exec gunicorn ihelper.wsgi \
    --bind "0.0.0.0:${PORT:-8000}" \
    --workers 3 \
    --timeout 60 \
    --access-logfile -
