-- A section is the unit this whole app has silently been one of.
--
-- Everything here -- the library, the notice board, the wall, the timetable --
-- has meant "Section I" since the first line was written, and nothing said so
-- anywhere. One install was one section, and the word appeared only in prose
-- and in `section_timetable`, a table whose name is singular because there was
-- only ever one. This file is where the word becomes a row.
--
-- It creates rows and nothing else: no column on any existing table changes
-- here, no policy changes here. 0041 hangs the section_id columns off this,
-- 0042 rewrites the policies that are the actual security boundary, and each
-- of those is safe to run by hand, on its own, against the live database --
-- which is how they will be run.
--
-- grad_year is in the key because a section name repeats. "Section I" is a
-- different hundred and ten people every intake, and the day the 2031s arrive
-- their Section I must be able to exist beside the 2030s' without either one
-- being renamed. Displayed as "Section I '30".

-- ---------------------------------------------------------------------------
-- Subject sets: the curriculum split, which is a real thing at MANIT and not
-- a generalisation invented here.
--
-- In semester 1 sections A-E follow one curriculum and F-J another, and the
-- two swap in semester 2. That is the whole of it: a named list of subjects a
-- section points at, and a swap is one section pointing somewhere else. There
-- is deliberately no semester column, no rollover job and no calendar: the
-- owner changes a pointer twice a year, by hand, and anything more is a
-- machine built for a decision that happens twice.
create table subject_sets (
  id   uuid primary key default gen_random_uuid(),
  name text not null unique
);

create table sections (
  id             uuid primary key default gen_random_uuid(),
  name           text not null,                        -- 'I'
  grad_year      int  not null,                        -- 2030
  subject_set_id uuid references subject_sets(id),
  created_at     timestamptz not null default now(),
  unique (name, grad_year)
);

-- A subject belongs to one set. Nullable, because 0040 has to be applyable to
-- a populated database before the backfill two statements below has run, and
-- because a subject nobody has filed yet is a better state than a subject
-- filed under a guess.
alter table subjects add column set_id uuid references subject_sets(id);

-- ---------------------------------------------------------------------------
-- The backfill, which is not optional: Section I is live, with real students,
-- real uploads and a real notice board, and every one of those rows has to end
-- up inside a section on the way past. This is the row they all land in.
--
-- Section I follows Set B. That is the owner's fact, not a default: the twelve
-- subjects in this database are the ones Section I is actually taught, and
-- they are Set B's.
insert into subject_sets (name) values ('Set A'), ('Set B')
  on conflict (name) do nothing;

update subjects set set_id = (select id from subject_sets where name = 'Set B')
 where set_id is null;

-- 2030 is the graduation year of the first-years this install was built for --
-- a four-year B.Tech intake that arrived in 2026.
insert into sections (name, grad_year, subject_set_id)
select 'I', 2030, (select id from subject_sets where name = 'Set B')
 where not exists (select 1 from sections where name = 'I' and grad_year = 2030);

-- SET A STARTS EMPTY, and that is a deliberate departure from the design note,
-- which asks for every Set B subject to be copied into Set A as a starting
-- point. It cannot be done, and this is where to say why rather than leaving
-- the next reader to work it out from a primary key.
--
-- A subject's code IS its primary key, and nine other tables hold a foreign
-- key onto it. A row can therefore carry exactly one set_id, and the same code
-- cannot appear in two sets -- a literal copy is a primary key violation. The
-- only copy that inserts is one with a mangled code, 'MC1101-A', and that is
-- worse than nothing three times over: notes.py keys a hardcoded table of
-- names and search aliases off these codes, so a code it has never heard of
-- has no name to render; test_profiles.py asserts the exact twelve, on the
-- owning connection where no policy filters them; and every one of those rows
-- would be a placeholder the owner has to find and delete rather than correct.
--
-- What the copy was FOR is still true: the owner needs Set A to be fillable
-- and a section to be pointable at it. Both work with it empty. Set A's real
-- contents are twelve different codes taught by different professors -- that
-- is what a curriculum split IS -- so the owner is typing them in either way.
--
-- A section pointed at an empty Set A shows its members an empty subject list,
-- which is the honest reading of "nobody has entered this curriculum yet".

alter table subject_sets enable row level security;
alter table sections     enable row level security;

-- No select policy in this file, on purpose. The policy that belongs here is
-- "the section you are in", and the column that sentence needs -- and the
-- my_section() helper that reads it -- do not exist until 0041. Row level
-- security with no policy denies every row to every member, which is the
-- direction to be wrong in for the minutes between these two files being
-- applied by hand. 0042 writes the real ones.
--
-- The grants are still stated here, because a table's own migration is where
-- its privileges are written down (0031) and because a table with policies and
-- no grant is how a student got "permission denied for table doubts" in
-- production. Select only: a member reads which section they are in; creating
-- one is the super admin's, on a connection that owns these tables.
grant select on sections, subject_sets to authenticated;
