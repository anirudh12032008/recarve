# recarve Phase 2 — the Section I app

Status: approved 2026-09-08. Supersedes nothing; Phase 1 (`notes.py`) stays as-is
and becomes the worker's engine.

## Goal

Give the ~110 students of MANIT Section I one place for class material, so nobody
has to scroll a WhatsApp group to find last week's chemistry slides. Anyone can
record a lecture once; everyone gets the notes.

## Non-goals for v1

Campus map, clubs, other sections, quizzes as a separate feature, push
notifications, and a leaderboard screen. Points exist but are only a number on a
profile. "New notes are up" is a WhatsApp message from the admin until Apple is
paid and native push works.

## Constraints

- **No paid services.** Supabase free, Cloudflare R2 free, worker on the admin's
  Mac. The only cost is Claude API calls against existing credit (~$0.14/lecture).
- **The Mac accepts no inbound connections.** Everything it does is outbound polling.
- **iOS ships as a PWA** until the $99 Apple account exists. Same Expo codebase
  compiles to native later.

## Architecture

Three pieces, connected only through Postgres and R2:

| Piece | Runs on | Responsibility |
|---|---|---|
| App | Expo + React Native Web → Android, iOS PWA, web | UI, auth, upload, playback |
| Backend | Supabase free | Postgres, auth, row-level security, signed-URL edge function |
| Worker | Admin's Mac | Polls for queued lectures, runs `notes.py`, writes results back |

**Why the worker polls.** The Mac has no public address and sleeps. A recording
uploads to R2 and inserts a `queued` row; the Mac claims it when awake. The phone
never blocks on transcription, and moving the worker to a cloud box later is a
config change because the queue boundary already exists.

### Upload path

Direct browser/app → R2 using a presigned PUT, so audio never passes through
Supabase (whose free egress is only 2GB/month). A Supabase Edge Function signs
the URL after checking the caller is an approved user. R2 egress is free, which
is the whole reason files live there rather than in Supabase Storage.

## Data model

```sql
-- Who is in the class.
create table profiles (
  id          uuid primary key references auth.users on delete cascade,
  name        text not null,
  roll_no     text unique,
  status      text not null default 'pending' check (status in ('pending','approved','blocked')),
  trusted     boolean not null default false,  -- first upload approved => true
  is_admin    boolean not null default false,
  created_at  timestamptz not null default now()
);

-- Codes pasted into the WhatsApp group.
create table invites (
  code        text primary key,
  created_by  uuid references profiles(id),
  expires_at  timestamptz not null,
  max_uses    int not null default 200,
  uses        int not null default 0
);

-- The 12 Section I subjects, seeded from SUBJECTS in notes.py.
create table subjects (
  code  text primary key,          -- 'CY1107'
  name  text not null,             -- 'Engineering Chemistry'
  sort  int  not null default 0
);

-- A recorded class.
create table lectures (
  id            uuid primary key default gen_random_uuid(),
  subject_code  text not null references subjects(code),
  uploader_id   uuid not null references profiles(id),
  title         text,
  audio_key     text not null,              -- R2 object key
  status        text not null default 'queued'
                check (status in ('queued','transcribing','done','failed')),
  claimed_at    timestamptz,                -- for stale-claim recovery
  attempts      int not null default 0,
  error         text,
  transcript    text,
  notes_md      text,
  recorded_at   timestamptz not null default now()
);

-- Notes, slides and PDFs people upload themselves.
create table materials (
  id            uuid primary key default gen_random_uuid(),
  subject_code  text not null references subjects(code),
  uploader_id   uuid not null references profiles(id),
  filename      text not null,
  file_key      text not null,
  size_bytes    bigint not null,
  status        text not null default 'visible'
                check (status in ('pending','visible','removed')),
  created_at    timestamptz not null default now()
);

-- One vote per person per material; the schema makes double-voting impossible.
create table votes (
  material_id  uuid not null references materials(id) on delete cascade,
  voter_id     uuid not null references profiles(id) on delete cascade,
  created_at   timestamptz not null default now(),
  primary key (material_id, voter_id)
);

create table reports (
  id           uuid primary key default gen_random_uuid(),
  material_id  uuid references materials(id) on delete cascade,
  reporter_id  uuid not null references profiles(id),
  reason       text,
  created_at   timestamptz not null default now()
);

-- Points are derived, never stored, so they cannot drift out of sync.
-- security_invoker is mandatory: without it a Postgres view runs with the
-- owner's rights and silently bypasses every RLS policy below.
create view points with (security_invoker = true) as
select p.id,
       count(distinct m.id)                       as uploads,
       count(distinct l.id)                       as recordings,
       count(v.material_id)                       as votes_received,
       count(distinct m.id) * 5
         + count(distinct l.id) * 10
         + count(v.material_id)                   as score
from profiles p
left join materials m on m.uploader_id = p.id and m.status = 'visible'
left join lectures  l on l.uploader_id = p.id and l.status = 'done'
left join votes     v on v.material_id = m.id
group by p.id;
```

## Access rules

Enforced by Postgres row-level security, not by app code — the app is a client
and cannot be trusted.

- `pending` and `blocked` profiles read nothing but their own row.
- `approved` profiles read every subject, lecture, and `visible` material.
- Anyone approved inserts lectures and materials, but only as themselves
  (`uploader_id = auth.uid()`), and may delete only their own uploads.
- A material inserted by an untrusted user is forced to `status = 'pending'` by
  a trigger, so the client cannot publish by lying about the field.
- Admins read and update everything.
- Votes: insert and delete only your own row. The primary key blocks doubles.

Signup is the one flow that cannot use RLS, because the person has no profile
yet: validating an invite code and creating the `pending` profile happens in a
`security definer` function that takes the code, checks expiry and `uses`,
increments it, and inserts the row. Nothing else in the system uses
`security definer`, and that function never returns invite data to the caller —
only success or failure, so codes cannot be enumerated.

## Moderation

First upload from a new person lands as `pending` and is invisible. The admin
approves it, which flips `profiles.trusted = true` and publishes every pending
item from that person. Everything they upload afterwards is instantly visible.
The queue is therefore only ever as long as the number of newcomers, not the
number of uploads.

Reports go to an admin inbox; setting a material to `removed` hides it without
deleting the file, so a mistake is reversible.

## Job lifecycle

```
queued --(worker claims)--> transcribing --(notes written)--> done
                                 |
                                 +--(3 failed attempts)--> failed
```

The worker claims atomically so two Macs could never double-process:

```sql
update lectures set status = 'transcribing', claimed_at = now(), attempts = attempts + 1
where id = (select id from lectures
            where status = 'queued'
               or (status = 'transcribing' and claimed_at < now() - interval '2 hours')
            order by recorded_at limit 1
            for update skip locked)
returning *;
```

The stale-claim clause is what recovers a lecture whose processing died when the
Mac slept mid-run. `notes.py`'s existing checkpoint means the retry resumes
rather than restarting.

After three attempts the row goes `failed` with the error text, visible to the
uploader so they know to re-record rather than waiting forever.

## Screens

1. **Join** — email OTP + invite code → pending screen while waiting for approval
2. **Subjects** — the 12, each showing lecture and material counts
3. **Subject** — two tabs, Lectures and Notes; notes sorted by votes
4. **Lecture** — notes rendered, transcript behind a disclosure, link to audio
5. **Record** — screen-on recorder, subject picker, uploads on stop
6. **Upload** — file/photo picker for your own notes
7. **Profile** — your points and uploads
8. **Admin** — pending joiners, pending first uploads, reports

## Error handling

- Upload interrupted → the R2 object is orphaned and the row never inserted; a
  weekly worker pass deletes objects with no matching row.
- Worker offline → rows sit `queued`. The lecture list shows "waiting to be
  processed" with the queue position, so it looks intentional rather than broken.
- Claude API error → attempt counter increments, retried on the next pass.
- Supabase free tier pauses after 7 days idle. Daily class use never triggers it;
  expect to un-pause after a long break.

## Testing

- **RLS policies are the security boundary, so they get real tests**: a pending
  user, an approved user, and an admin each attempt every read and write; the
  matrix of allowed/denied is asserted. This is the one area where a bug leaks
  the class's material to outsiders.
- Worker claim logic: two concurrent claims must not return the same row.
- Trigger test: an untrusted user cannot insert a material with `status='visible'`.
- Points view: votes and uploads produce the expected score.
- One end-to-end pass: record → upload → worker → notes visible in the app.

## Rollout

1. Admin plus three friends for a week, real lectures.
2. Whole section, invite code in the WhatsApp group.
3. Only then consider other sections — which is where auth stops being an invite
   code and the design needs revisiting.

## Open items

- Apple Developer account ($99/yr) — deferred until the section is actually using it.
- Timetable-based auto-filing of recordings needs the Section I grid confirmed by
  hand; the PDF's blank cells are ambiguous when extracted.
- Whether transcripts should be stored in Postgres (simple, but rows get large)
  or in R2 as text objects. Starting in Postgres; revisit past ~500 lectures.
