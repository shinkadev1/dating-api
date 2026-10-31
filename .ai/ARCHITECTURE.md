# Архитектура vk-bot

Проверено: **2026-10-31**.

## Поток данных

VK/TG → REST Chat API → ChatService → SQL/Redis → WebSocket клиентам. Сайт и mobile приходят через Worker из репозитория landing. Для внешнего собеседника: Redis → engine_bridge → REST/WS chat-engine → attach в Chat API.

## Точки входа

| Путь | Ответственность |
| --- | --- |
| `chat_api/app/main.py` | FastAPI, lifespan, подключения к Postgres/Redis/S3, `/health`. |
| `chat_api/app/routers/` | REST/WS: peers, engine, channels, media, visitors, stats, ws. |
| `chat_api/app/services/chat.py` | Поиск, остановка, жалобы, создание пары и пересылка. |
| `chat_api/app/repositories/` | SQL для сессий, посетителей и медиа. |
| `chat_api/app/events.py` | Redis Streams и выдача событий по WebSocket. |
| `chat_api/app/deps.py` | Авторизация и ограничения платформ для токенов. |
| `chat_api/app/db.py` | Повторный запуск всех SQL-файлов из init при старте. |
| `supabase/init/` | 001_schema.sql и 002_premium_grant.sql: схема и изменения данных. |
| `engine_bridge/app/bridge.py` | demand → hunt → внешний поиск → attach → пересылка. |
| `docker-compose.yml` | Postgres, Redis, API, bridge и вспомогательные сервисы. |

## Контракты и ограничения

- Адрес участника: `(platform, bot_key, user_id)`; фазы: `idle`, `searching`, `chatting`. Ключ профиля bot_users в актуальной схеме — `(platform, user_id)`.
- Живые люди приоритетны; engine не встаёт в человеческую очередь. Attach должен учитывать уже найденную пару и отмену поиска.
- `POST .../media` сохраняет байты и вызывает пересылку события; README с описанием только хранения устарел. Для TG предусмотрены file_id / method.
- SQL-файлы выполняются при каждом старте, а не один раз. Изменения должны выдерживать повторное применение.
- Сервисный, TG, app и engine токены имеют разные области доступа; app обслуживает платформу web.

## Развёртывание

Push в `master` запускает `.github/workflows/deploy.yml`; серверное обновление — `deploy/update.sh`.

Схема описывает репозиторий, а не подтверждённое состояние production.
