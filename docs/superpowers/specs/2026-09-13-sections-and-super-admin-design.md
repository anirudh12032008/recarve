# Sections, roles and the super admin

Written 2026-09-13, before the Tuesday beta. This is the CONTRACT that the
parallel agents build against. If you are an agent and reality disagrees with
this document, stop and say so rather than inventing a third design.

## Why

recarve is single-tenant today. "Section" exists only in prose and in
`section_timetable` (a template table, singular). One install = one section.
The owner needs the *framework* for many sections before the beta -- not full
production traffic. Section I keeps working exactly as it does now; one test
section gets created to prove the machinery.

## The shape

### 1. `sections`

```sql
create table sections (
  id            uuid primary key default gen_random_uuid(),
  name          text not null,               -- 'I'
  grad_year     int  not null,               -- 2030
  subject_set_id uuid references subject_sets(id),
  created_at    timestamptz not null default now(),
  unique (name, grad_year)
);
```

Displayed as **Section I '30**. `grad_year` is the graduation year, so the
same section name repeats safely across intakes.

### 2. Subject sets (the curriculum split)

In semester 1, sections A-E follow one curriculum and F-J another; the two
swap in semester 2. Model that as a named set of subjects a section points at:

```sql
create table subject_sets (id uuid primary key, name text not null unique);
alter table subjects add column set_id uuid references subject_sets(id);
```

Backfill: create sets **'Set A'** and **'Set B'**. Every existing subject
belongs to **Set B** (Section I follows Set B). Then COPY every Set B subject
into Set A as a starting point -- the owner will correct Set A's real contents
later. Section I points at Set B.

A section's `subject_set_id` is changeable, which is how the semester-2 swap
happens. Do not build semester logic beyond that; YAGNI.

### 3. What is section-scoped and what is not

**Per-section** (gets `section_id uuid not null references sections(id)`):
`profiles`, `materials`, `lectures`, `announcements`, `timetable`,
`section_timetable`, `attendance`, `doubts`, `posts` (feed + confessions),
`votes` where it hangs off the above.

**Institute-wide** (NO section_id -- shared by everyone at MANIT):
`clubs`, `events`, `places`, the academic calendar.

Professors differ per section, so notes and announcements MUST be per-section.

### 4. RLS -- the dangerous part

RLS is the security boundary. Every scoped table's policies must additionally
constrain to the caller's own section. Add a helper beside the existing
`is_admin()` / `is_trusted()`:

```sql
create or replace function my_section() returns uuid
language sql stable security definer set search_path = public as $$
  select section_id from profiles where id = auth_uid()
$$;
```

(Match the existing helpers' exact idiom for getting the caller -- read
`0009_roles.sql` and `0005_trust.sql` first; do not guess the auth function.)

Every existing policy on a scoped table gets `and section_id = my_section()`.
A test MUST prove that a member of section X cannot read section Y's
materials, announcements, doubts or feed posts -- by connecting AS a section-Y
member and getting zero rows, not by trusting the application layer.

### 5. Roles

`ROLES = ("student", "trusted", "cr", "admin")`, ranked in that order.

- **student** -- reads. The default, including after an admin approves them
  (fixed in `0039`).
- **trusted** -- may upload/add materials. Granted manually.
- **cr** (class representative) -- everything trusted may do, PLUS posting
  announcements. This is the new role.
- **admin** -- runs the section.

Super admin is NOT a role; it is platform-level and orthogonal. See below.

The role check must stay rank-based so future roles slot in without touching
every call site.

### 6. Super admin

Lives at **`/super`**, a hidden route that is in no navigation and whose markup
is never sent to students.

Authentication is deliberately SEPARATE from student auth, so that no bug in
the student path can escalate into it:

- `RECARVE_SUPER_EMAIL` and `RECARVE_SUPER_PASSWORD` read from the environment
  (the systemd unit already loads `~/app/.env` via `EnvironmentFile`).
- If either is unset, `/super` returns 404 -- not a login form. An install
  without the env vars has no super admin surface at all.
- Success sets its own signed cookie, distinct from the student session
  cookie. Reuse the existing session-signing helper; do not invent new crypto.
- Passwords in this app are plain text by deliberate decision. Keep that.

What `/super` can do, and nothing more for now:
1. List every section with its member count.
2. Create a section (name, grad year, subject set).
3. Open one section and list its members.
4. Change a member's role, including granting `cr`.

### 7. Timetable import

Creating a section needs a timetable. The owner photographs a printed
timetable, hands it to an AI, and pastes back a simple format. Use **CSV**,
because it survives being pasted into a textarea:

```csv
day,period,subject_code
Monday,1,MC1101
Monday,2,MA1101
```

- `day` is a full weekday name, case-insensitive.
- `period` is 1-8 (`PERIODS` in notes.py).
- `subject_code` must already exist in that section's subject set; an unknown
  code is an error naming the line, not a silent skip.
- The parser is a PURE function (text in, rows out, errors as values) so it can
  be tested without a database, and it reports EVERY bad line at once rather
  than dying on the first.

## Migration numbering (do not collide)

`0039` is taken (the approval fix). Then:

- **0040-0044** -- sections, subject sets, section_id columns, RLS rewrite, backfill
- **0045** -- the `cr` role
- **0046+** -- timetable import, if it needs schema at all

## Backfill is not optional

Section I is live with real students. Every migration must backfill the
existing rows into a Section I row (`name='I'`, the correct grad year) and be
safe to run against a populated database. Nothing may be `not null` without a
default or a backfill in the same migration. The deploy runs migrations BY HAND
with `psql -f`, one at a time, so each file must stand alone.

## Out of scope for Tuesday

Billing, storage quotas, cross-section discovery, per-section theming,
semester rollover automation. The owner named these as "full production"
concerns; do not build them.
