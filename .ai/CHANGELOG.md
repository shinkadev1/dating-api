# История важных изменений

## 2026-11-09 — медиа в Supabase Storage (ветка `loop/supabase-storage-media`)

Фото, видео, кружки и голосовые теперь хранятся в self-hosted Supabase Storage на том же сервере (бакет `chat-media`, виден в Studio → Storage → Files), а не в S3 Beget. Ключи объектов сохранены; `stored_media.object_key` не менялся. Добавлены `storage` (supabase/storage-api v1.74.0, тег из docker-compose supabase/supabase на ревизии со studio 2026.09.07), `storage-gw` (nginx 1.27-alpine, `/storage/v1/*` для Studio), `media-migrate` (rclone 1.69.1, профиль migrate), `003_storage_roles.sql` (недостающие роли anon/authenticated/service_role/supabase_storage_admin). `deploy/update.sh`: ключи `STORAGE_S3_*`, свои `JWT_SECRET`/`ANON_KEY`/`SERVICE_ROLE_KEY` вместо демо, диагностика, проверка места, копирование Beget → chat-media двумя проходами с `rclone check`, маркер `.media-migrated`, smoke. Откат на Beget — `MEDIA_S3_*` в `.env` сервера (см. README). Beget-бакет не изменяется и не очищается. Docker на машине разработки недоступен: интеграционный прогон не выполнялся.

## 2026-11-04 — доработка оплаты ЮKassa (ветка `loop/payments-complete`)

Страница оформления по структуре референса: тарифы проекта, два обязательных согласия (версия 2026-11-04, сервер проверяет сам), текст автопродления из `billing.renewal_terms`, понятные статусы заказа. Подтверждённый отказ ЮKassa (4xx при создании платежа) переводит заказ в failed/rejected и не блокирует новый; 429/5xx/сеть остаются «неизвестным исходом» с тем же Idempotence-Key. Отмена подписки очищает сохранённую карту (method_id). Блок кодов /link скрыт на странице аккаунта. Добавлена инструкция локального теста. Не сделано: возврат через API, `/link` в ботах.

## 2026-11-03 — оплата премиума и запрет медиа без него (ветка `payments`)

Перенесён premium_site (ЮKassa) из старого worktree ветки `premium` без монолита vk_bot/. Добавлены подписанная ссылка на оплату `GET .../premium/link`, вход на странице оплаты по `utm_content` без email, синхронизация `bot_users.premium_until` и 403 на медиа без премиума у любой из сторон. Причина: премиум = отправка фото/видео/аудио, покупка по аккаунту платформы. Новые переменные: `PREMIUM_LINK_SECRET`, `PAY_PUBLIC_URL` (chat-api) и переменные premium_site в `.env.example`. Тесты без Postgres прошли; тесты с базой и запуск compose не выполнялись (нет Docker/Postgres).

## 2026-10-31 — оркестрация и фиксация контекста

Добавлены .codex/config.toml и explorer/planner/developer/reviewer/reporter, правила routing и workflow в AGENTS.md. Модель planner/reviewer — gpt-6-astra/high по подтверждённому выбору пользователя. Обновлены правила и память; контекст и конфигурация сохраняются отдельным локальным коммитом без прикладных изменений и push. Проверки: 42 TOML, строгая загрузка конфигураций в 7 корнях, diff check, reviewer PASS; mobile typecheck прошёл. Запуск всех ролей и ESLint не проверены (ESLint не настроен).

## 2026-10-31 — локальный контекст репозитория

Добавлены PROJECT, ARCHITECTURE, CONVENTIONS, CURRENT_TASK, STATE и CHANGELOG в .ai/; AGENTS.md направляет к ним будущие сессии. Комплект описывает именно vk-bot и не требует соседних репозиториев для начала работы. Существующие инструкции сохранены.

Код и деплой не менялись. Проверки контекста и ограничения отражены в [STATE.md](STATE.md).

Историческая точка исходников: `master` / `e746076`. Это не подтверждение деплоя или отсутствия незакоммиченных изменений.

## Формат следующих записей

Дата, изменение и причина, затронутые контракты, коммит/файлы и реальные проверки. Текущие планы хранить в CURRENT_TASK; актуальные ограничения — в STATE.
