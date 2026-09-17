-- One row per person per day they opened the app: when they were last seen
-- that day, roughly how many minutes they were about, the last thing they
-- asked for and what they asked with. /super reads it; nobody else does.
--
-- Per day rather than one last_seen column on profiles, because the same table
-- then answers all of it -- last seen (max), active in the last 24 hours,
-- a daily-active chart, how many days somebody has actually turned up -- and
-- because a write to profiles goes through every trigger that guards a role.
--
-- Row level security on and NO policy, which is default deny: a member who
-- asks reads zero rows and can write none, and the owning connection, which
-- is the only thing that touches this, is not subject to it. The select grant
-- is there because every table with RLS states its grant (test_table_grants);
-- with no policy behind it, it reaches nothing. The revoke first is for the
-- test database, whose default privileges (see 0051) would otherwise hand
-- members insert and update as well.

create table activity_days (
  profile_id uuid not null references profiles on delete cascade,
  day        date not null default current_date,
  last_seen  timestamptz not null default now(),
  -- One per stamp, and the server stamps a person at most once a minute, so
  -- this is "minutes with a tap in them", near enough.
  minutes    integer not null default 1,
  last_path  text,
  agent      text,
  primary key (profile_id, day)
);

create index activity_days_last_seen on activity_days (last_seen desc);

revoke all on activity_days from public, anon, authenticated;
grant select on activity_days to authenticated;
alter table activity_days enable row level security;
