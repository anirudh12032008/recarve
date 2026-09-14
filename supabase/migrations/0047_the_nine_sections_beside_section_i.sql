-- The other nine sections, and the curriculum five of them follow.
--
-- MANIT's first year is ten sections, A to J, split into two groups that are
-- taught different subjects and swap in semester 2: Group-MT is A-E and
-- Group-ST is F-J. This database has known the shape of that since 0040 --
-- `subject_sets`, and a `sections` table with one row in it -- and has known
-- none of the content: Set A was empty because nobody had typed Group-MT's
-- codes in, and nine of the ten sections did not exist.
--
-- Both come off the same page of the institute timetable for I Sem 2026-27,
-- the Scheme, which is also where 0001's twelve codes came from.
--
-- Set A is Group-MT and Set B is Group-ST. The names now read as luck rather
-- than design -- Set A really is the set Sections A to E follow -- and they
-- are left alone because they are what 0040 wrote and what the tests look up.

-- ---------------------------------------------------------------------------
-- Group-MT's codes. Twelve new rows; MC1101 and NC1151 are already here and
-- are taught to both groups, which is what 0046 exists for.
insert into subjects (code, name, sort) values
  ('PY1102', 'Physics',                              13),
  ('CE1103', 'Engineering Mechanics',                14),
  ('ME1104', 'Engineering Graphics',                 15),
  ('CS1105', 'Computer Programming & Problem Solving', 16),
  ('HS1106', 'Communication Skill',                  17),
  ('CE1121', 'Engineering Mechanics Laboratory',     18),
  ('PY1122', 'Physics Laboratory',                   19),
  ('ME1123', 'Engineering Graphics Laboratory',      20),
  ('CS1124', 'Computer Programming Laboratory',      21),
  ('HS1128', 'Language Laboratory',                  22),
  ('SA1141', 'Life Skill Management',                23),
  ('SA1142', 'Physical Education',                   24)
on conflict (code) do nothing;

-- LSM and PHE are two codes rather than one because the timetable gives them
-- two different slots -- LSM on Friday afternoon for every MT section, PHE on
-- the section's own afternoon -- and a student takes one of them. Set B's
-- SA1143 merges NSS, Yoga and UHV into a single code for the opposite reason:
-- there all three share one Friday block, so one row says the same thing.

insert into subject_set_members (set_id, subject_code)
select (select id from subject_sets where name = 'Set A'), code
  from (values ('MC1101'), ('PY1102'), ('CE1103'), ('ME1104'), ('CS1105'),
               ('HS1106'), ('CE1121'), ('PY1122'), ('ME1123'), ('CS1124'),
               ('HS1128'), ('SA1141'), ('SA1142'), ('NC1151')) as v(code)
on conflict do nothing;

-- NC1151 is on both scheme pages and 0040 filed it under Set B alone.
insert into subject_set_members (set_id, subject_code)
values ((select id from subject_sets where name = 'Set B'), 'NC1151')
on conflict do nothing;

-- ---------------------------------------------------------------------------
-- The sections. 2030 for the same reason 0040 used it: a four-year B.Tech
-- intake that arrived in 2026. Section I is already here and is not touched --
-- it has a hundred and ten real people in it.
insert into sections (name, grad_year, subject_set_id)
select v.name, 2030,
       (select id from subject_sets where name = v.set)
  from (values ('A', 'Set A'), ('B', 'Set A'), ('C', 'Set A'), ('D', 'Set A'),
               ('E', 'Set A'), ('F', 'Set B'), ('G', 'Set B'), ('H', 'Set B'),
               ('I', 'Set B'), ('J', 'Set B')) as v(name, "set")
 where not exists (select 1 from sections s
                    where s.name = v.name and s.grad_year = 2030);

-- No invites are minted here. A section with no code has no door, which is the
-- honest state for nine sections nobody has been told about yet; /super mints
-- the first one when somebody is ready to be let in.
--
-- No timetables either: periods are data, seeded by
-- backend/dev/seed_timetables.sql, the same division 0008 and 0020 set.
