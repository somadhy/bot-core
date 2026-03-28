FROM python:3.12-slim

WORKDIR /app

RUN apt-get update && apt-get install -y --no-install-recommends \
    gcc \
    && rm -rf /var/lib/apt/lists/*

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY meshcore_bot ./meshcore_bot

ENV MESHCORE_BOT_CONFIG=/app/config.yaml
ENV PYTHONUNBUFFERED=1

CMD ["python", "-m", "meshcore_bot"]
