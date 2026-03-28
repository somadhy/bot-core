# MeshCore BotCore

Python-бот для [MeshCore](https://meshcore.co.uk) Companion по USB: команды в выбранных каналах и в личке, погода ([Open-Meteo](https://open-meteo.com/) по умолчанию, без ключа; опционально OpenWeatherMap), help, чёрный список, остановка админом (личка).

**Основной способ запуска — Docker на Linux** с пробросом USB. Нативный Python — для разработки или если Docker на вашей ОС не подходит.

## Требования

- **Docker + Docker Compose** (Linux-хост с доступом к USB serial)
- Узел с прошивкой Companion
- По умолчанию погода через **Open-Meteo** (ключ не нужен). Для **OpenWeatherMap** задайте **`WEATHER_API_KEY`** в `.env` и в `config.yaml`: **`weather.provider: openweathermap`**

## Docker: запуск бота

```bash
cd BotCore
cp config.example.yaml config.yaml
# В config.yaml: serial.device = /dev/ttyUSB0 (как в контейнере), channels, locale, admins
# Опционально, только для OpenWeatherMap:
# echo 'WEATHER_API_KEY=your_key' >> .env
docker compose up --build
```

- USB на хосте по умолчанию монтируется как **`/dev/ttyUSB0`** внутри контейнера. Если на хосте другое имя:

  ```bash
  SERIAL_DEVICE=/dev/ttyACM0 docker compose up --build
  ```

- Файлы **`./config.yaml`** и **`./data`** монтируются с хоста.

### Docker: диагностика (Companion жив, список каналов и индексов)

Бот **не** запускается — только опрос устройства:

```bash
docker compose --profile diagnose run --rm diagnose
```

Нужны `config.yaml` и доступ к тому же USB. **`WEATHER_API_KEY` не обязателен.**

### Остановка и перезапуск контейнера

После команды **`stop`** (в личке) процесс завершается с кодом 0. При **`restart: unless-stopped`** Compose **перезапустит** контейнер. Чтобы бот остался выключенным: **`docker compose stop`** или в `docker-compose.yml` задайте **`restart: "no"`**.

---

## Локальный запуск (без Docker)

Удобно на Windows с COM-портом или для отладки.

```bash
python -m venv .venv
source .venv/bin/activate   # Windows: .venv\Scripts\activate
pip install -r requirements.txt
cp config.example.yaml config.yaml
# serial.device: COM3 (Windows) или /dev/ttyUSB0 (Linux)
# export WEATHER_API_KEY=...  # только для weather.provider: openweathermap
python -m meshcore_bot
```

Диагностика:

```bash
python -m meshcore_bot --diagnose
# или: python -m meshcore_bot -d
```

На **Windows** USB в Docker Desktop обычно недоступен так же, как на Linux; для продакшена на Windows чаще используют **локальный** `python -m meshcore_bot` или Linux/VM с Docker.

---

## Команды в эфире

Префикса нет. В канале строки часто вида `Имя: команда` — команда берётся после первого `:`.

| Команда | Описание |
|--------|----------|
| `погода` / `weather` [город] | Погода; без города — `weather.default_city` |
| `помощь` / `help` | Короткий список команд |
| `стоп` / `stop` | Остановка процесса (**только личка**, ключ в `admins.public_keys`) |

Ответы бота начинаются с `@[ник]` (в канале — из части до `:` в вашей строке).

**`стоп` в канале игнорируется** (в пакете канала нет ключа отправителя).

## Конфигурация

См. [config.example.yaml](config.example.yaml). В контейнере путь к конфигу: **`MESHCORE_BOT_CONFIG=/app/config.yaml`** (задаётся в образе).

- **`locale`**: `ru` | `en`
- **`reply_delay_sec`**: пауза в секундах перед ответом (0 — нет; максимум 600)
- **`weather.provider`**: `openmeteo` (по умолчанию) или `openweathermap` (нужен `WEATHER_API_KEY`)
- **`blacklist.path`**: JSON `{"blocked_keys": ["hex", ...]}`
- **`admins.public_keys`**: полные публичные ключи (hex) для удалённой остановки

## Лицензия

MIT (при необходимости добавьте файл LICENSE).
