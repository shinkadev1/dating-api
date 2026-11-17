#!/usr/bin/env bash
# Суточный отчёт по пользователям и диалогам. Текст для Telegram — между маркерами TG_REPORT_BEGIN/END. Запускать на сервере vk-bot:
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

CREATE TEMP VIEW p AS
SELECT k, d,
       (d::timestamp AT TIME ZONE current_setting('report.tz')) AS f,
       ((d + 1)::timestamp AT TIME ZONE current_setting('report.tz')) AS t
FROM (VALUES ('cur', current_setting('report.day')::date),
             ('prev', current_setting('report.day')::date - 1)) AS v(k, d);

CREATE TEMP VIEW new_u AS
SELECT p.k, b.platform, count(*) AS n
FROM bot_users b JOIN p ON b.started_at >= p.f AND b.started_at < p.t
WHERE b.platform IN ('vk', 'tg', 'web')
GROUP BY p.k, b.platform;

CREATE TEMP VIEW dlg AS
SELECT p.k, count(*) AS total,
       count(*) FILTER (WHERE a_platform <> 'engine' AND b_platform <> 'engine') AS humans
FROM dialogs x JOIN p ON x.started_at >= p.f AND x.started_at < p.t
GROUP BY p.k;

CREATE TEMP VIEW act AS
SELECT p.k, u.platform, count(*) AS n FROM p, LATERAL (
  SELECT platform, user_id FROM user_activity WHERE day = p.d
  UNION
  SELECT CASE m.sender WHEN 'a' THEN x.a_platform ELSE x.b_platform END,
         CASE m.sender WHEN 'a' THEN x.a_user_id ELSE x.b_user_id END
  FROM messages m JOIN dialogs x ON x.id = m.dialog_id
  WHERE m.created_at >= p.f AND m.created_at < p.t
) u
WHERE u.platform IN ('vk', 'tg', 'web')
GROUP BY p.k, u.platform;

CREATE TEMP VIEW stat AS
SELECT
  COALESCE((SELECT sum(n) FROM new_u WHERE k = 'cur'), 0) AS nu_c,
  COALESCE((SELECT sum(n) FROM new_u WHERE k = 'prev'), 0) AS nu_p,
  COALESCE((SELECT n FROM new_u WHERE k = 'cur' AND platform = 'vk'), 0) AS nu_vk,
  COALESCE((SELECT n FROM new_u WHERE k = 'cur' AND platform = 'tg'), 0) AS nu_tg,
  COALESCE((SELECT n FROM new_u WHERE k = 'cur' AND platform = 'web'), 0) AS nu_web,
  COALESCE((SELECT total FROM dlg WHERE k = 'cur'), 0) AS d_c,
  COALESCE((SELECT total FROM dlg WHERE k = 'prev'), 0) AS d_p,
  COALESCE((SELECT humans FROM dlg WHERE k = 'cur'), 0) AS h_c,
  COALESCE((SELECT humans FROM dlg WHERE k = 'prev'), 0) AS h_p,
  COALESCE((SELECT sum(n) FROM act WHERE k = 'cur'), 0) AS a_c,
  COALESCE((SELECT sum(n) FROM act WHERE k = 'prev'), 0) AS a_p,
  COALESCE((SELECT n FROM act WHERE k = 'cur' AND platform = 'vk'), 0) AS a_vk,
  COALESCE((SELECT n FROM act WHERE k = 'cur' AND platform = 'tg'), 0) AS a_tg,
  COALESCE((SELECT n FROM act WHERE k = 'cur' AND platform = 'web'), 0) AS a_web;

CREATE FUNCTION pg_temp.delta(c numeric, pr numeric) RETURNS text LANGUAGE sql AS $f$
  SELECT CASE WHEN pr = 0 THEN '—'
              WHEN c >= pr THEN '▲' || round((c - pr) * 100 / pr) || '%'
              ELSE '▼' || round((pr - c) * 100 / pr) || '%' END
$f$;

\echo
SELECT 'Отчёт за ' || current_setting('report.day') || ' (' || current_setting('report.tz') || ')';
\echo
\echo 'TG_REPORT_BEGIN'
SELECT '📊 Статистика за ' || to_char(current_setting('report.day')::date, 'DD.MM.YYYY') || ' (МСК)' || E'\n'
  || '• Новые пользователи: ' || nu_c || ' (VK ' || nu_vk || ' / TG ' || nu_tg || ' / Web ' || nu_web || ') — '
  || pg_temp.delta(nu_c, nu_p) || ' к пред. дню' || E'\n'
  || '• Диалоги: ' || d_c || ', из них между людьми ' || h_c || ' — ' || pg_temp.delta(d_c, d_p) || E'\n'
  || '• Всего пользователей за сутки: ' || a_c || ' (VK ' || a_vk || ' / TG ' || a_tg || ' / Web ' || a_web || ') — '
  || pg_temp.delta(a_c, a_p)
FROM stat;
\echo 'TG_REPORT_END'
SQL
