-- Doubts: a question the class answers, and the votes that sort the answers.
--
-- One table, not two. A question and an answer are the same thing said at two
-- depths -- somebody's words, on a lecture, at a time -- and splitting them
-- would double every policy, every index and every delete rule for the sake of
-- one nullable column. parent_id null is the question; parent_id set is an
-- answer to it, and the insert policy below is what keeps it exactly two deep
-- rather than a comment tree nobody asked for.
--
-- A question is about a subject, and about one of that subject's lectures when
-- there is a row for it. Both, rather than lectures alone: the library holds
-- files the database has never heard of -- a static export, a set of slides
-- somebody dropped in -- and "why is this integral like that" asked on a
-- subject is a real question that must not need a recording to exist first.
--
-- An answer carries neither: it is about its question, and the question says
-- which subject that is. Copying the subject onto every answer is one more
-- column that can disagree with the row above it.
--
-- Deleting hides. Same reason as the notice board: these are words a hundred
-- and ten classmates may already have read, and a row that is gone is a row
-- nobody can look at again when somebody asks what was said. deleted_at is the
-- whole of it, and the select policy is what makes it a delete for everybody.
create table doubts (
  id           uuid primary key default gen_random_uuid(),
  parent_id    uuid references doubts(id) on delete cascade,
  subject_code text references subjects(code),
  lecture_id   uuid references lectures(id) on delete cascade,
  author_id    uuid not null references profiles(id),
  body         text not null check (length(btrim(body)) between 1 and 2000),
  deleted_at   timestamptz,
  created_at   timestamptz not null default now(),
  -- A question has a subject and no parent; an answer has a parent and no
  -- subject. Written as one equality so neither half can be forgotten.
  constraint doubts_question_or_answer
    check ((parent_id is null) = (subject_code is not null)),
  -- And only a question can name a lecture, for the same reason.
  constraint doubts_lecture_on_questions
    check (parent_id is null or lecture_id is null)
);

-- The two orders the app ever asks for: a thread's live questions newest
-- first, and one question's answers.
create index on doubts (subject_code, lecture_id, created_at desc)
  where parent_id is null and deleted_at is null;
create index on doubts (parent_id) where deleted_at is null;

alter table doubts enable row level security;

-- Everybody approved reads the thread, students included. That is the whole
-- point of the feature: a student may not upload, may not record, may not
-- spend the API budget on Explain, and has had nothing to give until now.
-- Asking at 1am and answering a classmate is the one thing the role was never
-- the gate for -- and neither is the score, which opens nothing here either.
--
-- A hidden row stays readable to the person who hid it, exactly as the notice
-- board's does, and for a reason that is not a kindness: Postgres checks the
-- row an UPDATE leaves behind against the select policy too, so a policy that
-- hides it from its own author is a policy under which nobody can hide
-- anything at all. What the thread actually shows is db_doubts' business, and
-- it asks for the live rows.
create policy "members read live doubts" on doubts for select
  using (is_approved()
         and (deleted_at is null or author_id = auth.uid() or is_admin()));

-- author_id = auth.uid() so nobody asks or answers in somebody else's name --
-- the thread says who wrote each line and the class reads that.
--
-- The subselect is what keeps this two levels deep: an answer's parent has to
-- be a question that is still there. Without it a reply to a reply is one
-- request away, and every screen that draws a thread has to grow a recursion
-- it was never designed for. `doubts.parent_id` is qualified because a bare
-- parent_id inside that subquery is q's own column, and q.id = q.parent_id is
-- never true -- which would refuse every answer ever written.
create policy "members ask and answer" on doubts for insert
  with check (
    is_approved() and author_id = auth.uid()
    and (parent_id is null
         or exists (select 1 from doubts q
                     where q.id = doubts.parent_id and q.parent_id is null
                       and q.deleted_at is null))
  );

-- Hiding your own words, and an admin's reach over anybody's. Nothing else
-- runs through here: a doubt is not editable, because a question somebody has
-- already answered must not be able to become a different question.
create policy "your own doubts, or any as admin" on doubts for update
  using (is_admin() or author_id = auth.uid())
  with check (is_admin() or author_id = auth.uid());

-- No delete policy at all, on purpose, like announcements: the app hides with
-- deleted_at and nothing reachable from a session may destroy the row under it.

-- ---------------------------------------------------------------------------
-- Answers are voted on with the votes table the notes already use, not with a
-- second one that looks like it.
--
-- The alternative was a doubt_votes table, and it would have been a copy of
-- every line here: the same one-per-person rule, the same "not your own", the
-- same is_approved() read, the same /vote handler forked in two. A vote is a
-- vote. What changes is which column holds the thing being voted for, and the
-- check below is what says exactly one of them ever does.
--
-- The primary key goes because it cannot be half-null, and comes back as two
-- partial unique indexes that say the same thing per column. The handler's
-- 409 rides on a unique violation and keeps riding on one.
alter table votes add column doubt_id uuid references doubts(id) on delete cascade;
alter table votes drop constraint votes_pkey;
alter table votes alter column material_id drop not null;
alter table votes add constraint votes_one_thing
  check ((material_id is null) <> (doubt_id is null));
create unique index on votes (material_id, voter_id) where material_id is not null;
create unique index on votes (doubt_id, voter_id)    where doubt_id is not null;

-- 0022's rule, now in both directions. Answering your own question and then
-- upvoting the answer is one person agreeing with themselves, and on a board
-- where the top answer is the one people read that is worth closing.
--
-- Two `not exists` rather than one clause about "the thing": a vote for a row
-- that does not exist has to keep failing on the foreign key, so the server can
-- still answer "no such item" instead of "you cannot upvote your own".
drop policy "vote as yourself" on votes;
create policy "vote as yourself" on votes for insert
  with check (
    is_approved() and voter_id = auth.uid()
    and not exists (select 1 from materials m
                     where m.id = material_id and m.uploader_id = voter_id)
    and not exists (select 1 from doubts d
                     where d.id = doubt_id and d.author_id = voter_id)
  );

-- The policies above decide WHO may do what; these decide that the role may
-- reach the table at all. Both are needed and they fail differently: without
-- the grant every member gets "permission denied for table doubts", whatever
-- the policies say.
--
-- Easy to leave out, because backend/dev/migrate.py sets default privileges
-- before it applies anything, so a granted-by-default test database says the
-- feature works. Production has no such default and said "permission denied"
-- to the first student who tried to ask a question. Every migration that
-- creates a table states its grants explicitly, and test_table_grants.py now
-- fails the build if one does not.
--
-- No delete: a doubt is hidden by setting deleted_at, never removed, so the
-- thread a classmate answered cannot silently lose its question.
grant select, insert, update on doubts to authenticated;
