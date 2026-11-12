-- Счётчик завершённых диалогов человека для показа «купи премиум» после каждого 10-го. Файл идемпотентный.
-- Увеличивает _close_open_dialog в repositories/sessions.py при закрытии диалога. Прошлые диалоги не пересчитываем: счёт с нуля.

ALTER TABLE bot_users ADD COLUMN IF NOT EXISTS dialogs_ended INTEGER NOT NULL DEFAULT 0;

COMMENT ON COLUMN bot_users.dialogs_ended IS 'Сколько диалогов человека закрыто с момента появления колонки. Каждый 10-й даёт событие promo';
