-- История диалогов: диалог (пара), реплики и журнал выгрузок в S3. Файл идемпотентный.
-- Сторона a — инициатор пары (искавший; для chat-engine — аккаунт), сторона b — второй участник.
-- Реплики старше MESSAGES_HOT_DAYS Chat API выгружает в S3 и удаляет из messages; dialogs остаются.

CREATE TABLE IF NOT EXISTS dialogs (
    id BIGSERIAL PRIMARY KEY,
    a_platform TEXT NOT NULL,
    a_bot_key TEXT NOT NULL,
    a_user_id BIGINT NOT NULL,
    b_platform TEXT NOT NULL,
    b_bot_key TEXT NOT NULL,
    b_user_id BIGINT NOT NULL,
    started_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    ended_at TIMESTAMPTZ,
    end_reason TEXT CHECK (end_reason IN ('stopped', 'reported', 'undeliverable')),
    ended_by TEXT CHECK (ended_by IN ('a', 'b'))
);

CREATE INDEX IF NOT EXISTS dialogs_a_idx ON dialogs (a_platform, a_user_id, started_at DESC);

CREATE INDEX IF NOT EXISTS dialogs_b_idx ON dialogs (b_platform, b_user_id, started_at DESC);

CREATE TABLE IF NOT EXISTS messages (
    id BIGSERIAL PRIMARY KEY,
    dialog_id BIGINT NOT NULL REFERENCES dialogs (id),
    sender TEXT NOT NULL CHECK (sender IN ('a', 'b')),
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    kind TEXT NOT NULL CHECK (kind IN ('text', 'photo', 'video', 'video_note', 'voice', 'audio')),
    text TEXT,
    media_id UUID,
    tg_file_id TEXT,
    tg_method TEXT,
    content_type TEXT,
    status TEXT NOT NULL CHECK (status IN ('delivered', 'left', 'skipped'))
);

CREATE INDEX IF NOT EXISTS messages_created_idx ON messages (created_at);

CREATE INDEX IF NOT EXISTS messages_dialog_idx ON messages (dialog_id, id);

CREATE TABLE IF NOT EXISTS message_archives (
    day DATE NOT NULL,
    part INTEGER NOT NULL DEFAULT 1,
    object_key TEXT NOT NULL UNIQUE,
    rows_count INTEGER NOT NULL,
    size_bytes BIGINT NOT NULL,
    first_message_id BIGINT NOT NULL,
    last_message_id BIGINT NOT NULL,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    PRIMARY KEY (day, part)
);

-- Открытый диалог пары. Без внешнего ключа: сессия живёт независимо от истории.
ALTER TABLE chat_sessions ADD COLUMN IF NOT EXISTS dialog_id BIGINT;

-- Таблицы с перепиской недоступны ролям Supabase API: читает и пишет только Chat API.
DO $$
DECLARE
    role_name TEXT;
BEGIN
    FOREACH role_name IN ARRAY ARRAY['anon', 'authenticated'] LOOP
        IF EXISTS (SELECT FROM pg_roles WHERE rolname = role_name) THEN
            EXECUTE format('REVOKE ALL ON TABLE dialogs, messages, message_archives FROM %I', role_name);
            EXECUTE format('REVOKE ALL ON SEQUENCE dialogs_id_seq, messages_id_seq FROM %I', role_name);
        END IF;
    END LOOP;
END
$$;

COMMENT ON TABLE dialogs IS 'Диалог пары: кто с кем, когда начался и чем закончился. Строки не удаляются.';

COMMENT ON COLUMN dialogs.ended_by IS 'Сторона (a или b), которая закончила диалог; NULL — закрыл сервис.';

COMMENT ON COLUMN dialogs.end_reason IS 'Причина: stopped, reported или undeliverable.';

COMMENT ON TABLE messages IS 'Реплики диалогов за последние MESSAGES_HOT_DAYS дней; старше выгружаются в S3 и удаляются.';

COMMENT ON COLUMN messages.sender IS 'Сторона диалога (a или b), приславшая реплику.';

COMMENT ON COLUMN messages.status IS 'delivered — отправлена собеседнику, left — пары уже не было, skipped — файл Telegram без байтов не передан.';

COMMENT ON TABLE message_archives IS 'Журнал выгрузок реплик за день в S3 (archive/messages/ГГГГ/ММ/ДД.jsonl.gz).';

COMMENT ON COLUMN chat_sessions.dialog_id IS 'Открытый диалог этой пары в dialogs; NULL вне диалога и у пар, созданных до истории.';
