-- Clubs and events: the other two thirds of Campus.
--
-- Schema here, rows in backend/dev/seed_campus.sql. Same split as the
-- timetable, and for the same reason: the test database is rebuilt from these
-- files on every run, so a migration full of MANIT's real societies would make
-- every test in the suite start out with twenty-four clubs it did not ask for
-- and would have to filter around. The institute's list is data. It changes
-- when a society folds, not when the schema does.
--
-- A club is a directory entry and nothing more -- a name, what it does, and
-- where to find it. There is no membership table, no "join this club" button
-- and no count of members, because this app knows none of those things and a
-- number it cannot stand behind is worse than no number.
create table clubs (
  -- Slug rather than a uuid, so the seed file is re-runnable: `on conflict
  -- (slug) do update` lets somebody fix a description without inventing a
  -- second club of the same name.
  slug        text primary key check (slug ~ '^[a-z0-9-]{2,48}$'),
  name        text not null check (length(btrim(name)) between 1 and 80),
  blurb       text not null default '',
  category    text not null default '',
  -- What it carries, in the society's own words. An array, not a join table:
  -- nothing ever queries "every club tagged robotics", the tags are read on
  -- the club's own screen, and a tags table would be two policies and an index
  -- to store what a text[] stores.
  tags        text[] not null default '{}',
  -- Where to actually find them. Two fields because they are two things: a
  -- link is tapped, a contact is a person or a handle that is read.
  link        text,
  contact     text,
  -- Hidden, not deleted. A society that goes quiet for a semester comes back,
  -- and an admin who hid one has to be able to put it back.
  hidden      boolean not null default false,
  created_at  timestamptz not null default now()
);

alter table clubs enable row level security;

-- Every approved member reads the directory. A hidden one stays visible to the
-- admins who can unhide it, for the same reason a hidden announcement does:
-- Postgres checks the row an UPDATE leaves behind against the select policy
-- too, so hiding what you cannot then see is hiding you cannot undo.
create policy "members read the club directory" on clubs for select
  using (is_approved() and (not hidden or is_admin()));

create policy "admins add clubs" on clubs for insert with check (is_admin());
create policy "admins edit clubs" on clubs for update
  using (is_admin()) with check (is_admin());

-- No delete policy: hidden is the delete, and the row underneath survives it.

grant select, insert, update on clubs to authenticated;

-- ---------------------------------------------------------------------------
-- An event is a date with a name on it. That is the whole point of the table:
-- "this is coming up on this day", folded into the same Coming up list Home
-- already draws the institute's calendar into.
--
-- `society` is text and not a foreign key onto clubs, deliberately. Half the
-- real fests are run by bodies that are not in the directory -- Pravah runs
-- Vervana, Tooryanaad is a festival and a society at once -- and a nullable FK
-- plus a text fallback is two ways to say one thing, which is how they end up
-- disagreeing. The directory is a directory; this column is a credit line.
--
-- ends_on is null for a one-day event rather than equal to starts_on, so
-- "9 Sep" and "9 Sep to 9 Sep" cannot both exist and mean the same thing. The
-- reads below coalesce it, once, in the two places that care.
--
-- No RSVP, no attendee count, no "N going". There is nothing behind such a
-- number here and a fabricated one on a screen a hundred and ten people read
-- is worse than a blank space.
create table events (
  id         uuid primary key default gen_random_uuid(),
  title      text not null check (length(btrim(title)) between 1 and 120),
  society    text not null default '',
  starts_on  date not null,
  ends_on    date,
  venue      text not null default '',
  blurb      text not null default '',
  added_by   uuid not null references profiles(id),
  deleted_at timestamptz,
  created_at timestamptz not null default now(),
  constraint events_end_after_start check (ends_on is null or ends_on >= starts_on)
);

-- The one order every read asks for: what is still ahead, soonest first.
create index on events (starts_on) where deleted_at is null;

alter table events enable row level security;

-- Approved members read; a removed event stays readable to whoever can put it
-- back, same as everything else on this tab that is hidden rather than gone.
create policy "members read live events" on events for select
  using (is_approved()
         and (deleted_at is null or added_by = auth.uid() or is_admin()));

-- Trusted adds, and only in their own name -- the same bar as adding to the
-- library, because an event on Home's Coming up list is a thing a hundred and
-- ten people rearrange an afternoon around.
create policy "trusted members add events" on events for insert
  with check (is_trusted() and added_by = auth.uid());

-- Your own, or anybody's as an admin. Removing is setting deleted_at, so it
-- runs through here too.
create policy "your own events, or any as admin" on events for update
  using (is_admin() or (is_trusted() and added_by = auth.uid()))
  with check (is_admin() or (is_trusted() and added_by = auth.uid()));

grant select, insert, update on events to authenticated;
