-- Which section each row belongs to, and the two functions that answer it.
--
-- This file adds a column and changes no policy. That split is deliberate:
-- the columns are inert on their own -- every existing query keeps returning
-- exactly what it returned before -- and 0042, which turns them into the
-- security boundary, can then be read as one page about one thing. Applying
-- this one and stopping is a database that has learned a new fact about its
-- rows and enforces nothing with it yet, which is a safe place to be between
-- two `psql -f` runs.
--
-- Every column goes on in three steps -- add nullable, backfill, then set the
-- default and the not-null -- rather than `add column ... not null default
-- my_section()`. Two reasons, both about a populated production database.
-- The one-liner would refuse outright on any table that already has rows,
-- because the default is evaluated per row and on the deploy's own connection
-- it is null. And even where it worked, adding a column with a non-constant
-- default rewrites the whole table under an exclusive lock; `set default`
-- after the fact touches nothing but the catalogue.
--
-- THE BACKFILL IS THE POINT. Section I is live. Every row in this database was
-- written by, for, or about Section I, because Section I is the only section
-- that has ever existed here -- so "every existing row is Section I" is not an
-- assumption, it is what the word meant until 0040 ran.

-- ---------------------------------------------------------------------------
-- profiles first, because the two helpers read it.

alter table profiles add column section_id uuid references sections(id);

update profiles set section_id =
  (select id from sections where name = 'I' and grad_year = 2030)
 where section_id is null;

-- The caller's own section, and the sentence every policy in 0042 ends with.
--
-- security definer for the same reason is_admin() and is_trusted() are, and it
-- is not a convenience: this function is called from the policy ON profiles,
-- so a version that obeyed row level security would consult that policy, which
-- would call this function, which would consult that policy. It returns one
-- uuid about the caller and nothing else -- a member can already see their own
-- section on their own profile row.
--
-- auth.uid() rather than anything else, because that is how every helper in
-- this schema has identified the caller since 0002: PostgREST puts the claims
-- in request.jwt.claims, notes.act_as sets the same GUC, and the tests set it
-- too, so a policy behaves identically in all three.
--
-- Null for somebody with no profile -- a stranger, a signup mid-flight. Null
-- is the fail-closed answer: `section_id = my_section()` is NULL, not true, so
-- a caller without a section matches no row anywhere.
create or replace function my_section() returns uuid
language sql stable security definer set search_path = public as $$
  select section_id from profiles where id = auth.uid()
$$;

-- And the section a row lands in when the writer did not say.
--
-- For a member this is exactly my_section(): their own, which is the only one
-- 0042's with-check clauses will accept from them anyway. The coalesce is for
-- the connection that OWNS these tables -- the CLI's bootstrap, the dev seeds,
-- the migration runner, the test suite's setup -- which has no auth.uid() at
-- all and is the connection every `insert into materials (...)` written before
-- today runs on. Without the fallback, every one of those inserts would have
-- to learn a new column on the day this file is applied.
--
-- The fallback is the oldest section, which on this install is Section I and
-- will be Section I for as long as this install exists. It is reachable ONLY
-- from a connection that bypasses row level security entirely, so it can widen
-- nothing: a member who somehow had no section would get Section I from the
-- default and then be refused by the with-check, because Section I is not
-- their my_section().
create or replace function default_section() returns uuid
language sql stable security definer set search_path = public as $$
  select coalesce(my_section(),
                  (select id from sections order by created_at, name limit 1))
$$;

alter table profiles
  alter column section_id set default default_section(),
  alter column section_id set not null;

-- ---------------------------------------------------------------------------
-- And the rest. One `do` block rather than fourteen copies of the same three
-- statements, because fourteen copies is fourteen chances to paste the wrong
-- table name into the second line and land a table's rows in nothing.
--
-- WHAT IS IN THIS LIST AND WHAT IS NOT is the whole security design, so it is
-- spelled out rather than derived:
--
--   profiles            -- who is in the section (above, by hand)
--   materials, lectures -- the library. Professors differ per section, so the
--                          notes do too; this is the spec's headline case.
--   announcements       -- the notice board, posted by that section's admin
--   posts               -- the wall and the confessions
--   doubts              -- questions and answers about that section's classes
--   messages            -- the subject room, which is talk between classmates
--   votes               -- who upvoted what, which names people
--   reports             -- what was reported, read by that section's admin
--   timetable           -- one student's week
--   section_timetable   -- the template a section's weeks are seeded from
--   attendance          -- one student's marks
--   cancelled_classes   -- a class called off, which is one section's Tuesday
--   invites             -- the door. A code an admin mints lets somebody into
--                          THAT admin's section, and 0044 is where that is
--                          actually honoured.
--
-- Institute-wide, deliberately NOT in this list, because MANIT has one of each
-- and a second copy per section would be a worse copy:
--
--   clubs, events, places, academic_calendar, subjects, subject_sets, sections
--
-- Also not in this list, for a different reason: announcement_reads and
-- bookmarks. Both are already private to one person in every direction -- the
-- only policies on them are `profile_id = auth.uid()` -- so there is no row on
-- either that anybody in another section can reach, with or without a column.
-- A section_id on them would be a column that is never read.
do $$
declare t text;
begin
  foreach t in array array[
    'materials', 'lectures', 'announcements', 'posts', 'doubts', 'messages',
    'votes', 'reports', 'timetable', 'section_timetable', 'attendance',
    'cancelled_classes', 'invites'
  ] loop
    execute format(
      'alter table %I add column section_id uuid references sections(id)', t);
    execute format(
      'update %I set section_id = (select id from sections '
      '  where name = ''I'' and grad_year = 2030) where section_id is null', t);
    execute format(
      'alter table %I alter column section_id set default default_section(), '
      '               alter column section_id set not null', t);
  end loop;
end $$;

-- ---------------------------------------------------------------------------
-- Two primary keys have to grow, because they say "one per section" and did
-- not know the word.
--
-- section_timetable is keyed (day, period): one grid, because there was one
-- section. Two sections both having a period 1 on Monday is the normal case,
-- and without the section in the key the second one to be typed in would
-- collide with the first.
alter table section_timetable drop constraint section_timetable_pkey;
alter table section_timetable add primary key (section_id, day, period);

-- Same shape: "the 9am lab on the 14th is off" is a fact about one section's
-- Tuesday, and Section II calling off their own must not be refused because
-- Section I already called off theirs.
alter table cancelled_classes drop constraint cancelled_classes_pkey;
alter table cancelled_classes add primary key (section_id, on_date, period, subject_code);

-- The read every scoped screen makes now starts with "my section", so the two
-- biggest lists get an index that starts there too.
create index on materials (section_id, subject_code, created_at desc);
create index on lectures  (section_id, subject_code, recorded_at desc);

-- No new table here, so no new grant: every table above already states its own
-- privileges in the migration that created it, and a column inherits the
-- table's. posts is the exception worth naming -- its grants are column-level
-- (0035) and section_id is deliberately NOT added to them. A policy may test a
-- column the caller cannot select, and a member has no business reading the
-- section off a confession when the section is their own by construction.
