-- The institute timetable for B.Tech I Sem 2026-27, all ten sections.
-- Typed off "11FINAL Time Table-I Sem BTech August 2026-27", classes w.e.f.
-- 24/8/2026. Data, not schema: applied to the live database only, never to the
-- test one. Supersedes seed_section_i.sql, whose Section I grid is reproduced
-- here unchanged, period for period.
--
-- THREE THINGS THE PDF SAYS THAT ONE GRID PER SECTION CANNOT:
--
-- 1. Labs run in two batches. Where the two batches have the same lab at
--    different hours -- "Chem. Lab-I" at 11 and "Chem. Lab-II" at 2:30 -- both
--    slots are here, because both are that section's afternoon and the student
--    deletes the one that is not theirs. That is what seed_section_i.sql has
--    always done and what the per-student timetable is for.
-- 2. Where one slot holds two DIFFERENT labs -- "Phy.Lab-I/EML-II", Batch I in
--    the physics lab while Batch II is in NTB-116 -- only one subject fits in a
--    (section, day, period) key, and the slot takes Batch I's. Batch II has
--    the same two labs the same day the other way round, so the week is right
--    for both and only the order is wrong for one.
-- 3. LSM and PHE are alternatives a student picks between, and the timetable
--    gives them different slots. Both are on every MT section's week here for
--    the same reason as the labs.
--
-- Section D really is timetabled four periods of Engineering Mechanics against
-- the Scheme's three (Mon II, Tue II, Wed V, Thu III). Every other section in
-- both groups matches its scheme page exactly. Transcribed as printed; if the
-- institute meant three, the institute has to say so.
--
-- Saturday is empty on purpose: the only thing on it is NCC, 9-11am, which is
-- an additional course a few students take and not this section's week.
begin;

delete from section_timetable
 where section_id in (select id from sections where grad_year = 2030);

insert into section_timetable (section_id, day, period, subject_code)
select s.id, v.day, v.period, v.code
  from (values
    -- Section A
    ('A',1,1,'CS1124'), ('A',1,2,'CS1124'), ('A',1,5,'CE1103'), ('A',1,6,'PY1102'), ('A',1,7,'MC1101'),
    ('A',2,1,'PY1122'), ('A',2,2,'PY1122'), ('A',2,3,'MC1101'), ('A',2,4,'PY1102'), ('A',2,5,'CE1121'),
    ('A',2,6,'CE1121'), ('A',2,7,'CE1103'),
    ('A',3,1,'PY1102'), ('A',3,2,'CE1103'), ('A',3,3,'HS1128'), ('A',3,4,'HS1128'), ('A',3,5,'MC1101'),
    ('A',3,6,'HS1106'), ('A',3,7,'CS1105'),
    ('A',4,1,'CS1105'), ('A',4,2,'MC1101'), ('A',4,3,'HS1128'), ('A',4,4,'HS1128'), ('A',4,6,'SA1142'),
    ('A',4,7,'SA1142'),
    ('A',5,2,'ME1104'), ('A',5,3,'ME1123'), ('A',5,4,'ME1123'), ('A',5,5,'SA1141'), ('A',5,6,'SA1141'),
    -- Section B
    ('B',1,1,'MC1101'), ('B',1,2,'PY1102'), ('B',1,3,'CE1103'), ('B',1,4,'CS1105'), ('B',1,5,'HS1128'),
    ('B',1,6,'HS1128'),
    ('B',2,1,'CE1103'), ('B',2,2,'ME1104'), ('B',2,3,'ME1123'), ('B',2,4,'ME1123'), ('B',2,5,'HS1106'),
    ('B',2,6,'PY1102'),
    ('B',3,1,'PY1122'), ('B',3,2,'PY1122'), ('B',3,3,'MC1101'), ('B',3,4,'CE1103'), ('B',3,5,'CE1121'),
    ('B',3,6,'CE1121'),
    ('B',4,1,'CS1124'), ('B',4,2,'CS1124'), ('B',4,3,'CS1105'), ('B',4,4,'MC1101'), ('B',4,6,'SA1142'),
    ('B',4,7,'SA1142'),
    ('B',5,1,'HS1128'), ('B',5,2,'HS1128'), ('B',5,3,'PY1102'), ('B',5,4,'MC1101'), ('B',5,5,'SA1141'),
    ('B',5,6,'SA1141'),
    -- Section C
    ('C',1,1,'PY1122'), ('C',1,2,'PY1122'), ('C',1,3,'HS1128'), ('C',1,4,'HS1128'), ('C',1,5,'CE1121'),
    ('C',1,6,'CE1121'),
    ('C',2,1,'CS1124'), ('C',2,2,'CS1124'), ('C',2,3,'PY1102'), ('C',2,4,'MC1101'), ('C',2,6,'SA1142'),
    ('C',2,7,'SA1142'),
    ('C',3,1,'PY1102'), ('C',3,2,'CE1103'), ('C',3,3,'CS1105'), ('C',3,4,'HS1106'), ('C',3,6,'MC1101'),
    ('C',3,7,'CE1103'),
    ('C',4,2,'ME1104'), ('C',4,3,'ME1123'), ('C',4,4,'ME1123'), ('C',4,5,'PY1102'), ('C',4,6,'MC1101'),
    ('C',4,7,'CS1105'),
    ('C',5,1,'MC1101'), ('C',5,2,'CE1103'), ('C',5,3,'HS1128'), ('C',5,4,'HS1128'), ('C',5,5,'SA1141'),
    ('C',5,6,'SA1141'),
    -- Section D
    ('D',1,1,'MC1101'), ('D',1,2,'CE1103'), ('D',1,3,'CS1105'), ('D',1,4,'PY1102'), ('D',1,5,'MC1101'),
    ('D',1,6,'HS1106'),
    ('D',2,1,'PY1102'), ('D',2,2,'CE1103'), ('D',2,3,'HS1128'), ('D',2,4,'HS1128'), ('D',2,6,'SA1142'),
    ('D',2,7,'SA1142'),
    ('D',3,1,'MC1101'), ('D',3,2,'ME1104'), ('D',3,3,'ME1123'), ('D',3,4,'ME1123'), ('D',3,5,'CE1103'),
    ('D',3,6,'HS1128'), ('D',3,7,'HS1128'),
    ('D',4,1,'PY1122'), ('D',4,2,'PY1122'), ('D',4,3,'CE1103'), ('D',4,4,'MC1101'), ('D',4,5,'CE1121'),
    ('D',4,6,'CE1121'),
    ('D',5,1,'CS1124'), ('D',5,2,'CS1124'), ('D',5,3,'PY1102'), ('D',5,4,'CS1105'), ('D',5,5,'SA1141'),
    ('D',5,6,'SA1141'),
    -- Section E
    ('E',1,2,'ME1104'), ('E',1,3,'ME1123'), ('E',1,4,'ME1123'), ('E',1,6,'MC1101'), ('E',1,7,'CE1103'),
    ('E',2,1,'CE1103'), ('E',2,2,'MC1101'), ('E',2,3,'PY1102'), ('E',2,4,'CS1105'), ('E',2,6,'SA1142'),
    ('E',2,7,'SA1142'),
    ('E',3,1,'CS1124'), ('E',3,2,'CS1124'), ('E',3,3,'PY1102'), ('E',3,4,'MC1101'), ('E',3,5,'CS1105'),
    ('E',3,6,'CE1103'), ('E',3,7,'HS1106'),
    ('E',4,1,'HS1128'), ('E',4,2,'HS1128'), ('E',4,3,'MC1101'), ('E',4,4,'PY1102'), ('E',4,5,'HS1128'),
    ('E',4,6,'HS1128'),
    ('E',5,1,'PY1122'), ('E',5,2,'PY1122'), ('E',5,3,'CE1121'), ('E',5,4,'CE1121'), ('E',5,5,'SA1141'),
    ('E',5,6,'SA1141'),
    -- Section F
    ('F',1,1,'MC1101'), ('F',1,2,'CY1107'), ('F',1,3,'EE1108'), ('F',1,4,'BS1111'), ('F',1,5,'ME1109'),
    ('F',1,6,'ME1127'), ('F',1,7,'ME1127'),
    ('F',2,1,'CY1110'), ('F',2,2,'HS1112'), ('F',2,5,'CY1107'), ('F',2,6,'EE1108'), ('F',2,7,'MC1101'),
    ('F',3,1,'CY1110'), ('F',3,2,'MC1101'), ('F',3,3,'EE1125'), ('F',3,4,'EE1125'), ('F',3,5,'EE1125'),
    ('F',3,6,'EE1125'),
    ('F',4,1,'MC1101'), ('F',4,2,'BS1111'), ('F',4,5,'HS1112'), ('F',4,6,'EE1108'), ('F',4,7,'CY1107'),
    ('F',5,1,'CY1126'), ('F',5,2,'CY1126'), ('F',5,3,'CY1126'), ('F',5,4,'CY1126'), ('F',5,6,'SA1143'),
    ('F',5,7,'SA1143'),
    -- Section G
    ('G',1,1,'EE1108'), ('G',1,2,'HS1112'), ('G',1,3,'CY1126'), ('G',1,4,'CY1126'), ('G',1,5,'CY1126'),
    ('G',1,6,'CY1126'), ('G',1,7,'MC1101'),
    ('G',2,1,'BS1111'), ('G',2,2,'CY1107'), ('G',2,3,'EE1108'), ('G',2,4,'CY1110'), ('G',2,6,'MC1101'),
    ('G',2,7,'HS1112'),
    ('G',3,1,'CY1107'), ('G',3,2,'MC1101'), ('G',3,5,'ME1127'), ('G',3,6,'ME1127'),
    ('G',4,2,'CY1110'), ('G',4,3,'MC1101'), ('G',4,4,'BS1111'), ('G',4,5,'CY1107'), ('G',4,6,'ME1109'),
    ('G',4,7,'EE1108'),
    ('G',5,1,'EE1125'), ('G',5,2,'EE1125'), ('G',5,3,'EE1125'), ('G',5,4,'EE1125'), ('G',5,6,'SA1143'),
    ('G',5,7,'SA1143'),
    -- Section H
    ('H',1,2,'HS1112'), ('H',1,3,'MC1101'), ('H',1,4,'EE1108'), ('H',1,5,'CY1110'), ('H',1,6,'CY1107'),
    ('H',2,1,'ME1127'), ('H',2,2,'ME1127'), ('H',2,3,'EE1125'), ('H',2,4,'EE1125'), ('H',2,5,'EE1125'),
    ('H',2,6,'EE1125'), ('H',2,7,'HS1112'),
    ('H',3,3,'MC1101'), ('H',3,4,'BS1111'), ('H',3,5,'CY1110'), ('H',3,6,'EE1108'), ('H',3,7,'CY1107'),
    ('H',4,1,'MC1101'), ('H',4,3,'CY1126'), ('H',4,4,'CY1126'), ('H',4,5,'CY1126'), ('H',4,6,'CY1126'),
    ('H',4,7,'BS1111'),
    ('H',5,1,'ME1109'), ('H',5,2,'EE1108'), ('H',5,3,'MC1101'), ('H',5,4,'CY1107'), ('H',5,6,'SA1143'),
    ('H',5,7,'SA1143'),
    -- Section I
    ('I',1,1,'CY1107'), ('I',1,2,'MC1101'), ('I',1,3,'EE1125'), ('I',1,4,'EE1125'), ('I',1,5,'EE1125'),
    ('I',1,6,'EE1125'), ('I',1,7,'HS1112'),
    ('I',2,1,'CY1110'), ('I',2,2,'EE1108'), ('I',2,3,'CY1126'), ('I',2,4,'CY1126'), ('I',2,5,'CY1126'),
    ('I',2,6,'CY1126'),
    ('I',3,2,'HS1112'), ('I',3,3,'CY1107'), ('I',3,4,'ME1109'), ('I',3,5,'MC1101'), ('I',3,6,'EE1108'),
    ('I',4,1,'BS1111'), ('I',4,2,'CY1107'), ('I',4,3,'EE1108'), ('I',4,4,'BS1111'), ('I',4,5,'CY1110'),
    ('I',4,6,'MC1101'),
    ('I',5,1,'MC1101'), ('I',5,3,'ME1127'), ('I',5,4,'ME1127'), ('I',5,6,'SA1143'), ('I',5,7,'SA1143'),
    -- Section J
    ('J',1,3,'BS1111'), ('J',1,4,'MC1101'), ('J',1,5,'CY1110'), ('J',1,6,'EE1108'), ('J',1,7,'HS1112'),
    ('J',2,3,'MC1101'), ('J',2,4,'CY1107'), ('J',2,5,'CY1110'), ('J',2,6,'EE1108'), ('J',2,7,'ME1109'),
    ('J',3,1,'MC1101'), ('J',3,2,'HS1112'), ('J',3,3,'CY1126'), ('J',3,4,'CY1126'), ('J',3,5,'CY1126'),
    ('J',3,6,'CY1126'), ('J',3,7,'CY1107'),
    ('J',4,1,'ME1127'), ('J',4,2,'ME1127'), ('J',4,3,'EE1125'), ('J',4,4,'EE1125'), ('J',4,5,'EE1125'),
    ('J',4,6,'EE1125'), ('J',4,7,'BS1111'),
    ('J',5,2,'MC1101'), ('J',5,3,'CY1107'), ('J',5,4,'EE1108'), ('J',5,6,'SA1143'), ('J',5,7,'SA1143')
       ) as v(name, day, period, code)
  join sections s on s.name = v.name and s.grad_year = 2030;

-- And everybody already in a section gets what the template says. New members
-- are seeded by the trigger 0043 rewrote; these are the ones who joined first.
insert into timetable (profile_id, section_id, day, period, subject_code)
select p.id, p.section_id, s.day, s.period, s.subject_code
  from profiles p join section_timetable s on s.section_id = p.section_id
on conflict (profile_id, day, period) do nothing;

commit;
