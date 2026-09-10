-- The institute's own calendar, so the app stops inventing classes that the
-- institute already said would not happen.
--
-- Transcribed from MANIT/Academic/2026/1685 dated 14.07.2026 (First Semester
-- 2026-27) and its Annexure "A" (Closed Holidays for 2026). Every holiday's
-- weekday was checked against the real 2026 calendar before this was written.
--
-- 17 of the 89 non-Sunday days between the first and last day of UG classes are
-- days with no class -- 19% of the semester. Without this table the timetable
-- confidently shows a full Monday on Ganesh Chaturthi.
--
-- WHY THIS DOES NOT TOUCH THE ATTENDANCE ARITHMETIC: it does not need to. A
-- mark is one of three states and only two of them are rows, so a holiday that
-- nobody marks already counts in neither the numerator nor the denominator.
-- The percentage is correct on a holiday whether or not this table exists. What
-- this table fixes is the screen: not prompting somebody to mark a class that
-- the institute cancelled back in July.
--
-- `teaching` is the load-bearing column and it is not the same as "is this a
-- special day". Mini tests run *during class hours* with no separate timetable,
-- so classes happen and teaching stays true. The mid-sem break and the exam
-- windows are the opposite. Read the source line before flipping one of these.
create table academic_calendar (
  starts_on date not null,
  ends_on   date not null,
  title     text not null,
  kind      text not null check (kind in ('holiday', 'break', 'exam', 'milestone')),
  -- Do ordinary classes run on these days?
  teaching  boolean not null,
  -- Shown on the countdown, or only used to grey out the timetable?
  notable   boolean not null default false,
  primary key (starts_on, title),
  check (ends_on >= starts_on)
);

-- The one read the countdown makes: the next notable thing from today.
create index on academic_calendar (starts_on) where notable;

alter table academic_calendar enable row level security;

-- The same dates for all 110 of them, and nothing private in it. Approved
-- members read it; nobody writes it from the app -- it comes from a notice the
-- institute publishes once a semester, so it arrives by migration.
create policy "members read the calendar" on academic_calendar for select
  using (is_approved());

grant select on academic_calendar to authenticated;

insert into academic_calendar (starts_on, ends_on, title, kind, teaching, notable) values
  ('2026-08-24', '2026-08-24', 'First day of classes',            'milestone', true,  false),

  -- Closed holidays falling inside the teaching term. Diwali is a Sunday, so it
  -- is here for completeness and changes nothing.
  ('2026-08-26', '2026-08-26', 'Milad-un-Nabi',                   'holiday',   false, false),
  ('2026-09-04', '2026-09-04', 'Janmashtami',                     'holiday',   false, false),
  ('2026-09-14', '2026-09-14', 'Ganesh Chaturthi',                'holiday',   false, false),
  ('2026-10-02', '2026-10-02', 'Gandhi Jayanti',                  'holiday',   false, false),
  ('2026-10-20', '2026-10-20', 'Dussehra',                        'holiday',   false, false),
  ('2026-11-08', '2026-11-08', 'Diwali',                          'holiday',   false, false),
  ('2026-11-24', '2026-11-24', 'Guru Nanak Jayanti',              'holiday',   false, false),

  -- Attendance is displayed twice before the list that matters.
  ('2026-09-25', '2026-09-25', 'Attendance displayed',            'milestone', true,  true),
  ('2026-10-26', '2026-10-26', 'Attendance displayed',            'milestone', true,  true),

  -- "No separate TimeTable shall be issued & Examinations to be conducted
  -- during class hours only" -- so classes DO run through this window.
  ('2026-09-28', '2026-10-06', 'Mini tests and quizzes',          'exam',      true,  true),

  ('2026-10-19', '2026-10-23', 'Mid-semester break',              'break',     false, true),
  ('2026-10-27', '2026-11-03', 'Mid-term examinations',           'exam',      false, true),
  ('2026-11-06', '2026-11-06', 'Mid-term marks displayed',        'milestone', true,  false),
  ('2026-11-09', '2026-11-13', 'Feedback and seminar exams',      'milestone', true,  false),
  ('2026-11-16', '2026-11-17', 'Elective choices for next term',  'milestone', true,  true),

  -- The 75% deadline. The detention list is published the same day classes
  -- end, which is why this is the date the attendance screen counts down to.
  ('2026-12-04', '2026-12-04', 'Last day of classes and final detention list',
                                                                  'milestone', true,  true),

  ('2026-12-07', '2026-12-12', 'End-term examinations',           'exam',      false, true),
  ('2026-12-14', '2026-12-19', 'End-term practical examinations', 'exam',      false, true),
  ('2026-12-20', '2027-01-03', 'Semester break',                  'break',     false, true),
  ('2027-01-04', '2027-01-04', 'Second semester begins',          'milestone', true,  false),
  ('2027-01-08', '2027-01-08', 'First semester results',          'milestone', true,  true);

-- Is this a day on which ordinary classes run? Sunday never is, and neither is
-- any day inside a non-teaching entry above.
--
-- Saturday is deliberately NOT excluded: MANIT schedules Saturday classes, and
-- the timetable is the authority on which periods exist on a given weekday.
-- This function answers only "is the term running today", never "do I have a
-- class" -- that question belongs to the timetable and always has.
create or replace function is_teaching_day(d date) returns boolean
  language sql stable
  set search_path to 'public'
as $$
  select extract(isodow from d)::int <> 7
     and not exists (
       select 1 from academic_calendar c
        where not c.teaching and d between c.starts_on and c.ends_on
     );
$$;

grant execute on function is_teaching_day(date) to authenticated;
