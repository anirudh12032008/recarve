-- Two answers to one request: "a comment section for the notes, and a chat
-- room for each class".
--
-- The comment section already exists and is called doubts. 0025 gave every
-- lecture note a thread of the section's own words, votable, hideable, two
-- deep -- which is a comment section with a better name, because the name is
-- what tells a first-year that typing in it is allowed. Building a `comments`
-- table beside it would be a second body column, a second deleted_at, a
-- second votes column and a second /doubts handler, and the two would drift
-- the first time somebody fixed a bug in one.
--
-- What genuinely had no thread is an UPLOADED file. A photo of the board or a
-- PDF of last year's paper is the thing people most want to say "page 3 is
-- the wrong year" about, and there was nowhere to say it. So: one more
-- nullable thread key on the table that already does this.
alter table doubts add column material_id uuid references materials(id) on delete cascade;

-- A question hangs on a subject, and optionally on ONE thing inside it: a
-- lecture or an uploaded file, never both. An answer still hangs on nothing
-- but its question -- 0025's doubts_lecture_on_questions says so for lectures
-- and this says the same for files.
alter table doubts add constraint doubts_material_on_questions
  check (parent_id is null or material_id is null);
alter table doubts add constraint doubts_one_thing_per_question
  check (lecture_id is null or material_id is null);

create index on doubts (subject_code, material_id, created_at desc)
  where parent_id is null and deleted_at is null and material_id is not null;

-- No new grant and no new policy: the rows are doubts, and every rule 0025
-- wrote about who may read, ask, answer and hide one applies unchanged to a
-- question that happens to name a file.

-- ---------------------------------------------------------------------------
-- The subject room: short messages, newest at the bottom.
--
-- Not doubts. A doubt is a question that wants an answer and keeps its value
-- for a term; a room is talk, read once, in order, and the two want opposite
-- screens -- one sorted by votes and one strictly by time. Sharing a table
-- would mean every doubt thread growing a "was this chatter" column and every
-- chat message carrying a votes count nobody counts.
--
-- The id is an identity bigint rather than a uuid, and that is the whole
-- polling story: the phone holds the last id it has and asks for what is
-- after it. A uuid would force every poll to send a timestamp, and two
-- messages in the same millisecond would either repeat or vanish.
create table messages (
  id           bigint generated always as identity primary key,
  subject_code text not null references subjects(code),
  author_id    uuid not null references profiles(id),
  body         text not null check (length(btrim(body)) between 1 and 500),
  deleted_at   timestamptz,
  created_at   timestamptz not null default now()
);

-- The one read the room makes: this subject, after this id.
create index on messages (subject_code, id);

alter table messages enable row level security;

-- Everybody approved reads and writes the room -- deliberately not a role.
-- A student who may not upload, may not record and may not spend the API
-- budget can still be the one who knows the lab moved.
--
-- The hidden row stays readable to its author for the same UPDATE-visibility
-- reason as doubts; db_chat asks for the live ones.
create policy "members read the room" on messages for select
  using (is_approved()
         and (deleted_at is null or author_id = auth.uid() or is_admin()));

create policy "members talk as themselves" on messages for insert
  with check (is_approved() and author_id = auth.uid());

create policy "your own messages, or any as admin" on messages for update
  using (is_admin() or author_id = auth.uid())
  with check (is_admin() or author_id = auth.uid());

-- No delete policy: a message is hidden, never destroyed, like everything
-- else somebody said out loud here.

-- A room is one tap from a keyboard, so the flood limit is the database's and
-- not the page's. Thirty in five minutes is a fast conversation and nothing
-- like a script. SECURITY DEFINER with execute revoked, same shape as 0035:
-- the count must be true about every row and not only the ones a policy shows.
create function messages_rate_limit() returns trigger
language plpgsql security definer set search_path = public as $$
begin
  if (select count(*) from messages
       where author_id = new.author_id
         and created_at > now() - interval '5 minutes') >= 30 then
    -- Left as the default P0001 rather than a check violation: the
    -- handler has to tell "too fast" apart from "too long", and the
    -- two answer with different status codes.
    raise exception 'slow down a moment';
  end if;
  return new;
end $$;

revoke execute on function messages_rate_limit() from public;

create trigger messages_rate_limit before insert on messages
  for each row execute function messages_rate_limit();

-- Policies decide who; privileges decide that the role may reach the table at
-- all, and which columns of it.
--
-- The revoke matters here and not in production: backend/dev/migrate.py sets
-- `alter default privileges ... grant all on tables` before it applies
-- anything, so a table created here arrives with delete and a full update
-- already granted -- which is exactly the two things this feature must not
-- have. Production starts with nothing granted; the revoke puts the test
-- database in the same place, so what the tests prove is what ships.
revoke all on messages from authenticated;
grant select, insert on messages to authenticated;
-- Hiding is the only write after the fact. A message must not become
-- different words under the people who already read it, and nothing
-- reachable from a session may destroy the row underneath it.
grant update (deleted_at) on messages to authenticated;
