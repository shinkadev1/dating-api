-- Режим «Пошлый чат»: очередь делится по режиму, пары собираются только внутри режима. Файл идемпотентный.
-- Режим пишет set_searching (поиск человека или hunt аккаунта chat-engine) и не сбрасывает set_idle:
-- по нему report() заново ищет в том же режиме. Существующие строки получают 'normal'.

ALTER TABLE chat_sessions ADD COLUMN IF NOT EXISTS mode TEXT NOT NULL DEFAULT 'normal';

DO $$
BEGIN
  IF NOT EXISTS (
    SELECT 1 FROM pg_constraint WHERE conname = 'chat_sessions_mode_check' AND conrelid = 'chat_sessions'::regclass
  ) THEN
    ALTER TABLE chat_sessions ADD CONSTRAINT chat_sessions_mode_check CHECK (mode IN ('normal', 'adult'));
  END IF;
END $$;

COMMENT ON COLUMN chat_sessions.mode IS 'normal или adult: режим последнего поиска. Живые и аккаунты chat-engine соединяются только в одном режиме';
