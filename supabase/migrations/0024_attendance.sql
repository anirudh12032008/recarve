-- Attendance, per subject, because that is the number MANIT actually checks:
-- 75% in each subject to sit that subject's exam. Overall attendance is not a
-- thing anybody is refused an exam over, so it is not a thing this stores.
--
-- THREE STATES, and only two of them are rows. present and absent are written
-- down; not-yet-marked is the absence of a row. There is deliberately no
-- default: a silent "present" would quietly produce a percentage that is wrong
-- in the student's favour, which is worse than no percentage at all, and a
-- silent "absent" is worse still. An unmarked period counts in neither the
-- numerator nor the denominator.
--
-- subject_code is stored rather than looked up through the timetable at read
-- time. A student who fixes their lab batch in October must not have every
-- September mark re-filed under the subject they now have in that slot -- the
-- mark was about the class they sat in, and the row says which one that was.
create table attendance (
  profile_id   uuid not null references profiles(id) on delete cascade,
  on_date      date not null,
  period       int  not null check (period between 1 and 8),
  subject_code text not null references subjects(code),
  state        text not null check (state in ('present', 'absent')),
  marked_at    timestamptz not null default now(),
  primary key (profile_id, on_date, period)
);

-- The one read the summary makes: everything one person ever marked, by subject.
create index on attendance (profile_id, subject_code);

-- A class that did not happen must not count against anybody, and that is a
-- fact about the CLASS, not about a student -- so it is class-wide and one
-- trusted member sets it for everyone.
--
-- Per-student would have been less code and is the wrong answer twice over.
-- It would make "cancelled" a button that removes a class from your own
-- denominator, which is indistinguishable from lying about it, and it would
-- leave 110 people holding 110 different denominators for the same Tuesday --
-- so no two of their percentages would mean the same thing. Class-wide costs a
-- trusted role to set, which the app already has.
--
-- Keyed by subject as well as period because Section I splits for the labs:
-- period 3 on a Tuesday is one subject for one batch and another for the next,
-- and cancelling one lab must not cancel the other.
create table cancelled_classes (
  on_date      date not null,
  period       int  not null check (period between 1 and 8),
  subject_code text not null references subjects(code),
  reason       text not null default '',
  set_by       uuid not null references profiles(id),
  set_at       timestamptz not null default now(),
  primary key (on_date, period, subject_code)
);

alter table attendance        enable row level security;
alter table cancelled_classes enable row level security;

-- Your own attendance and nobody else's, in every direction. Not a where
-- clause in a handler: this is private in the way a mark is private, and a
-- student holding a database session gets the same answer the app gives.
-- `for all` rather than four policies, exactly as the timetable does it: one
-- rule, one place for it to be true.
--
-- The with-check is also where "you cannot mark a class you do not have"
-- lives. The handler says it too, with a readable reason -- but saying it here
-- makes it true rather than intended, and it keeps the marks joined to the
-- timetable that is the only reason anybody knows the class existed.
-- extract(isodow) is 1=Monday..7=Sunday, which is exactly what timetable.day
-- already means, so there is no translation and Sunday simply has no rows.
create policy "your own attendance" on attendance for all
  using (profile_id = auth.uid())
  with check (
    is_approved()
    and profile_id = auth.uid()
    and on_date <= current_date          -- tomorrow has not happened yet
    and exists (
      select 1 from timetable t
       where t.profile_id = auth.uid()
         and t.day = extract(isodow from attendance.on_date)::int
         and t.period = attendance.period
         and t.subject_code = attendance.subject_code
    )
  );

-- Everybody approved reads which classes were called off: it is the same fact
-- for all of them, and a student whose denominator it changes has to be able
-- to see why.
create policy "members read cancelled classes" on cancelled_classes for select
  using (is_approved());

-- Setting one changes everyone's denominator, so it is a trusted act -- the
-- same bar as adding to the library. set_by = auth.uid() so the row says who,
-- and cannot say somebody else.
create policy "trusted call a class off" on cancelled_classes for insert
  with check (is_trusted() and set_by = auth.uid());

-- And take it back, because a class called off by mistake is one somebody has
-- to be able to un-call. Any trusted member, not just the one who set it: the
-- person who can fix it at 9am is whoever is awake.
create policy "trusted put a class back" on cancelled_classes for delete
  using (is_trusted());

-- No update policy: a cancellation is on or off, and the two writes above are
-- the whole of it.

grant select, insert, update, delete on attendance to authenticated;
grant select, insert, delete on cancelled_classes to authenticated;
