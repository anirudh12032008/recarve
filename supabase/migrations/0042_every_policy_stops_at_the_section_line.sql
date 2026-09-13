-- The dangerous one. Read it twice.
--
-- Row level security is this app's security boundary, not a second opinion on
-- top of one. notes.py runs every member request on a connection that has set
-- role authenticated and auth.uid() to that member, and then writes `select *
-- from materials` -- so what a policy says is precisely what a student can
-- reach, and a policy that forgets a clause is not a rendering bug. It is one
-- section reading another section's private words.
--
-- Which is why this file is a rewrite of EVERY policy on EVERY scoped table
-- rather than a few additions: a table with four policies and a section clause
-- on three of them is a table with no section boundary at all, because
-- Postgres ORs permissive policies together. One forgotten policy re-opens the
-- whole table.
--
-- The clause is the same seven characters everywhere, deliberately:
--
--     and section_id = my_section()
--
-- It reads as an extra AND on the rule that was already there, never as a
-- replacement for it, and it is always an AND -- a section boundary narrows
-- what somebody may do and must never widen it. If my_section() is null (a
-- stranger, somebody mid-signup, a connection with no claims) the comparison
-- is NULL rather than true, so they match nothing. Fail closed.
--
-- What this file does NOT do: reads. There is no handler change to go with it
-- and there does not need to be one. `select ... from materials` run by a
-- member of section Y already returns only section Y's rows once these
-- policies are in place, because that is what a select policy IS -- a where
-- clause the database adds and the caller cannot remove.

-- ---------------------------------------------------------------------------
-- The two tables 0040 created and left shut, now that my_section() exists.

-- Your own section, by name and year, so a screen can say "Section I '30".
-- Not every section: which sections exist is the super admin's list, read on a
-- connection that owns these tables, and a member has no use for the roll of
-- an intake they are not in.
create policy "read the section you are in" on sections for select
  using (id = my_section());

-- And the set it follows, for the subjects policy below to lean on. Same
-- reasoning: yours, not the catalogue.
create policy "read the set your section follows" on subject_sets for select
  using (id = (select s.subject_set_id from sections s where s.id = my_section()));

-- ---------------------------------------------------------------------------
-- Subjects are institute-wide rows read through a per-section lens.
--
-- There is one subjects table and there should be: MC1101 is MC1101 whoever is
-- taught it. What differs is WHICH subjects a section is taught, and that is
-- the subject set. Without this policy a subject set is a decorative column --
-- every member would see every set's subjects in the same list, which for
-- Section I means Set A's twelve placeholder rows appearing in their library
-- the moment 0040 runs.
--
-- Its own helper rather than a subquery on sections, for the reason every
-- helper in this schema is one: a policy subquery runs with the CALLER's
-- privileges and against the CALLER's policies, so reading sections from
-- inside here would chain one policy onto another for a fact the caller
-- already knows about themselves.
create or replace function my_subject_set() returns uuid
language sql stable security definer set search_path = public as $$
  select s.subject_set_id from sections s where s.id = my_section()
$$;

drop policy "everyone approved reads subjects" on subjects;
create policy "everyone approved reads their own set's subjects"
  on subjects for select
  using (is_approved() and set_id = my_subject_set());

-- ---------------------------------------------------------------------------
-- profiles. The class list is the first thing that leaks if this is wrong:
-- names, roll numbers and phone numbers of a hundred and ten strangers.
--
-- "read own profile always" is deliberately left exactly as it was. It is the
-- policy that lets somebody pending, blocked, or halfway through signing up
-- see their own row, and it is keyed on their own id -- there is no section to
-- add to `id = auth.uid()` that would make it narrower. Adding one would in
-- fact break signup, whose whole problem is that the section is not known yet.

drop policy "approved members read the class" on profiles;
create policy "approved members read their own section" on profiles for select
  using (is_approved() and section_id = my_section());

-- An admin runs A section, not the institute. Moving somebody else's section
-- is not something the admin panel does; the super admin does it on a
-- connection that owns these tables.
drop policy "admins manage profiles" on profiles;
create policy "admins manage their own section's profiles" on profiles for update
  using (is_admin() and section_id = my_section())
  with check (is_admin() and section_id = my_section());

-- The pinning policy, with one more thing pinned. Without the last clause a
-- member updates their own name and their own section_id in the same
-- statement, and walks into any section whose id they can guess -- which is
-- the entire boundary, undone by an UPDATE the app already sends on the
-- profile screen. status and role were pinned here for exactly this reason
-- since 0002; section is the third.
drop policy "edit own name only" on profiles;
create policy "edit own name only"
  on profiles for update
  using (id = auth.uid())
  with check (
    id = auth.uid()
    and status     = (select p.status     from profiles p where p.id = auth.uid())
    and role       = (select p.role       from profiles p where p.id = auth.uid())
    and section_id = (select p.section_id from profiles p where p.id = auth.uid())
  );

-- ---------------------------------------------------------------------------
-- invites. A code is a door into one section, so an admin may only see and
-- mint the codes for their own. 0044 is where redeeming one honours it.
drop policy "admins manage invites" on invites;
create policy "admins manage their own section's invites" on invites for all
  using (is_admin() and section_id = my_section())
  with check (is_admin() and section_id = my_section());

-- ---------------------------------------------------------------------------
-- lectures and materials: the library, and the headline case of the whole
-- feature. Professors differ per section, so a recording of one section's
-- Tuesday is not a thing the other section may read.

drop policy "members read lectures" on lectures;
create policy "members read lectures" on lectures for select
  using (is_approved() and section_id = my_section());

drop policy "trusted add lectures" on lectures;
create policy "trusted add lectures" on lectures for insert
  with check (is_trusted() and uploader_id = auth.uid()
              and section_id = my_section());

drop policy "own or admin edit lectures" on lectures;
create policy "own or admin edit lectures" on lectures for update
  using ((is_admin() or uploader_id = auth.uid()) and section_id = my_section());

drop policy "own or admin delete lectures" on lectures;
create policy "own or admin delete lectures" on lectures for delete
  using ((is_admin() or uploader_id = auth.uid()) and section_id = my_section());

drop policy "members read visible materials" on materials;
create policy "members read visible materials" on materials for select
  using (is_approved() and section_id = my_section()
         and (status = 'visible' or uploader_id = auth.uid() or is_admin()));

drop policy "trusted add materials" on materials;
create policy "trusted add materials" on materials for insert
  with check (is_trusted() and uploader_id = auth.uid()
              and section_id = my_section());

drop policy "own or admin edit materials" on materials;
create policy "own or admin edit materials" on materials for update
  using ((is_admin() or uploader_id = auth.uid()) and section_id = my_section())
  with check (
    section_id = my_section()
    and (is_admin()
         or (uploader_id = auth.uid()
             and (status = (select m.status from materials m where m.id = materials.id)
                  or is_trusted())))
  );

drop policy "own or admin delete materials" on materials;
create policy "own or admin delete materials" on materials for delete
  using ((is_admin() or uploader_id = auth.uid()) and section_id = my_section());

-- ---------------------------------------------------------------------------
-- votes. Not content, but it names people: who upvoted which answer is a fact
-- about a hundred and ten classmates, and the board is drawn from it.
--
-- The insert policy's two `not exists` clauses stay exactly as 0035 left them.
-- They are about "not your own", not about sections, and a vote whose target
-- is in another section is already unreachable -- the member cannot see the
-- row to learn its id, and the row they would write carries their own section
-- and is invisible to the section it points at.

drop policy "members read votes" on votes;
create policy "members read votes" on votes for select
  using (is_approved() and section_id = my_section());

drop policy "vote as yourself" on votes;
create policy "vote as yourself" on votes for insert
  with check (
    is_approved() and voter_id = auth.uid() and section_id = my_section()
    and not exists (select 1 from materials m
                     where m.id = material_id and m.uploader_id = voter_id)
    and not exists (select 1 from doubts d
                     where d.id = doubt_id and d.author_id = voter_id)
  );

drop policy "unvote your own" on votes;
create policy "unvote your own" on votes for delete
  using (voter_id = auth.uid() and section_id = my_section());

-- ---------------------------------------------------------------------------
-- reports. Only an admin ever reads one, and an admin runs one section.

drop policy "report as yourself" on reports;
create policy "report as yourself" on reports for insert
  with check (is_approved() and reporter_id = auth.uid()
              and section_id = my_section());

drop policy "admins read reports" on reports;
create policy "admins read reports" on reports for select
  using (is_admin() and section_id = my_section());

-- ---------------------------------------------------------------------------
-- The week: one student's grid, the template it is seeded from, the marks
-- against it, and the classes called off.
--
-- timetable and attendance are already private to one person, so the section
-- clause changes nothing anybody can reach today. It goes on anyway, because
-- "this row belongs to somebody in my section" is the rule, and a rule with an
-- exception in it is a rule the next person has to re-derive.

drop policy "your own timetable" on timetable;
create policy "your own timetable" on timetable for all
  using (profile_id = auth.uid() and section_id = my_section())
  with check (is_approved() and profile_id = auth.uid()
              and section_id = my_section());

drop policy "members read the section timetable" on section_timetable;
create policy "members read the section timetable" on section_timetable
  for select using (is_approved() and section_id = my_section());

drop policy "your own attendance" on attendance;
create policy "your own attendance" on attendance for all
  using (profile_id = auth.uid() and section_id = my_section())
  with check (
    is_approved()
    and profile_id = auth.uid()
    and section_id = my_section()
    and on_date <= current_date
    and exists (
      select 1 from timetable t
       where t.profile_id = auth.uid()
         and t.day = extract(isodow from attendance.on_date)::int
         and t.period = attendance.period
         and t.subject_code = attendance.subject_code
    )
  );

drop policy "members read cancelled classes" on cancelled_classes;
create policy "members read cancelled classes" on cancelled_classes for select
  using (is_approved() and section_id = my_section());

drop policy "trusted call a class off" on cancelled_classes;
create policy "trusted call a class off" on cancelled_classes for insert
  with check (is_trusted() and set_by = auth.uid() and section_id = my_section());

drop policy "trusted put a class back" on cancelled_classes;
create policy "trusted put a class back" on cancelled_classes for delete
  using (is_trusted() and section_id = my_section());

-- ---------------------------------------------------------------------------
-- The notice board. A section's admin tells a section; the other section is
-- not told, and cannot read it either.

drop policy "members read live announcements" on announcements;
create policy "members read live announcements" on announcements for select
  using (is_approved() and section_id = my_section()
         and (deleted_at is null or author_id = auth.uid()));

drop policy "admins post announcements" on announcements;
create policy "admins post announcements" on announcements for insert
  with check (is_admin() and author_id = auth.uid() and section_id = my_section());

drop policy "admins edit their own announcements" on announcements;
create policy "admins edit their own announcements" on announcements for update
  using (is_admin() and author_id = auth.uid() and section_id = my_section())
  with check (is_admin() and author_id = auth.uid() and section_id = my_section());

-- ---------------------------------------------------------------------------
-- Doubts: questions about one section's classes, answered by that section.

drop policy "members read live doubts" on doubts;
create policy "members read live doubts" on doubts for select
  using (is_approved() and section_id = my_section()
         and (deleted_at is null or author_id = auth.uid() or is_admin()));

drop policy "members ask and answer" on doubts;
create policy "members ask and answer" on doubts for insert
  with check (
    is_approved() and author_id = auth.uid() and section_id = my_section()
    and (parent_id is null
         or exists (select 1 from doubts q
                     where q.id = doubts.parent_id and q.parent_id is null
                       and q.deleted_at is null))
  );

drop policy "your own doubts, or any as admin" on doubts;
create policy "your own doubts, or any as admin" on doubts for update
  using ((is_admin() or author_id = auth.uid()) and section_id = my_section())
  with check ((is_admin() or author_id = auth.uid()) and section_id = my_section());

-- ---------------------------------------------------------------------------
-- The subject room. The spec's list of per-section tables does not name
-- messages, because messages did not exist when the list was written -- but a
-- room is a hundred and ten classmates talking about the lab they just sat in,
-- which is the most per-section thing in the app. Scoped, and said out loud
-- here so that the omission is not read later as a decision.

drop policy "members read the room" on messages;
create policy "members read the room" on messages for select
  using (is_approved() and section_id = my_section()
         and (deleted_at is null or author_id = auth.uid() or is_admin()));

drop policy "members talk as themselves" on messages;
create policy "members talk as themselves" on messages for insert
  with check (is_approved() and author_id = auth.uid()
              and section_id = my_section());

drop policy "your own messages, or any as admin" on messages;
create policy "your own messages, or any as admin" on messages for update
  using ((is_admin() or author_id = auth.uid()) and section_id = my_section())
  with check ((is_admin() or author_id = auth.uid()) and section_id = my_section());

-- ---------------------------------------------------------------------------
-- The wall and the confessions.
--
-- A confession is the row in this database with the most to lose. 0035 keeps
-- its author unreadable by never granting select on author_id; the section
-- clause is the other half of the same promise -- a confession is said to a
-- section, and a stranger from another one must not be reading it at all.
--
-- section_id is tested here and is not in the column grants (0041 says why).
-- A policy expression is evaluated by the system, not by the caller, so it may
-- look at a column the caller may not select; the caller still cannot ask for
-- it, which is the point.

drop policy "members read live posts" on posts;
create policy "members read live posts" on posts for select
  using (is_approved() and section_id = my_section()
         and (deleted_at is null or author_id = auth.uid() or is_admin()));

drop policy "members post" on posts;
create policy "members post" on posts for insert
  with check (is_approved() and author_id = auth.uid()
              and section_id = my_section());

drop policy "your own posts, or any as admin" on posts;
create policy "your own posts, or any as admin" on posts for update
  using ((is_admin() or author_id = auth.uid()) and section_id = my_section())
  with check ((is_admin() or author_id = auth.uid()) and section_id = my_section());
