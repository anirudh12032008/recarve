-- The second door, and the first one nobody has to be handed a code for.
--
-- 0044 said there was exactly one signup path and that the invite was the
-- honest place to read a section from, because the invite was the one thing a
-- joiner had been given by a person who knew which section they meant. That
-- was true when nine of the ten sections had no students and no codes. It is
-- not true now: the institute publishes the section list itself -- 1054
-- students, roll number and scholar number side by side -- and mails every one
-- of them from an address with their scholar number in it.
--
-- So there is a better answer to "which section is this person in" than a code
-- over WhatsApp: ask Google who they are, and look the answer up in the list
-- the registrar wrote. Nobody types a roll number, nobody mints a code, and a
-- student cannot land in the wrong section by mistyping one character.
--
-- The invite path is untouched. It is still how somebody joins who is not on
-- this list -- a transfer, a repeat year, a section the list does not cover --
-- and it is still how the very first admin of an install is made.

-- ---------------------------------------------------------------------------
-- The list. One row is one seat, and it holds nothing about a person: no
-- phone, no password, no profile. It exists to answer one question, asked by
-- one function.
--
-- The section comes from the roll number's letter. It is NOT in the scholar
-- number -- 26113011201 is an Electrical student in Section A and 26113011208
-- is an Electrical student in Section B -- which is the whole reason this
-- table has rows instead of being a substring.
create table roll_list (
  scholar_no text primary key,
  roll_no    text not null unique,
  name       text not null,
  section_id uuid not null references sections(id)
);

-- Row level security on, and not one policy, deliberately. This is the class
-- list of the entire first year with names attached, and no member of any
-- section has a reason to read another section's. RLS with no policy is how a
-- table says "nobody": every select matches nothing, for everyone. The one
-- thing that reads it is join_with_google below, which is security definer and
-- therefore does not consult this at all.
--
-- The grant beside it looks like the opposite and is not. Reaching a table and
-- reading rows out of it are two different permissions in Postgres, and this
-- install's two guards -- test_policy_matrix's "every public table has RLS" and
-- test_table_grants' "every table with RLS is granted" -- want both said out
-- loud so that neither can be the one somebody forgot. The policy is what
-- refuses the rows, and there is none.
alter table roll_list enable row level security;
grant select on roll_list to authenticated;

-- Rows are in backend/dev/seed_roll_list.sql, not here, the same division
-- 0047 and seed_timetables.sql keep: schema in a migration, the institute's
-- data in a seed the live database is given by hand.

-- ---------------------------------------------------------------------------
-- Which Google account a profile is. Unique, because two profiles claiming one
-- address is two people sharing a seat or one person holding two, and both are
-- worse discovered late.
alter table profiles add column email text unique;

-- ---------------------------------------------------------------------------
-- The door itself. Same shape as join_with_invite: returns a bare boolean,
-- tells a caller who is not on the list nothing, and takes the identity from
-- auth.uid() rather than from an argument, so nothing a request sends can
-- decide whose profile is written.
--
-- The email is not checked here. Postgres cannot see a Google token, and a
-- function that took the domain on trust from its caller would be a function
-- that lets anybody claim any seat. The check that the address is real, is
-- verified and ends in the institute's domain happens in the server, before
-- this is called, and this exists to make the *section* unforgeable once it
-- has: the scholar number indexes a row the registrar wrote.
--
-- The password is random and nobody is told it. A Google account does not have
-- a password here, but the column is what the roll-number login reads and a
-- null in it means "claimable by whoever types the roll number first" (0013).
-- Sixty-four hex characters nobody has ever seen is the honest way to say "this
-- account does not open that way" without a second flag for the gate to forget
-- about. Two random uuids rather than gen_random_bytes: pgcrypto is an
-- extension this database does not have and does not need for one string.
create or replace function join_with_google(p_scholar text, p_email text)
returns boolean
language plpgsql security definer set search_path = public as $$
declare
  v_row roll_list;
begin
  if auth.uid() is null then
    return false;
  end if;

  select * into v_row from roll_list where scholar_no = p_scholar;
  if v_row.roll_no is null then
    return false;
  end if;

  -- status 'approved', not the 'pending' default. A pending joiner waits for
  -- an admin to recognise a name on a WhatsApp list; there is nothing for an
  -- admin to add to "the registrar put this scholar number in this section and
  -- Google says this person holds that institute address". Approval was always
  -- a check on identity, and this path already has a stronger one.
  insert into profiles (id, name, roll_no, phone, password, section_id,
                        status, email)
  values (auth.uid(), v_row.name, v_row.roll_no, null,
          replace(gen_random_uuid()::text || gen_random_uuid()::text, '-', ''),
          v_row.section_id,
          'approved', lower(p_email))
  on conflict (id) do nothing;

  return true;
end;
$$;

revoke all on function join_with_google(text, text) from public;
grant execute on function join_with_google(text, text) to authenticated;
