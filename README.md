# MeshCore BotCore

Python-бот для [MeshCore](https://meshcore.co.uk) Companion по USB (serial): команды в выбранных каналах и в личке, погода (OpenWeatherMap), краткий help, чёрный список по ключу, остановка по команде админа (только личка).

## Требования

- Python 3.11+
- Узел с прошивкой Companion, USB
- Ключ [OpenWeatherMap](https://openweathermap.org/api) в переменной **`WEATHER_API_KEY`**

## Установка (Ubuntu / Windows)

```bash
cd BotCore
python -m venv .venv
source .venv/bin/activate   # Windows: .venv\Scripts\activate
pip install -r requirements.txt
cp config.example.yaml config.yaml
# отредактируйте config.yaml: serial.device, channels, locale, admins.public_keys
export WEATHER_API_KEY=...   # Windows: set WEATHER_API_KEY=...
python -m meshcore_bot
```

- **Ubuntu**, serial: обычно `/dev/ttyUSB0` или `/dev/ttyACM0` (права `dialout` при необходимости).
- **Windows**: порт `COM3` и т.д. в `config.yaml`.

## Диагностика (без запуска бота)

После `cp config.example.yaml config.yaml` и правки `serial.device`:

```bash
python -m meshcore_bot --diagnose
# кратко:  python -m meshcore_bot -d
```

Выводится связь с Companion, краткие **SELF_INFO** / **DEVICE_INFO**, батарея (если доступна), таблица **каналов с индексами** (`[0]`, `[1]`, …) и именами. Эти индексы нужны для `channels.enabled_indices` в конфиге. **`WEATHER_API_KEY` для диагностики не нужен.**

## Docker (Linux-хост)

```bash
cp config.example.yaml config.yaml
# задайте WEATHER_API_KEY в .env или окружении
echo 'WEATHER_API_KEY=your_key' >> .env
docker compose up --build
```

Проброс USB: переменная `SERIAL_DEVICE` (по умолчанию `/dev/ttyUSB0`). Файлы `./config.yaml` и `./data` монтируются в контейнер.

На **Windows** USB в Docker обычно проблемный; проще запускать `python -m meshcore_bot` на хосте.

## Команды (префикс по умолчанию `!`)

| Команда | Описание |
|--------|----------|
| `!погода` / `!weather` [город] | Погода; без города — `weather.default_city` |
| `!помощь` / `!help` | Короткий список команд (одно сообщение) |
| `!стоп` / `!stop` | Остановка процесса (**только личка**, отправитель должен быть в `admins.public_keys`) |

Команда **`!стоп` в канале игнорируется** (в пакете канала нет ключа отправителя, проверить админа нельзя).

## Конфигурация

См. [config.example.yaml](config.example.yaml). Путь к файлу: переменная **`MESHCORE_BOT_CONFIG`** (по умолчанию `./config.yaml`).

- **`locale`**: `ru` или `en` — язык ответов.
- **`blacklist.path`**: JSON `{"blocked_keys": ["hex", ...]}` (полный ключ или префикс 12 hex).
- **`admins.public_keys`**: полные публичные ключи в hex для удалённой остановки.

## Остановка и Docker

После админской команды процесс завершается с кодом **0**. При `restart: unless-stopped` контейнер **перезапустится**. Чтобы бот остался выключенным: `docker compose stop` или `restart: "no"`.

## Лицензия

MIT (при необходимости добавьте файл LICENSE).
