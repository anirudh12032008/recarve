-- Dev-only shim: the slice of Supabase that migrations depend on.
--
-- This file is NEVER applied to the real Supabase project — Supabase already
-- provides auth.users, auth.uid() and the authenticated/service_role roles.
-- It exists so the schema and its RLS policies can be tested against plain
-- Postgres, with no Docker and no cloud project.
--
-- Everything in supabase/migrations/ must run unchanged against both this and
-- real Supabase. If a migration needs something Supabase provides that is
-- missing here, add it to this file rather than weakening the migration.

create schema if not exists auth;

create table if not exists auth.users (
  id          uuid primary key,
  instance_id uuid,
  aud         text,
  role        text,
  email       text unique
);

-- Supabase derives the caller from the JWT that PostgREST puts into the
-- request.jwt.claims GUC. Reading the same GUC means policies written against
-- auth.uid() behave identically here and in production.
-- nullif before the cast, which is what real Supabase does and what this
-- copy did not. notes.act_as(conn, None) clears the setting by writing an
-- empty string, and ''::json raises rather than returning null -- so any
-- statement that reached auth.uid() on the owning connection died instead of
-- being told there is nobody there. RLS hid that for a long time: the owner
-- bypasses policies, so nothing called this until a trigger did.
create or replace function auth.uid() returns uuid
language sql stable as $$
  select nullif(
    coalesce(nullif(current_setting('request.jwt.claims', true), '')::json ->> 'sub', ''),
    ''
  )::uuid;
$$;

do $$
begin
  if not exists (select from pg_roles where rolname = 'anon') then
    create role anon nologin;
  end if;
  if not exists (select from pg_roles where rolname = 'authenticated') then
    create role authenticated nologin;
  end if;
  if not exists (select from pg_roles where rolname = 'service_role') then
    create role service_role nologin;
  end if;
end
$$;

grant usage on schema auth   to anon, authenticated, service_role;
grant usage on schema public to anon, authenticated, service_role;
grant execute on function auth.uid() to anon, authenticated, service_role;

-- Tests insert auth.users rows while acting as a member, which production
-- never does (Supabase Auth owns that table).
grant select, insert on auth.users to authenticated, service_role;
