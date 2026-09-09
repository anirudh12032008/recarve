-- One student's week, so Home can say what today holds.
--
-- Per person, not per class: Section I splits into batches for the labs and no
-- two students swear their grid is the same. Nobody has typed the institute
-- timetable in yet either, and the PDF's column alignment is ambiguous enough
-- that a guessed grid would file lectures under the wrong subject in silence.
--
-- day 1 = Monday .. 6 = Saturday, which is exactly what JavaScript's getDay()
-- already calls them, so the phone needs no translation table. Sunday is 0 and
-- has no periods. Periods are numbered, not timed: the times are in the same
-- ambiguous PDF, and a wrong bell is worse than no bell.
create table timetable (
  profile_id   uuid not null references profiles(id) on delete cascade,
  day          int  not null check (day between 1 and 6),
  period       int  not null check (period between 1 and 8),
  subject_code text not null references subjects(code),
  primary key (profile_id, day, period)
);

alter table timetable enable row level security;

-- Your own and nothing else, in every direction. `for all` rather than four
-- policies: there is one rule here and splitting it four ways is four places
-- for the rule to drift.
create policy "your own timetable" on timetable for all
  using (profile_id = auth.uid())
  with check (is_approved() and profile_id = auth.uid());
