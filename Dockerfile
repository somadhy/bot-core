# Образ для сервисов `bot` и `diagnose` в docker-compose.yml (основной сценарий запуска).
FROM python:3.12-slim

# uv — установка зависимостей по lock-файлу uv.lock (воспроизводимые сборки).
COPY --from=ghcr.io/astral-sh/uv:latest /uv /uvx /bin/

WORKDIR /app

RUN apt-get update && apt-get install -y --no-install-recommends \
    gcc \
    curl \
    && rm -rf /var/lib/apt/lists/*

# Ставим зависимости до копирования исходников — не ломаем слой кеша Docker.
COPY pyproject.toml uv.lock ./
ENV UV_LINK_MODE=copy
RUN uv sync --frozen --no-dev

COPY meshcore_bot ./meshcore_bot

ENV MESHCORE_BOT_CONFIG=/app/config.yaml
ENV PYTHONUNBUFFERED=1
# python из окружения uv (.venv) для CMD и команд docker compose run.
ENV PATH="/app/.venv/bin:$PATH"

CMD ["python", "-m", "meshcore_bot"]
