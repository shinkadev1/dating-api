-- Дни активности людей для аналитики CRM (DAU/WAU/MAU, retention). Файл идемпотентный.
-- Одна строка — человек был активен в этот день по Москве. Пишет _remember в repositories/sessions.py.
-- Историю до появления таблицы восстанавливаем один раз: день прихода, день последнего визита и дни диалогов.

CREATE TABLE IF NOT EXISTS user_activity (
    platform TEXT NOT NULL,
    user_id BIGINT NOT NULL,
    day DATE NOT NULL,
    PRIMARY KEY (platform, user_id, day)
);

CREATE INDEX IF NOT EXISTS user_activity_day_idx ON user_activity (day, platform);

DO $$
BEGIN
    IF NOT EXISTS (SELECT 1 FROM user_activity LIMIT 1) THEN
        INSERT INTO user_activity (platform, user_id, day)
        SELECT platform, user_id, (started_at AT TIME ZONE 'Europe/Moscow')::date FROM bot_users
        UNION
        SELECT platform, user_id, (last_seen_at AT TIME ZONE 'Europe/Moscow')::date FROM bot_users
        UNION
        SELECT a_platform, a_user_id, (started_at AT TIME ZONE 'Europe/Moscow')::date FROM dialogs WHERE a_platform <> 'engine'
        UNION
        SELECT b_platform, b_user_id, (started_at AT TIME ZONE 'Europe/Moscow')::date FROM dialogs WHERE b_platform <> 'engine'
        ON CONFLICT DO NOTHING;
    END IF;
END
$$;

DO $$
DECLARE
    role_name TEXT;
BEGIN
    FOREACH role_name IN ARRAY ARRAY['anon', 'authenticated'] LOOP
        IF EXISTS (SELECT FROM pg_roles WHERE rolname = role_name) THEN
            EXECUTE format('REVOKE ALL ON TABLE user_activity FROM %I', role_name);
        END IF;
    END LOOP;
END
$$;

COMMENT ON TABLE user_activity IS 'Дни (по Москве), когда человек писал боту или искал собеседника. Для аналитики CRM';
