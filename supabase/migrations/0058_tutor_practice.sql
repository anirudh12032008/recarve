-- Practice: the tutor asks first, from a curriculum written ahead of time
-- (tutor/course/), and keeps a per-subtopic record of how the student did.
-- See docs/specs/2026-10-19-tutor-practice.md.

-- A topic's one session is a practice session; the older modes stay for
-- the lessons already stored.
alter table tutor_sessions drop constraint tutor_sessions_mode_check;
alter table tutor_sessions add constraint tutor_sessions_mode_check
  check (mode in ('teach','quiz','pyq','revise','ask','practice'));

-- Where the student sets the theory / questions balance, per subject.
alter table tutor_profile add column mix text not null default 'questions'
  check (mix in ('questions','mixed','theory'));

-- One row per student and subtopic they have answered anything in.
-- status: green clear, yellow minor gaps, red struggling. asked: the
-- curriculum question ids already put to them, so a reopened topic carries on
-- instead of repeating. notes: up to three one-line observations from the
-- grader, newest last ("forgets HCl can be the by-product").
create table tutor_progress (
  owner_id     uuid not null references profiles(id) on delete cascade,
  subject_code text not null references subjects(code),
  unit         text not null,
  topic        text not null,
  subtopic     text not null,
  status       text not null default 'yellow' check (status in ('green','yellow','red')),
  right_count  int  not null default 0,
  wrong_count  int  not null default 0,
  streak       int  not null default 0,   -- +n right in a row, -n wrong in a row
  last_score   numeric(4,2),             -- 0..1, the last answer's share of the marks
  last_level   int,
  asked        text[] not null default '{}',
  notes        text[] not null default '{}',
  updated_at   timestamptz not null default now(),
  primary key (owner_id, subject_code, unit, topic, subtopic)
);

alter table tutor_progress enable row level security;

create policy "trusted people read every progress row" on tutor_progress for select
  using (is_trusted() or owner_id = auth.uid());
create policy "your own progress" on tutor_progress for insert
  with check (is_approved() and owner_id = auth.uid());
create policy "your own progress changes" on tutor_progress for update
  using (owner_id = auth.uid()) with check (owner_id = auth.uid());
create policy "your own progress goes" on tutor_progress for delete
  using (owner_id = auth.uid());

grant select, insert, update, delete on tutor_progress to authenticated;
