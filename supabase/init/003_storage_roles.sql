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

-- storage-api работает под supabase_admin и переключается на anon/authenticated/service_role.
-- Полные права на объекты схемы storage получают только postgres и service_role; anon и authenticated
-- видят схему, а права на buckets и objects им выдают сами миграции storage-api (как в официальном Supabase).
CREATE SCHEMA IF NOT EXISTS storage;

GRANT USAGE ON SCHEMA storage TO postgres, anon, authenticated, service_role;

ALTER DEFAULT PRIVILEGES IN SCHEMA storage GRANT ALL ON TABLES TO postgres, service_role;

ALTER DEFAULT PRIVILEGES IN SCHEMA storage GRANT ALL ON FUNCTIONS TO postgres, service_role;

ALTER DEFAULT PRIVILEGES IN SCHEMA storage GRANT ALL ON SEQUENCES TO postgres, service_role;

GRANT ALL ON ALL TABLES IN SCHEMA storage TO postgres, service_role;

GRANT ALL ON ALL FUNCTIONS IN SCHEMA storage TO postgres, service_role;

GRANT ALL ON ALL SEQUENCES IN SCHEMA storage TO postgres, service_role;
