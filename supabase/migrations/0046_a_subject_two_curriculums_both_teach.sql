-- Mathematics 1 is taught to all ten sections, and until this file it could
-- only be taught to five.
--
-- 0040 hung set membership off `subjects.set_id`: one column, one set, one
-- curriculum per subject. That was true of the twelve codes in the database
-- when it was written, because all twelve were Set B's. The institute
-- timetable says otherwise about two of them -- MC1101 and NC1151 appear on
-- both scheme pages, Group-MT's and Group-ST's -- and a single column cannot
-- hold both answers. Filed under either set, half the institute loses Maths
-- from its library, because 0042's policy on subjects reads exactly that
-- column.
--
-- So membership becomes what it always was: a relation. A subject is in a set;
-- a subject may be in two.

create table subject_set_members (
  set_id       uuid not null references subject_sets(id) on delete cascade,
  subject_code text not null references subjects(code)   on delete cascade,
  primary key (set_id, subject_code)
);

-- The backfill is exact: every row that had a set keeps it, and nothing that
-- had none gains one.
insert into subject_set_members (set_id, subject_code)
select set_id, code from subjects where set_id is not null;

-- ---------------------------------------------------------------------------
-- The policy 0042 wrote, with the join table under it. Its own helper for the
-- reason every helper in this schema is one and 0042 spells out at length: a
-- subquery inside a policy runs with the CALLER's privileges against the
-- CALLER's policies, and this one would be consulting a table the caller is
-- granted nothing on. security definer, one boolean about one code, and it
-- still ends at my_subject_set() -- so it can widen nothing.
create or replace function in_my_subject_set(p_code text) returns boolean
language sql stable security definer set search_path = public as $$
  select exists (select 1 from subject_set_members m
                  where m.subject_code = p_code and m.set_id = my_subject_set())
$$;

drop policy "everyone approved reads their own set's subjects" on subjects;
create policy "everyone approved reads their own set's subjects"
  on subjects for select
  using (is_approved() and in_my_subject_set(code));

-- And only now, with nothing left depending on it. The column has to outlive
-- the policy that reads it, which is why this line is here and not up beside
-- the backfill: postgres refuses to drop a column a live policy names, and
-- `cascade` would take the policy with it and leave subjects readable by
-- everybody for as long as it took somebody to notice.
alter table subjects drop column set_id;

-- Row level security with no policy: the mapping is read through the helper
-- above, which is security definer and does not consult it, and through the
-- owning connection /super runs on, which bypasses it. Nobody else has
-- business listing another curriculum's contents. The grant is stated anyway,
-- because a table's own migration is where its privileges are written down
-- (0031) and because "RLS on, no grant" is how a student got "permission
-- denied for table doubts" in production.
alter table subject_set_members enable row level security;
grant select on subject_set_members to authenticated;
