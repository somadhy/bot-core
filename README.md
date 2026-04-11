# MeshCore BotCore

Python-бот для [MeshCore](https://meshcore.co.uk) Companion по USB: команды в выбранных каналах и в личке, погода ([Open-Meteo](https://open-meteo.com/) по умолчанию, без ключа; опционально OpenWeatherMap), help, чёрный список, остановка админом, в личке админу — список каналов (`каналы` / `channels`) и отправка в канал от бота (`мсг` / `msg`).

**Основной способ запуска — Docker на Linux** с пробросом USB. Нативный Python — для разработки или если Docker на вашей ОС не подходит.

## Требования

- **Docker + Docker Compose** (Linux-хост с доступом к USB serial)
- Узел с прошивкой Companion
- По умолчанию погода через **Open-Meteo** (ключ не нужен).
- Для **OpenWeatherMap** задайте **`WEATHER_API_KEY`** в `.env` и в `config.yaml`: **`weather.provider: openweathermap`**
- Для **Meteostat через RapidAPI** задайте **`RAPIDAPI_KEY`** в `.env` и в `config.yaml`: **`weather.provider: meteostat_rapidapi`** (или `weather.fallback_provider: meteostat_rapidapi`)
- В `config.example.yaml` по умолчанию настроен **fallback** на **Meteostat** (`weather.fallback_provider: meteostat`) на случай ошибок у основного провайдера.
- Можно настроить **fallback-провайдера**: `weather.fallback_provider` (будет использован, если основной провайдер вернул ошибку).

## Docker: запуск бота

```bash
cd BotCore
cp config.example.yaml config.yaml
# В config.yaml: serial.device = /dev/ttyUSB0 (как в контейнере), channels, locale, admins
# Опционально, только для OpenWeatherMap:
# echo 'WEATHER_API_KEY=your_key' >> .env
# Опционально, только для meteostat_rapidapi:
# echo 'RAPIDAPI_KEY=your_key' >> .env
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
| `стоп` / `stop` | Остановка процесса: в **личке** — только если публичный ключ отправителя есть в `admins.public_keys`; в **канале** — только на индексах из `admins.channel_indices` |
| `каналы` / `channels` | Список каналов из `channels.enabled_indices`: строки вида `индекс: имя` (имя с companion). **Только личка** и **только админ** (`admins.public_keys`). Длинный ответ режется на несколько сообщений (**каждое ≤ 150 байт UTF-8**, с префиксом `@[ник] `); между частями пауза не меньше `reply_delay_sec`, а при `reply_delay_sec: 0` — не меньше ~0,35 с. В каналах команда не обрабатывается. |
| `мсг` / `msg` `индекс:текст` | Отправка в канал от лица бота (см. **`flood_ack`**: для flood — прослушивание эфира и лог репитеров). Длина текста как у обычных ответов бота в канале (~`MAX_MESSAGE_LEN` в коде). **Только личка**, **только админ**; индекс должен быть в `channels.enabled_indices`. **Первое** `:` в аргументе отделяет индекс от текста (остальные `:` — часть сообщения). Подтверждение или ошибка в ЛС — с учётом **`dm_delivery`**. В каналах не обрабатывается. |

Ответы бота начинаются с `@[ник]` (в канале — из части до `:` в вашей строке).

Дополнительные триггеры задаются в **`commands.*.aliases`** в `config.yaml` (в т.ч. `commands.channels`, `commands.msg`).

## Конфигурация

См. [config.example.yaml](config.example.yaml). В контейнере путь к конфигу: **`MESHCORE_BOT_CONFIG=/app/config.yaml`** (задаётся в образе).

- **`locale`**: `ru` | `en`
- **`reply_delay_sec`**: пауза в секундах перед ответом (0 — нет; максимум 600)
- **`flood_ack`**: после успешной отправки в канал companion возвращает `MSG_SENT` с признаком маршрута (**flood** = `type == 1`) или **`PACKET_OK`** без флага. Для **flood** (и для OK-only) бот **`interval_sec`** секунд слушает **эфир** (`RX_LOG_DATA`): ищет повторы того же группового сообщения (FLOOD / `GRP_TXT`), сопоставляет канал и текст, логирует **путь** и **последний хоп** (как правило ближайший передатчик); если префикс ключа есть в **контактах**, в лог добавляется **имя** (`adv_name`). **`interval_sec: 0`** — не слушать эфир. **`max_attempts`**: если за интервал **не было ни одного** подходящего приёма, повторная **отправка** (ещё раз слушание). Для **direct** (`type == 0`) прослушивание flood не выполняется. Нужны **расшифровка логов канала** (`set_decrypt_channel_logs`) и загрузка слотов канала (`get_channel`), иначе сопоставление может не сработать.
- **`dm_delivery`** (надёжность **личных** ответов бота): после `send_msg` бот ждёт **ACK доставки** с тем же ожидаемым кодом, не дольше **`dm_delivery.wait_sec`** секунд (по умолчанию 10). Если ACK нет — повторная отправка с тем же временем сообщения, не более **`dm_delivery.max_attempts`** попыток (по умолчанию 2). Если **`wait_sec: 0`**, ожидание ACK отключено.
- **`advert.interval_hours`**: периодический адверт узла через meshcore (`send_advert`); `0` — выключено; **`advert.flood`**: широкий адверт (зависит от прошивки)
- **`poll.keepalive_sec`**: keepalive для USB/companion при простое (по умолчанию 60; `0` — выключить). Можно переопределить `MESHCORE_BOT_KEEPALIVE_SEC`
- **`poll.keepalive_only_when_idle_sec`**: keepalive шлётся только если не было входящих сообщений ≥ N секунд (по умолчанию 30). Можно переопределить `MESHCORE_BOT_KEEPALIVE_ONLY_WHEN_IDLE_SEC`
- **`weather.provider`**: `openmeteo` (по умолчанию), `openweathermap` (нужен `WEATHER_API_KEY`), `meteostat` (Bulk Data, без ключа) или `meteostat_rapidapi` (RapidAPI, нужен `RAPIDAPI_KEY`)
- **`weather.fallback_provider`**: запасной провайдер погоды. Если основной провайдер вернул ошибку, бот попробует fallback (если задан). Любой поддерживаемый провайдер можно поставить основным и любым — запасным.

Примечание про **Meteostat**:

- Провайдер `meteostat` в этом боте использует **Bulk Data** (`data.meteostat.net`), который **не требует API-ключа** (но данные могут запаздывать до ~24 часов).
- Провайдер `meteostat_rapidapi` использует **Meteostat JSON API** (`meteostat.p.rapidapi.com`) и требует **`RAPIDAPI_KEY`**.
- **`blacklist.path`**: JSON `{"blocked_keys": ["hex", ...]}`
- **`admins.public_keys`**: полные публичные ключи (hex): остановка из **лички**, команды **`каналы` / `channels`** и **`мсг` / `msg`**
- **`admins.channel_indices`**: индексы каналов (из `channels.enabled_indices`), где разрешена команда **`стоп` / `stop`** без проверки ключа отправителя
- **`commands`**: опциональные **`aliases`** для команд `weather`, `help`, `stop`, `channels`, `msg` (см. [config.example.yaml](config.example.yaml))
- **`dm.enabled`**: если `false`, подписка на личные сообщения не ставится — ответов в ЛС не будет (в т.ч. не будет админских команд в ЛС)

### Keepalive и предупреждение «No CHANNEL/CONTACT ... for 90s»

Если в логе периодически появляется предупреждение про отсутствие сообщений 90s и через время бот перестаёт реагировать/отправлять, это часто похоже на “засыпание” USB/companion/радио при полном простое.

Решение: включить keepalive (по умолчанию он включён в `config.example.yaml`):

- **В `config.yaml`**:
  - `poll.keepalive_sec: 60`
  - `poll.keepalive_only_when_idle_sec: 30`
- **Через окружение (перезаписывает конфиг)**:
  - `MESHCORE_BOT_KEEPALIVE_SEC=60` (`0` — выключить)
  - `MESHCORE_BOT_KEEPALIVE_ONLY_WHEN_IDLE_SEC=30`

### Личка: «Fail» у отправителя, а у соседей ЛС ходит

Если **другие ноды рядом нормально обмениваются личками**, радиоканал и прошивка в целом рабочие — это не «сеть не умеет ЛС». Типично другое:

1. **Доставка именно до вашего узла с ботом** — маршрут, дальность, нет контакта/пути к ключу companion, другой частотный план и т.д. Сообщение **не доходит до USB-узла** → в логах бота **нет** строк `CONTACT_MSG_RECV` / `get_msg returned CONTACT_MSG_RECV` (см. `MESHCORE_BOT_TRACE_POLL=1`). Ошибка **«Fail» на телефоне отправителя** относится к этой попытке отправки, а не к Python-коду бота.
2. **Сообщение дошло, но это не команда** — бот молчит (by design). Включите **`MESHCORE_BOT_DEBUG=1`** — в логе будет строка `not a bot command, ignored` с превью текста.
3. **`dm.enabled: true`** в `config.yaml` и один процесс на serial (два клиента на порт — очередь может «есть» не тот).

Проверка: отправьте в ЛС узлу бота явную команду (`помощь`). Если в trace **нет** входящего CONTACT — проблема **до** бота (сеть/контакт/узел). Если **есть** — смотрите парсинг и ответ `send_msg` в логах.

## Лицензия

MIT.
