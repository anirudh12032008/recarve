-- A fourth role, and the ordering said once instead of everywhere.
--
-- The class representative is the person the professors actually tell things
-- to. Until now the only way to let them put a notice on the board was to make
-- them an admin -- which also hands them the invite code, the pending queue,
-- every confession's author and the power to block their own classmates. That
-- is a lot of class to hand over for one notice board, so `cr` is the role
-- that carries the notice board and nothing else: everything a trusted member
-- may do, plus posting announcements.
--
-- The ordering is the whole design. `student < trusted < cr < admin` is a
-- ladder, and every question this schema asks about a role is "are you at
-- least X" -- never "are you one of these three strings". 0009 spelled the
-- ladder out as a list in is_trusted(), in the check constraint and in a
-- generated column, which is why adding one rung means editing all three. So
-- the list moves into role_rank() below and the three questions are asked
-- through it. The next role is one line in one function.
--
-- Safe against the live class: nothing here is destructive, nobody's role
-- changes, and the only rows rewritten are profiles' -- a hundred and ten of
-- them -- when the generated column is re-derived.

-- The ladder itself, and the only place it is written down. Immutable because
-- a check constraint and a stored generated column both need it to be, and
-- because it genuinely is: it maps a string to its position in a fixed list
-- and touches no table.
--
-- An unknown role has no position, so this returns null for one -- which is
-- what makes it a check constraint below, and what makes at_least() below
-- answer "no" rather than "yes" for a role that is not on the ladder at all.
--
-- THE ONE RULE FOR WHOEVER ADDS THE NEXT ROLE: the array here is copied into
-- profiles.trusted's stored value the moment a row is written, so changing it
-- is not enough on its own. Re-run the generated-column block near the bottom
-- of this file in the same migration, or the column keeps answering with the
-- old ladder for every row that does not happen to be rewritten. The test
-- named "the old boolean is role_rank spelled the old way" is what says so.
create or replace function role_rank(p_role text) returns int
language sql immutable set search_path = public as $$
  select array_position(array['student', 'trusted', 'cr', 'admin'], p_role);
$$;

-- Whether the person asking stands at or above a rung. Security definer for
-- the same reason is_admin() always has been: it reads profiles, and a policy
-- on profiles that called an RLS-obeying version of it would recurse into
-- itself. It returns one boolean about the caller and leaks nothing else.
--
-- status is tested here and not left to the callers, because status is the
-- other axis and it outranks the ladder entirely: a pending admin is pending,
-- a blocked one is blocked, and every helper 0009 wrote made the same test for
-- the same reason.
create or replace function at_least(p_role text) returns boolean
language sql stable security definer set search_path = public as $$
  select exists (
    select 1 from profiles
     where id = auth.uid() and status = 'approved'
       and role_rank(role) >= role_rank(p_role)
  );
$$;

-- The three named questions, now one line each. They stay as functions rather
-- than being replaced by at_least('...') at every call site: twenty policies
-- across fifteen migrations already say is_trusted(), those files are history
-- and are not rewritten, and "is_trusted()" reads better in a policy than its
-- own definition does.
--
-- is_admin() is deliberately at_least('admin') and not role = 'admin'. Admin
-- is the top rung today so the two are the same set; if something is ever
-- added above it, every admin-only policy in the schema should follow the
-- ladder up rather than quietly start excluding the new role.
create or replace function is_admin() returns boolean
language sql stable security definer set search_path = public as $$
  select at_least('admin');
$$;

create or replace function is_trusted() returns boolean
language sql stable security definer set search_path = public as $$
  select at_least('trusted');
$$;

-- New, and the only reason this migration exists. Named for the rung rather
-- than for the notice board, because the next thing a cr is trusted with will
-- want to ask the same question.
create or replace function is_cr() returns boolean
language sql stable security definer set search_path = public as $$
  select at_least('cr');
$$;

grant execute on function role_rank(text) to authenticated;
grant execute on function at_least(text) to authenticated;
grant execute on function is_cr() to authenticated;
-- Restated rather than assumed: 0009 granted is_trusted() and `create or
-- replace` above keeps existing grants, but this file has to be enough to
-- rebuild the schema on its own.
grant execute on function is_admin() to authenticated;
grant execute on function is_trusted() to authenticated;

-- The column's own guard, widened by being taught the ladder instead of a
-- fourth string. 0009 wrote this inline, so Postgres named it itself; dropped
-- by that name with `if exists` so this file survives a database where it was
-- already replaced, and re-added under the same name so the next person finds
-- it where they expect.
--
-- `is not null` is the whole test: role_rank() answers null for a role that is
-- not on the ladder, which is exactly the set this constraint exists to refuse.
alter table profiles drop constraint if exists profiles_role_check;
alter table profiles add constraint profiles_role_check
  check (role_rank(role) is not null);

-- profiles.trusted is 0009's compatibility shim -- the boolean the old schema
-- had, kept as a generated column so that nothing which reads it can ever
-- disagree with role. One thing still reads it, and it is not decorative:
-- force_pending_for_untrusted() in 0004 holds a first-time uploader's file at
-- 'pending' until an admin publishes it. Left as `role in ('trusted','admin')`
-- a class representative's uploads would all arrive pending and nobody would
-- understand why.
--
-- Dropped and re-added rather than altered: `alter column ... set expression`
-- is Postgres 17 and this has to apply to whatever the hosted database is
-- running. Nothing depends on the column -- 0009 already rebuilt the two
-- policies that used to read it, and the one remaining reader is a plpgsql
-- function body, which is not a dependency Postgres tracks -- so the drop
-- takes nothing with it. The stored values are recomputed for every row on the
-- way back in, which is the point.
alter table profiles drop column trusted;
alter table profiles
  add column trusted boolean
  generated always as (role_rank(role) >= role_rank('trusted')) stored;

-- And the notice board itself: the one power that moves down a rung.
--
-- Dropped by name and rebuilt rather than edited, because a policy cannot be
-- altered in place. The names change with the rule -- a policy called "admins
-- post announcements" that lets a cr post is a lie the next reader has to
-- find out the hard way.
--
-- author_id = auth.uid() stays exactly as 0023 wrote it: the row says who
-- wrote it and a hundred and ten people read that name, so nobody posts under
-- somebody else's. And "their own, and only their own" for the edit, for the
-- same reason it was true of two admins -- a cr editing the admin's notice
-- would leave the wrong name on the changed words.
drop policy "admins post announcements" on announcements;
create policy "class reps post announcements" on announcements for insert
  with check (is_cr() and author_id = auth.uid());

drop policy "admins edit their own announcements" on announcements;
create policy "class reps edit their own announcements" on announcements for update
  using (is_cr() and author_id = auth.uid())
  with check (is_cr() and author_id = auth.uid());

-- No delete policy, still. 0023's reasoning has not changed: the app hides
-- with deleted_at, and nothing reachable from a session may destroy the row
-- underneath it.
--
-- Restated from 0031 so this file alone can rebuild what it touches. The read
-- policy on announcements is untouched -- the board was never gated on role,
-- and a student who cannot post one still has to be told the lab has moved.
grant select, insert, update on announcements to authenticated;
