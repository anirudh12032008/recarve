-- Bookmarks: a note you want to find again, without scrolling the subject to
-- find it. Nobody else's business -- unlike a vote, saving something says
-- nothing about whether it is good, only that one student wants it back.
--
-- Keyed by (subject, title) rather than a lecture id, the same way the page
-- already holds a note (see db_lecture_id and db_meta): a static export and a
-- file the database has never adopted both have a title and a subject, and
-- neither has a guaranteed row in `lectures`. A bookmark that needed a lecture
-- row could not be set on a revision sheet, which has none.
create table bookmarks (
  profile_id   uuid not null references profiles(id) on delete cascade,
  subject_code text not null references subjects(code),
  title        text not null,
  created_at   timestamptz not null default now(),
  primary key (profile_id, subject_code, title)
);

alter table bookmarks enable row level security;

-- Your own, in every direction -- the same shape as a mark of attendance, and
-- for the same reason: this is private in the way a "yes I did this" is
-- private, not a fact about the class.
create policy "your own bookmarks" on bookmarks for all
  using (profile_id = auth.uid())
  with check (is_approved() and profile_id = auth.uid());

-- No update: a bookmark is on or off, exactly like a vote. Toggling one is a
-- delete and a fresh insert, never a row that changes what it points at.
grant select, insert, delete on bookmarks to authenticated;
