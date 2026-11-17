-- Автозавершение молчащих диалогов. Файл идемпотентный и безопасный для живой таблицы.
-- last_activity_at: старт пары и каждая реплика/медиа в ней (у обеих сторон). NULL у старых строк — берётся updated_at.

ALTER TABLE chat_sessions ADD COLUMN IF NOT EXISTS last_activity_at TIMESTAMPTZ;

-- Новая причина end_reason = 'inactive'. Ограничение заменяется только если в нём ещё нет 'inactive'.
DO $$
DECLARE
    old_name TEXT;
BEGIN
    SELECT c.conname INTO old_name
    FROM pg_constraint c
    WHERE c.conrelid = 'dialogs'::regclass AND c.contype = 'c'
      AND pg_get_constraintdef(c.oid) LIKE '%end_reason%'
      AND pg_get_constraintdef(c.oid) NOT LIKE '%inactive%'
    LIMIT 1;
    IF old_name IS NOT NULL THEN
        EXECUTE format('ALTER TABLE dialogs DROP CONSTRAINT %I', old_name);
        ALTER TABLE dialogs ADD CONSTRAINT dialogs_end_reason_check
            CHECK (end_reason IN ('stopped', 'reported', 'undeliverable', 'inactive')) NOT VALID;
    END IF;
END
$$;

-- Выборка молчащих диалогов (то же выражение, что в repositories/sessions.py find_inactive).
CREATE INDEX IF NOT EXISTS chat_sessions_chatting_activity_idx
    ON chat_sessions ((COALESCE(last_activity_at, updated_at))) WHERE phase = 'chatting';

-- Проверка существующих строк отдельным запросом; на уже проверенном ограничении ничего не делает.
ALTER TABLE dialogs VALIDATE CONSTRAINT dialogs_end_reason_check;

COMMENT ON COLUMN chat_sessions.last_activity_at IS 'Последняя реплика пары или её старт; NULL — до появления колонки (тогда updated_at).';

COMMENT ON COLUMN dialogs.end_reason IS 'Причина: stopped, reported, undeliverable или inactive (никто не писал DIALOG_INACTIVITY_MINUTES минут; ended_by NULL).';
