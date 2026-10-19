-- The tutor: a teacher in the app for the student with no recording.
--
-- Five tables. Four of them are one person's: what the tutor taught them,
-- what it asked, how well they did, and what it has learnt about how they
-- learn. The fifth is the institute's: every past-paper question anybody has
-- extracted, with its answer, which the tutor cites and which the papers
-- screen already shows as PDFs.
--
-- Who reads what, and why it is not the usual section line
--
-- A lesson is not a section's Tuesday; it is one person's hour with a book
-- that talks back. The people the section trusts may read it -- that is the
-- whole point of keeping it in the database rather than in a browser tab:
-- a classmate who missed the same lecture reads the lesson that was already
-- taught instead of paying for it twice. So `select` is is_trusted(), and
-- every write is the owner's own row. No section_id: 0041's rule for
-- per-person tables (bookmarks, attendance) is the rule here.
--
-- Past-paper questions follow archive_documents, the table they are cut
-- from: every approved member reads, only an admin writes. A paper every
-- section sat is not one section's either.

create table tutor_sessions (
  id           uuid primary key default gen_random_uuid(),
  owner_id     uuid not null references profiles(id) on delete cascade,
  subject_code text not null references subjects(code),
  -- Null topic is a whole-subject conversation ("ask me anything about
  -- chemistry"); a lesson names both.
  unit         text,
  topic        text,
  mode         text not null check (mode in ('teach','quiz','pyq','revise','ask')),
  lang         text not null default 'english' check (lang in ('english','hinglish')),
  depth        text not null default 'exam' check (depth in ('basics','exam','deep')),
  title        text not null check (length(title) between 1 and 200),
  model        text,
  status       text not null default 'open' check (status in ('open','done')),
  turns        int  not null default 0,
  -- What this session has cost so far, in dollars, summed from its messages.
  -- Six decimals because a single short turn is worth thousandths of a cent
  -- and a day of them should still add up to the bill.
  cost_usd     numeric(10,6) not null default 0,
  created_at   timestamptz not null default now(),
  updated_at   timestamptz not null default now()
);

create index tutor_sessions_by_owner
  on tutor_sessions (owner_id, updated_at desc);
create index tutor_sessions_by_subject
  on tutor_sessions (subject_code, updated_at desc);

create table tutor_messages (
  id           bigserial primary key,
  session_id   uuid not null references tutor_sessions(id) on delete cascade,
  role         text not null check (role in ('student','tutor')),
  -- say: a plain turn. lesson/check/pyq/summary: what a tutor turn mostly
  -- was. answer: the student answering a check. action: a quick-action tap
  -- ("go deeper"), stored as the sentence it stands for.
  kind         text not null default 'say',
  -- Markdown. A tutor turn is kept exactly as the model wrote it, tags and
  -- all, because the model has to see what it asked when it reads the
  -- conversation back.
  body         text not null check (length(body) <= 60000),
  -- The parsed tags: {"state": {...}, "checks": [...], "pyqs": [...]}.
  meta         jsonb not null default '{}'::jsonb,
  input_tokens       int,
  output_tokens      int,
  cache_read_tokens  int,
  cache_write_tokens int,
  cost_usd     numeric(10,6) not null default 0,
  created_at   timestamptz not null default now()
);

create index tutor_messages_in_order
  on tutor_messages (session_id, id);

-- One row per topic the tutor has ever touched with this student. level is
-- the tutor's own estimate, 0 (never seen) to 5 (exam-ready); attempts and
-- correct are the checks the student actually answered.
create table tutor_mastery (
  owner_id     uuid not null references profiles(id) on delete cascade,
  subject_code text not null references subjects(code),
  unit         text not null,
  topic        text not null,
  level        int  not null default 0 check (level between 0 and 5),
  attempts     int  not null default 0,
  correct      int  not null default 0,
  -- The tutor's own flag on how much the exam cares, with its one-line
  -- reason. Overridden on the screen by real past-paper counts when there
  -- are any; this is what it says until then.
  importance   text check (importance is null or importance in ('low','medium','high','must')),
  importance_why text,
  -- One line about this topic and this student ("mixes up the two laws").
  note         text,
  last_seen    timestamptz,
  primary key (owner_id, subject_code, unit, topic)
);

-- The tutor's running picture of the learner, per subject: the language
-- they want, how deep they want to go, and a paragraph the tutor rewrites
-- as it goes ("prefers a worked example before the rule").
create table tutor_profile (
  owner_id     uuid not null references profiles(id) on delete cascade,
  subject_code text not null references subjects(code),
  lang         text not null default 'english' check (lang in ('english','hinglish')),
  depth        text not null default 'exam' check (depth in ('basics','exam','deep')),
  notes        text,
  updated_at   timestamptz not null default now(),
  primary key (owner_id, subject_code)
);

-- Every question anybody has cut out of a past paper, as text, with its
-- answer. document_id points at the PDF it came from when it came from one
-- in the archive; null for questions imported from elsewhere. The
-- fingerprint is what makes an import re-runnable: sha256 of the subject
-- and the normalised question text, so the same question read twice -- by
-- the pipeline and then by a person -- lands once.
create table pyq_questions (
  id           uuid primary key default gen_random_uuid(),
  subject_code text not null references subjects(code),
  document_id  uuid references archive_documents(id) on delete set null,
  exam         text check (exam is null or exam in ('mini','mid','end')),
  year         int  check (year is null or year between 2000 and 2100),
  set_for_section text check (set_for_section is null or set_for_section ~ '^[A-J]$'),
  qno          text,
  marks        numeric(5,1) check (marks is null or marks >= 0),
  -- Mapped onto syllabus.json's names where the extractor could; null where
  -- it could not, which the screen shows as "unplaced" rather than hiding.
  unit         text,
  topic        text,
  question_md  text not null check (length(question_md) between 1 and 20000),
  answer_md    text check (answer_md is null or length(answer_md) <= 40000),
  answer_model text,
  extracted_by text,
  fingerprint  text not null unique,
  created_at   timestamptz not null default now()
);

create index pyq_questions_by_topic
  on pyq_questions (subject_code, unit, topic);
create index pyq_questions_by_sitting
  on pyq_questions (subject_code, year desc nulls last, exam);

-- ---------------------------------------------------------------------------
-- Policies.
--
-- Every select is is_trusted() OR the owner. The owner clause is not
-- redundant: RECARVE_TUTOR_ROLE picks who may teach, and if it were ever
-- set below trusted, an owner who could write rows could not read them
-- back -- and an upsert's ON CONFLICT DO UPDATE needs select to see the
-- row it is updating, so their mastery would fail to save at all.

alter table tutor_sessions enable row level security;
alter table tutor_messages enable row level security;
alter table tutor_mastery  enable row level security;
alter table tutor_profile  enable row level security;
alter table pyq_questions  enable row level security;

create policy "trusted people read every lesson" on tutor_sessions for select
  using (is_trusted() or owner_id = auth.uid());
create policy "you open your own lessons" on tutor_sessions for insert
  with check (is_approved() and owner_id = auth.uid());
create policy "you change your own lessons" on tutor_sessions for update
  using (owner_id = auth.uid()) with check (owner_id = auth.uid());
create policy "you delete your own lessons" on tutor_sessions for delete
  using (owner_id = auth.uid());

-- A message is readable when its lesson is; writable when its lesson is
-- yours. Checked through the session rather than by copying owner_id onto
-- every message, so there is one place the answer lives.
create policy "trusted people read every message" on tutor_messages for select
  using (is_trusted() or exists (select 1 from tutor_sessions s
                                   where s.id = session_id and s.owner_id = auth.uid()));
create policy "you write into your own lessons" on tutor_messages for insert
  with check (exists (select 1 from tutor_sessions s
                       where s.id = session_id and s.owner_id = auth.uid()));
create policy "you delete from your own lessons" on tutor_messages for delete
  using (exists (select 1 from tutor_sessions s
                  where s.id = session_id and s.owner_id = auth.uid()));

create policy "trusted people read every mastery row" on tutor_mastery for select
  using (is_trusted() or owner_id = auth.uid());
create policy "your own mastery" on tutor_mastery for insert
  with check (is_approved() and owner_id = auth.uid());
create policy "your own mastery changes" on tutor_mastery for update
  using (owner_id = auth.uid()) with check (owner_id = auth.uid());
create policy "your own mastery goes" on tutor_mastery for delete
  using (owner_id = auth.uid());

create policy "trusted people read every profile" on tutor_profile for select
  using (is_trusted() or owner_id = auth.uid());
create policy "your own profile" on tutor_profile for insert
  with check (is_approved() and owner_id = auth.uid());
create policy "your own profile changes" on tutor_profile for update
  using (owner_id = auth.uid()) with check (owner_id = auth.uid());
create policy "your own profile goes" on tutor_profile for delete
  using (owner_id = auth.uid());

create policy "members read the questions" on pyq_questions for select
  using (is_approved());
create policy "admins add questions" on pyq_questions for insert
  with check (is_admin());
create policy "admins edit questions" on pyq_questions for update
  using (is_admin()) with check (is_admin());
create policy "admins remove questions" on pyq_questions for delete
  using (is_admin());

grant select, insert, update, delete on tutor_sessions to authenticated;
grant select, insert, delete on tutor_messages to authenticated;
grant select, insert, update, delete on tutor_mastery to authenticated;
grant select, insert, update, delete on tutor_profile to authenticated;
grant select, insert, update, delete on pyq_questions to authenticated;
grant usage, select on sequence tutor_messages_id_seq to authenticated;
