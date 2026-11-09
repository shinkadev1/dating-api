# Dating API

Люди из сообществ ВК, Telegram и сайта попадают в одну очередь. Логика поиска и пар живёт здесь.
Бот ВК — репозиторий `dating-bot-vk`, бот Telegram — `dating-bot-tg`.
Бот ВК вызывает REST и читает события из Redis Streams. Бот Telegram ходит по HTTPS
(`https://api.chatwithyou.site`) через Cloudflare Access и получает события по WebSocket
`/api/v1/events/tg`. Сайт (`landing`, отдельный репозиторий) ходит в тот же Chat API
через Cloudflare Worker.

## Собеседники из chat-engine

Если человек в очереди один, ему подбирается собеседник через chat-engine (bots.chatwithyou.site):
Chat API публикует событие `demand` в `chat:events:engine`, `engine_bridge` сразу запускает
поиск на свободном аккаунте chat-engine и, когда тот нашёл собеседника, вызывает
`POST /api/v1/engine/{account}/attach`. Живые люди в приоритете: аккаунты chat-engine в общую
очередь не попадают, а если человек за время поиска нашёл живого, `attach` отказывает и мост
закрывает разговор в chat-engine. Задержку перед подбором задаёт `ENGINE_FALLBACK_AFTER_SECONDS`
(по умолчанию 0), бот снимает человека с поиска через 30 секунд.

## Статистика для сайта

`GET /api/v1/stats` с любым токеном Chat API, кеш 5 секунд:

```json
{"online": 12, "searching": 2, "chatting": 8,
 "humans": {"online": 10, "searching": 2, "chatting": 4},
 "bots": {"online": 2, "searching": 0, "chatting": 4}}
```

Онлайн — кто в поиске или в диалоге, плюс кто писал за последние 5 минут
(`STATS_ONLINE_WINDOW_SECONDS`). Общие числа включают аккаунты chat-engine.

## Уведомление всем чатам

`POST /api/v1/notifications` с сервисным токеном бота (`CHAT_API_SERVICE_TOKEN`).

Только текст:

```json
{"text": "Сегодня возможны короткие паузы в поиске"}
```

Текст и одно фото на всю рассылку — форма `multipart`: поле `text` и файл `file`. Фото уходит вместе с текстом.

Ответ `{"queued": 120}` — сколько диалогов поставлено в очередь. Текст уходит каждому человеку
в каждом чате, куда подключён бот: сообщества ВК, бот Telegram и сайт. Человек, который писал
в два сообщества, получит сообщение в обоих. Текущий анонимный диалог не закрывается,
кнопки под последней репликой остаются. Доставка идёт около 10 сообщений в секунду,
чтобы не упереться в лимиты ВК и Telegram.

Сайт получает событие `type: notice` по WebSocket `/api/v1/events/web`.
Его нужно показать отдельно от реплики собеседника.

## Медиа

В активном диалоге бот забирает у человека фото, видео, кружок или голосовое и вызывает
`POST /api/v1/peers/{platform}/{bot_key}/{user_id}/media` (`multipart`: поле `kind` и файл `file`).
Chat API проверяет байты, кладёт объект в Supabase Storage (бакет `chat-media`) и пишет ключ в `stored_media`.
Собеседнику файл этим запросом не отправляется.

`kind`: `photo`, `video`, `video_note` (кружок), `voice`.
Пределы: фото и голосовое 20 МБ, кружок 32 МБ, видео 100 МБ.

### Хранилище медиа

Медиа лежат в self-hosted Supabase Storage (`storage-api`, файловый бэкенд на томе `storage_data`) на том же сервере.
Chat API ходит в него по S3-протоколу (`http://storage:5000/s3`, path-style, регион `local`); ключи объектов
остаются прежними. Файлы видны в Studio → Storage → Files, бакет `chat-media`. Порты storage наружу не публикуются;
Studio обращается к нему через внутренний шлюз `storage-gw` (`/storage/v1/*`).

Переменные: `STORAGE_S3_ACCESS_KEY_ID`, `STORAGE_S3_SECRET_KEY` (на сервере их создаёт `deploy/update.sh`),
`JWT_SECRET`, `ANON_KEY`, `SERVICE_ROLE_KEY` (на сервере выпускаются, если там демо-значения). Локально бакет создаётся так:
`docker compose --profile migrate run --rm media-migrate mkdir dst:chat-media`.

Порядок деплоя (`deploy/update.sh`):

1. `db` и `redis`, диагностика ролей и схем, `supabase/init/003_storage_roles.sql`, затем `storage` и `storage-gw`.
2. Бакет `chat-media`. Если в `.env` есть ключи Beget (`S3_*`) и не задан `MEDIA_S3_ENDPOINT_URL`, на КАЖДОМ деплое
   выполняется `rclone copy --size-only` из Beget в `chat-media` (проход 1). Пока нет файла `.media-migrated`
   (первый перенос), перед этим проверяется свободное место: нужно 1,5 × размер бакета + 1 ГиБ, иначе деплой
   останавливается до переключения chat-api. На Beget ничего не меняется и не удаляется.
3. Пересоздаётся `chat-api`; проход 2 `rclone copy` и `rclone check --size-only --one-way`. При расхождениях
   деплой падает; при нуле расхождений создаётся `.media-migrated` (он лишь пропускает проверку места).
4. SMOKE: контейнеры healthy, put/get/delete из chat-api, `GET /storage/v1/bucket` из Studio через шлюз,
   размеры бакетов и `count/sum(size_bytes)` по `stored_media` в логе.

Секреты: `JWT_SECRET` берётся из `.env` сервера; демо-значение (в том числе из `ENV_FILE`) игнорируется и заменяется
сгенерированным один раз, вместе с `ANON_KEY` и `SERVICE_ROLE_KEY`. Значения `local-storage-*` из `.env.example`
для `STORAGE_S3_*` на сервере отбрасываются. `JWT_SECRET` больше не передаётся контейнеру `db` (его init-скрипты
перекрыты, на базу он не влиял), поэтому его смена не пересоздаёт Postgres. Единственный перезапуск `db` на
первом деплое этой версии связан с удалением этой переменной из окружения контейнера.

Откат на Beget: в `.env` на сервере задать `MEDIA_S3_ENDPOINT_URL`, `MEDIA_S3_REGION`, `MEDIA_S3_BUCKET`,
`MEDIA_S3_ACCESS_KEY`, `MEDIA_S3_SECRET_KEY` (значения Beget) и выполнить `docker compose up -d --force-recreate chat-api`;
объекты Beget не удалялись. Пока `MEDIA_S3_ENDPOINT_URL` задан, деплой не копирует Beget → `chat-media`. Новые объекты
остаются в Beget; после возврата (удалить `MEDIA_S3_*` из `.env` и задеплоить) следующий деплой докопирует их в
`chat-media` автоматически. Объекты, записанные в `chat-media` до отката, в Beget не возвращаются
(`rclone copy dst:chat-media src:<бакет>`, если нужно).
Без ключей Chat API загрузка медиа отвечает 503.

## Структура

```
.
├── engine_bridge/              # Сервис: мост к chat-engine (контейнер vk-engine-bridge, профиль engine)
│   └── app/
│       ├── bridge.py           # demand → hunt → поиск в chat-engine → attach, пересылка реплик
│       ├── engine.py           # REST и WebSocket chat-engine
│       ├── chat_api.py         # Клиент Chat API от имени аккаунтов (токен CHAT_API_ENGINE_TOKEN)
│       └── events.py           # События Chat API из chat:events:engine
├── chat_api/                   # Сервис: Chat API на FastAPI (контейнер vk-chat-api)
│   ├── Dockerfile              # Собирается из корня: нужен каталог supabase/init
│   ├── requirements.txt
│   └── app/
│       ├── main.py             # Приложение, lifespan, /health
│       ├── config.py           # Настройки
│       ├── db.py               # Пул Postgres и прогон схемы при старте
│       ├── deps.py             # Авторизация по токену: бот ВК, бот Telegram, сайт, engine_bridge
│       ├── events.py           # Redis Streams и WebSocket-выдача событий ботам на других серверах
│       ├── peer.py
│       ├── schemas.py
│       ├── repositories/sessions.py  # SQL: очередь, пары, пользователи
│       ├── services/chat.py    # Логика поиска и диалога
│       └── routers/            # peers.py — REST действий, channels.py — чаты и /notifications,
│                               # engine.py — подбор из chat-engine, stats.py — /stats,
│                               # ws.py — /events/web, /events/{vk|tg}, /broadcasts/{vk|tg}
├── supabase/
│   ├── init/001_schema.sql     # Схема базы (идемпотентная)
│   └── studio/                 # Папки Supabase Studio
├── deploy/
│   └── update.sh               # Сервер: .env, pull образов, перезапуск
├── .github/workflows/
│   ├── deploy.yml              # Push в master → образы Chat API и моста → сервер
│   └── server-logs.yml         # Ручной просмотр логов на сервере
├── docker-compose.yml          # db, redis, chat-api, engine-bridge, studio, meta, cloudflared
└── .env.example
```

## Запуск

Всё вместе в Docker:

```sh
cp .env.example .env
docker compose up --build
```

- Chat API: http://127.0.0.1:8000/docs
- Studio: http://127.0.0.1:54323

Бот ВК запускается отдельно из репозитория `dating-bot-vk` и получает события по WebSocket.
Ему нужен тот же `CHAT_API_SERVICE_TOKEN`. Бот Telegram — репозиторий `dating-bot-tg`, на сервере chat-engine.

Секреты GitHub репозитория `aStormspirit/dating-api`:

- `CHAT_API_TG_TOKEN` — токен Chat API только для пользователей tg; тот же секрет в `dating-bot-tg`
- `CHAT_ENGINE_TOKEN` — `API_TOKEN` chat-engine, для `engine_bridge`

`CHAT_API_ENGINE_TOKEN` и `CHAT_API_SERVICE_TOKEN` генерируются на сервере.
Мост к chat-engine запускается, если задан `CHAT_ENGINE_TOKEN`.
