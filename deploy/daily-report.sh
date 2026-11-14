#!/usr/bin/env bash
# Суточный отчёт по пользователям. Запускать на сервере vk-bot:
#   bash deploy/daily-report.sh            # за вчера (по REPORT_TZ)
#   bash deploy/daily-report.sh 2026-11-14 # за конкретный день
# Только чтение: SELECT через psql внутри контейнера базы.
set -euo pipefail

# APP_DIR задаёт workflow, когда скрипт передаётся по ssh через stdin (bash -s).
cd "${APP_DIR:-$(dirname "$0")/..}"

DB_CONTAINER="${DB_CONTAINER:-vk-bot-db}"
REPORT_TZ="${REPORT_TZ:-Europe/Moscow}"
POSTGRES_DB="${POSTGRES_DB:-}"
if [ -z "$POSTGRES_DB" ] && [ -f .env ]; then
  POSTGRES_DB="$(grep -E '^POSTGRES_DB=' .env | tail -n1 | cut -d= -f2- | tr -d '"'"'"'\r')"
fi
POSTGRES_DB="${POSTGRES_DB:-postgres}"
DAY="${1:-}"

docker exec -i "$DB_CONTAINER" psql -U supabase_admin -d "$POSTGRES_DB" \
  -X -q -At -F ' ' -v ON_ERROR_STOP=1 -v tz="$REPORT_TZ" -v day="$DAY" <<'SQL'
SELECT set_config('report.tz', :'tz', false) AS _t \gset
SELECT set_config('report.day',
  COALESCE(NULLIF(:'day', '')::date,
           (now() AT TIME ZONE :'tz')::date - 1)::text, false) AS _d \gset

CREATE TEMP VIEW w AS
SELECT (current_setting('report.day')::date::timestamp AT TIME ZONE current_setting('report.tz')) AS f,
       ((current_setting('report.day')::date + 1)::timestamp AT TIME ZONE current_setting('report.tz')) AS t;

\echo
SELECT 'Отчёт за ' || current_setting('report.day') || ' (' || current_setting('report.tz') || ')';

\echo
\echo '1. Новые пользователи за сутки'
SELECT '   ' || COALESCE(platform, 'ВСЕГО') || ': ' || count(*)
FROM bot_users, w
WHERE started_at >= w.f AND started_at < w.t AND platform <> 'engine'
GROUP BY ROLLUP(platform)
ORDER BY platform NULLS LAST;
SELECT '   (нет новых)' WHERE NOT EXISTS (
  SELECT 1 FROM bot_users, w
  WHERE started_at >= w.f AND started_at < w.t AND platform <> 'engine');

\echo
\echo '2. Активные пользователи за сутки (≈, нижняя оценка по last_seen_at)'
SELECT '   ' || COALESCE(platform, 'ВСЕГО') || ': ≈' || count(*)
FROM bot_users, w
WHERE last_seen_at >= w.f AND last_seen_at < w.t AND platform <> 'engine'
GROUP BY ROLLUP(platform)
ORDER BY platform NULLS LAST;
SELECT '   (нет активных)' WHERE NOT EXISTS (
  SELECT 1 FROM bot_users, w
  WHERE last_seen_at >= w.f AND last_seen_at < w.t AND platform <> 'engine');

\echo
\echo '3. Диалоги за сутки: нет данных — история диалогов не записывается'
SELECT '   Срез сейчас (' || to_char(now() AT TIME ZONE current_setting('report.tz'), 'YYYY-MM-DD HH24:MI') || '): '
       || 'пар людей ' || (count(*) FILTER (WHERE partner_platform <> 'engine')) / 2
       || ', диалогов с chat-engine ' || count(*) FILTER (WHERE partner_platform = 'engine')
FROM chat_sessions WHERE phase = 'chatting';
SQL
