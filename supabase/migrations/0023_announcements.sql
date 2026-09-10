-- The notice board: the first real thing on Campus.
--
-- Two tables, because "have I read this" is a fact about a person and not
-- about the notice. Storing it here rather than in localStorage is the whole
-- point of the second one: a student reads a notice on their phone in the
-- corridor and opens the app on a laptop that evening, and it must not be new
-- again. localStorage would also make "unread" mean nothing after a cleared
-- cache, which is exactly what happened to the join cookie.
--
-- Deleting hides. An announcement goes out to a hundred and ten people at
-- once, so a typo posted at midnight is a thing somebody wants back; a row
-- that is gone is a row nobody can give back. deleted_at is the whole of it,
-- and the select policy below is what makes it a delete for everyone except
-- the admin who did it -- who can still see it, and put it back.
create table announcements (
  id         uuid primary key default gen_random_uuid(),
  author_id  uuid not null references profiles(id),
  title      text not null check (length(btrim(title)) between 1 and 120),
  body       text not null default '',
  pinned     boolean not null default false,
  deleted_at timestamptz,
  created_at timestamptz not null default now(),
  updated_at timestamptz
);

create table announcement_reads (
  announcement_id uuid not null references announcements(id) on delete cascade,
  profile_id      uuid not null references profiles(id) on delete cascade,
  read_at         timestamptz not null default now(),
  primary key (announcement_id, profile_id)
);

-- The one order the app ever asks for: pinned first, newest first, live only.
create index on announcements (pinned desc, created_at desc) where deleted_at is null;

alter table announcements      enable row level security;
alter table announcement_reads enable row level security;

-- Everybody approved reads the board. That is the point of a notice board, and
-- it is deliberately not gated on role: a student who cannot upload a thing
-- still has to be told when the lab is moved.
create policy "members read live announcements" on announcements for select
  using (is_approved() and (deleted_at is null or author_id = auth.uid()));

-- Posting is an admin act, and this is where a student is refused whatever the
-- app's screens show. author_id = auth.uid() so an admin cannot post in
-- somebody else's name -- the row says who wrote it and the class reads that.
create policy "admins post announcements" on announcements for insert
  with check (is_admin() and author_id = auth.uid());

-- Their own, and only their own. An admin editing another admin's notice would
-- leave the wrong name on the changed words.
create policy "admins edit their own announcements" on announcements for update
  using (is_admin() and author_id = auth.uid())
  with check (is_admin() and author_id = auth.uid());

-- No delete policy at all, on purpose: the app hides with deleted_at, and
-- nothing reachable from a session may destroy the row underneath it.

create policy "read your own read marks" on announcement_reads for select
  using (profile_id = auth.uid());
create policy "mark as read as yourself" on announcement_reads for insert
  with check (is_approved() and profile_id = auth.uid());
