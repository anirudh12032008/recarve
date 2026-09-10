-- MANIT Section I, B.Tech I Sem 2026-27 (Group-ST timetable, w.e.f. 24/8/2026).
-- Data, not schema: applied to the live database only, never to the test one.
delete from section_timetable;
insert into section_timetable (day, period, subject_code) values
  -- Monday
  (1,1,'CY1107'), (1,2,'MC1101'), (1,3,'EE1125'), (1,4,'EE1125'),
  (1,5,'EE1125'), (1,6,'EE1125'), (1,7,'HS1112'),
  -- Tuesday
  (2,1,'CY1110'), (2,2,'EE1108'), (2,3,'CY1126'), (2,4,'CY1126'),
  (2,5,'CY1126'), (2,6,'CY1126'),
  -- Wednesday
  (3,2,'HS1112'), (3,3,'CY1107'), (3,4,'ME1109'), (3,5,'MC1101'), (3,6,'EE1108'),
  -- Thursday
  (4,1,'BS1111'), (4,2,'CY1107'), (4,3,'EE1108'), (4,4,'BS1111'),
  (4,5,'CY1110'), (4,6,'MC1101'),
  -- Friday
  (5,1,'MC1101'), (5,3,'ME1127'), (5,4,'ME1127'), (5,6,'SA1143'), (5,7,'SA1143');

insert into timetable (profile_id, day, period, subject_code)
select p.id, s.day, s.period, s.subject_code from profiles p cross join section_timetable s
on conflict (profile_id, day, period) do nothing;
