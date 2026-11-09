-- Роли, которых ждут миграции Supabase Storage. В образе supabase/postgres они обычно уже есть:
-- существующие роли не трогаем, недостающие создаём. Повторный запуск безопасен.
DO $$
BEGIN
    -- Миграции storage-api выдают права роли postgres; в этой базе её нет.
    IF NOT EXISTS (SELECT FROM pg_roles WHERE rolname = 'postgres') THEN
        CREATE ROLE postgres NOLOGIN CREATEROLE CREATEDB REPLICATION BYPASSRLS;
    END IF;
    IF NOT EXISTS (SELECT FROM pg_roles WHERE rolname = 'anon') THEN
        CREATE ROLE anon NOLOGIN NOINHERIT;
    END IF;
    IF NOT EXISTS (SELECT FROM pg_roles WHERE rolname = 'authenticated') THEN
        CREATE ROLE authenticated NOLOGIN NOINHERIT;
    END IF;
    IF NOT EXISTS (SELECT FROM pg_roles WHERE rolname = 'service_role') THEN
        CREATE ROLE service_role NOLOGIN NOINHERIT BYPASSRLS;
    END IF;
    IF NOT EXISTS (SELECT FROM pg_roles WHERE rolname = 'supabase_storage_admin') THEN
        CREATE ROLE supabase_storage_admin NOLOGIN;
    END IF;
END
$$;

GRANT anon, authenticated, service_role TO supabase_storage_admin;

-- storage-api работает под supabase_admin и переключается на anon/authenticated/service_role:
-- схеме storage и её объектам нужны права этим ролям (в официальном образе их выдаёт init-скрипт).
CREATE SCHEMA IF NOT EXISTS storage;

GRANT USAGE ON SCHEMA storage TO postgres, anon, authenticated, service_role;

ALTER DEFAULT PRIVILEGES IN SCHEMA storage GRANT ALL ON TABLES TO postgres, anon, authenticated, service_role;

ALTER DEFAULT PRIVILEGES IN SCHEMA storage GRANT ALL ON FUNCTIONS TO postgres, anon, authenticated, service_role;

ALTER DEFAULT PRIVILEGES IN SCHEMA storage GRANT ALL ON SEQUENCES TO postgres, anon, authenticated, service_role;

GRANT ALL ON ALL TABLES IN SCHEMA storage TO postgres, anon, authenticated, service_role;

GRANT ALL ON ALL FUNCTIONS IN SCHEMA storage TO postgres, anon, authenticated, service_role;

GRANT ALL ON ALL SEQUENCES IN SCHEMA storage TO postgres, anon, authenticated, service_role;
