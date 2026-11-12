-- Ежедневная рассылка-промо (экран daily_promo у ботов). Файл идемпотентный.
-- daily_promos — одна строка на день по Москве с случайным моментом отправки,
-- daily_promo_log — кому уже ушло за день (защита от дублей при перезапусках).

CREATE TABLE IF NOT EXISTS daily_promos (
    day DATE PRIMARY KEY,
    send_at TIMESTAMPTZ NOT NULL,
    sent_count INTEGER NOT NULL DEFAULT 0,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE TABLE IF NOT EXISTS daily_promo_log (
    day DATE NOT NULL REFERENCES daily_promos (day) ON DELETE CASCADE,
    user_platform TEXT NOT NULL,
    user_id BIGINT NOT NULL,
    channel_id BIGINT NOT NULL REFERENCES channels (id) ON DELETE CASCADE,
    sent_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    PRIMARY KEY (day, user_platform, user_id)
);

-- Недоступны ролям Supabase API: читает и пишет только Chat API.
DO $$
DECLARE
    role_name TEXT;
BEGIN
    FOREACH role_name IN ARRAY ARRAY['anon', 'authenticated'] LOOP
        IF EXISTS (SELECT FROM pg_roles WHERE rolname = role_name) THEN
            EXECUTE format('REVOKE ALL ON TABLE daily_promos, daily_promo_log FROM %I', role_name);
        END IF;
    END LOOP;
END
$$;

COMMENT ON TABLE daily_promos IS 'Ежедневная рассылка: день по Москве, случайный send_at (19:00-21:00 МСК), сколько поставлено';
COMMENT ON TABLE daily_promo_log IS 'Кому и через какой чат ушла ежедневная рассылка за день; один человек - одно сообщение в день';
