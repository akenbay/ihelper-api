# --- build stage: колёса собираем отдельно, чтобы в финальный образ не тащить компиляторы ---
FROM python:3.12-slim AS builder

ENV PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1

RUN apt-get update \
    && apt-get install -y --no-install-recommends build-essential libpq-dev \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app
COPY requirements.txt .
RUN pip wheel --wheel-dir /wheels -r requirements.txt


# --- runtime stage ---
FROM python:3.12-slim AS runtime

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    DJANGO_SETTINGS_MODULE=ihelper.settings

RUN apt-get update \
    && apt-get install -y --no-install-recommends libpq5 \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

COPY --from=builder /wheels /wheels
COPY requirements.txt .
RUN pip install --no-index --find-links=/wheels -r requirements.txt && rm -rf /wheels

COPY . .
RUN chmod +x entrypoint.sh

# collectstatic на этапе сборки: в рантайме статика уже готова, отдаёт её WhiteNoise.
# SECRET_KEY здесь фиктивный и в образ не попадает как секрет — collectstatic просто
# требует, чтобы настройки импортировались.
RUN SECRET_KEY=build-only DEBUG=False python manage.py collectstatic --noinput

# Не root.
RUN useradd --create-home --uid 1000 appuser && chown -R appuser:appuser /app
USER appuser

# Render прокидывает $PORT; Dokploy — нет (entrypoint откатывается на 8000).
EXPOSE 8000

# entrypoint.sh: migrate на старте контейнера, затем gunicorn на 0.0.0.0:${PORT:-8000}.
# Shell-скрипт (а не exec-JSON gunicorn) — иначе ${PORT} не раскроется.
ENTRYPOINT ["./entrypoint.sh"]
