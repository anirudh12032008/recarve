-- One section shares one weekly schedule, so a template holds it and every
-- profile is seeded from that template. Without this, 110 students each retype
-- the same grid.
--
-- This migration is SCHEMA ONLY. The actual periods are data, seeded separately
-- (backend/dev/seed_timetables.sql) into the live database. Tests therefore start
-- with an empty template and an empty timetable, which is the behaviour the
-- timetable tests already assert.
create table if not exists section_timetable (
  day          int  not null check (day between 1 and 6),
  period       int  not null check (period between 1 and 8),
  subject_code text not null references subjects(code),
  primary key (day, period)
);

create or replace function seed_timetable_for_new_profile() returns trigger
language plpgsql security definer set search_path = public as $$
begin
  insert into timetable (profile_id, day, period, subject_code)
  select new.id, s.day, s.period, s.subject_code from section_timetable s
  on conflict (profile_id, day, period) do nothing;
  return new;
end;
$$;

drop trigger if exists profiles_seed_timetable on profiles;
create trigger profiles_seed_timetable after insert on profiles
  for each row execute function seed_timetable_for_new_profile();

alter table section_timetable enable row level security;
drop policy if exists "members read the section timetable" on section_timetable;
create policy "members read the section timetable" on section_timetable
  for select using (is_approved());
grant select on section_timetable to authenticated;
