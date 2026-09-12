-- The campus map's places. Schema here, rows in backend/dev/seed_campus.sql,
-- same split as clubs and the timetable above it.
--
-- The map itself needs a provider key that lives in the environment and is
-- never committed, so the table is the half of this feature that works today:
-- with no key the screen is still a list of where things are, which is what a
-- first-year in week one actually needs. The pins are the same rows.
--
-- `approx` is load-bearing and defaults to TRUE. These coordinates were read
-- off a public map, not surveyed, and most are the middle of a building rather
-- than its door. A pin that says "approximate" and is thirty metres out is
-- useful; the same pin presented as exact sends somebody to the wrong side of
-- a building and teaches them not to trust the screen. Anything set false here
-- has to have been checked on the ground.
create table places (
  slug     text primary key check (slug ~ '^[a-z0-9-]{2,48}$'),
  name     text not null check (length(btrim(name)) between 1 and 80),
  -- What it is, which is how the list groups itself. Free text with a check
  -- rather than an enum: adding a kind is a one-line migration either way and
  -- an enum cannot be dropped from under a running app.
  kind     text not null check (kind in ('academic', 'hostel', 'food', 'sport',
                                         'admin', 'gate', 'health', 'other')),
  lat      double precision check (lat between -90 and 90),
  lng      double precision check (lng between -180 and 180),
  approx   boolean not null default true,
  note     text not null default '',
  hidden   boolean not null default false
);

alter table places enable row level security;

create policy "members read places" on places for select
  using (is_approved() and (not hidden or is_admin()));

create policy "admins add places" on places for insert with check (is_admin());
create policy "admins edit places" on places for update
  using (is_admin()) with check (is_admin());

-- Unlike a notice or a doubt, a place is nobody's words -- deleting a pin
-- somebody typed the wrong coordinates into destroys nothing anyone wrote, so
-- this one table does get a real delete.
create policy "admins remove places" on places for delete using (is_admin());

grant select, insert, update, delete on places to authenticated;
