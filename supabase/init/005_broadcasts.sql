-- Рассылки из CRM: по времени и через паузу после события. Файл идемпотентный.
-- Отправляет фоновая задача Chat API (app/broadcasts), журнал broadcast_log не даёт отправить дважды.

CREATE TABLE IF NOT EXISTS broadcasts (
    id BIGSERIAL PRIMARY KEY,
    title TEXT NOT NULL DEFAULT '',
    text TEXT NOT NULL CHECK (char_length(text) BETWEEN 1 AND 4096),
    media_id TEXT,
    content_type TEXT,
    platforms TEXT[] NOT NULL CHECK (platforms <@ ARRAY['vk', 'tg', 'web'] AND cardinality(platforms) > 0),
    kind TEXT NOT NULL CHECK (kind IN ('at', 'event')),
    send_at TIMESTAMPTZ,
    event TEXT CHECK (event IN ('first_seen', 'last_seen', 'dialog_ended', 'premium_ended')),
    delay_minutes INTEGER CHECK (delay_minutes BETWEEN 0 AND 525600),
    repeat BOOLEAN NOT NULL DEFAULT FALSE,
    status TEXT NOT NULL CHECK (status IN ('scheduled', 'sending', 'done', 'active', 'paused', 'cancelled')),
    sent_count INTEGER NOT NULL DEFAULT 0,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    active_since TIMESTAMPTZ,
    finished_at TIMESTAMPTZ,
    CHECK (kind <> 'at' OR send_at IS NOT NULL),
    CHECK (kind <> 'event' OR (event IS NOT NULL AND delay_minutes IS NOT NULL))
);

CREATE INDEX IF NOT EXISTS broadcasts_due_idx ON broadcasts (status, send_at);

CREATE TABLE IF NOT EXISTS broadcast_log (
    id BIGSERIAL PRIMARY KEY,
    broadcast_id BIGINT NOT NULL REFERENCES broadcasts (id) ON DELETE CASCADE,
    user_platform TEXT NOT NULL,
    user_id BIGINT NOT NULL,
    channel_id BIGINT NOT NULL REFERENCES channels (id) ON DELETE CASCADE,
    event_at TIMESTAMPTZ,
    sent_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE UNIQUE INDEX IF NOT EXISTS broadcast_log_once_idx
    ON broadcast_log (broadcast_id, user_platform, user_id, channel_id, COALESCE(event_at, 'epoch'::timestamptz));

-- Рассылки и журнал недоступны ролям Supabase API: читает и пишет только Chat API.
DO $$
DECLARE
    role_name TEXT;
BEGIN
    FOREACH role_name IN ARRAY ARRAY['anon', 'authenticated'] LOOP
        IF EXISTS (SELECT FROM pg_roles WHERE rolname = role_name) THEN
            EXECUTE format('REVOKE ALL ON TABLE broadcasts, broadcast_log FROM %I', role_name);
            EXECUTE format('REVOKE ALL ON SEQUENCE broadcasts_id_seq, broadcast_log_id_seq FROM %I', role_name);
        END IF;
    END LOOP;
END
$$;

COMMENT ON TABLE broadcasts IS 'Рассылки из CRM: kind=at — один раз в send_at, kind=event — через delay_minutes после события';
COMMENT ON COLUMN broadcasts.event IS 'first_seen — первый раз написал в чат, last_seen — последняя активность, dialog_ended — закончился диалог, premium_ended — истёк премиум';
COMMENT ON COLUMN broadcasts.repeat IS 'Для event: false — одному человеку один раз, true — после каждого нового события';
COMMENT ON COLUMN broadcasts.active_since IS 'Для event: учитываются только события не раньше этого момента';
COMMENT ON TABLE broadcast_log IS 'Кому и в какой чат ушла рассылка; event_at — событие, после которого ушла';
