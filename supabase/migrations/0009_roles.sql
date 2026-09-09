-- One role per person, decided by an admin, enforced by the database.
--
-- `trusted` and `is_admin` were two independent booleans, so (trusted = false,
-- is_admin = true) was a state the schema allowed and nothing in the app meant.
-- `role` is the single truth now. The two booleans stay, as generated columns,
-- so every policy and query that already reads them keeps working -- and they
-- cannot drift: Postgres computes them from role and refuses any write that
-- tries to set one directly, so there is no way to make them disagree.
--
-- status stays a separate axis on purpose: a pending admin is still pending,
-- which is why every helper below tests status as well as role.

alter table profiles add column role text not null default 'student'
  check (role in ('student', 'trusted', 'admin'));

update profiles set role = case when is_admin then 'admin'
                                when trusted  then 'trusted'
                                else 'student' end;

-- Both are security definer for the same reason as before: they read profiles,
-- and obeying RLS would recurse into the policy that calls them. They return
-- one boolean about the caller and leak nothing else.
create or replace function is_admin() returns boolean
language sql stable security definer set search_path = public as $$
  select exists (
    select 1 from profiles
     where id = auth.uid() and status = 'approved' and role = 'admin'
  );
$$;

-- Trusted is what upload, record and Explain cost money on. Admin is trusted
-- and more, so it is spelled once here rather than at every call site.
create or replace function is_trusted() returns boolean
language sql stable security definer set search_path = public as $$
  select exists (
    select 1 from profiles
     where id = auth.uid() and status = 'approved'
       and role in ('trusted', 'admin')
  );
$$;

grant execute on function is_trusted() to authenticated;

-- These two policies read the booleans, so Postgres will not let the columns go
-- while they exist. Dropped by name and rebuilt below, rather than dropped with
-- `cascade`, which would take whatever else it found along with them.
drop policy "edit own name only" on profiles;
drop policy "own or admin edit materials" on materials;

alter table profiles drop column trusted;
alter table profiles drop column is_admin;
alter table profiles
  add column trusted  boolean generated always as (role in ('trusted', 'admin')) stored,
  add column is_admin boolean generated always as (role = 'admin') stored;

-- A member may still edit their own name and roll number. status and role are
-- admin-only, pinned to their current values by the with-check -- and pinning
-- role pins both booleans with it, because they are role.
create policy "edit own name only"
  on profiles for update
  using (id = auth.uid())
  with check (
    id = auth.uid()
    and status = (select p.status from profiles p where p.id = auth.uid())
    and role   = (select p.role   from profiles p where p.id = auth.uid())
  );

create policy "own or admin edit materials" on materials for update
  using (is_admin() or uploader_id = auth.uid())
  with check (
    is_admin()
    or (uploader_id = auth.uid()
        and (status = (select m.status from materials m where m.id = materials.id)
             or is_trusted()))
  );

-- Writing content is a trusted act, and this is where a student is refused
-- whatever the app does. A student running curl against /upload gets a 403 from
-- the handler; a student holding a database session gets nothing at all.
drop policy "members add materials" on materials;
create policy "trusted add materials" on materials for insert
  with check (is_trusted() and uploader_id = auth.uid());

drop policy "members add lectures" on lectures;
create policy "trusted add lectures" on lectures for insert
  with check (is_trusted() and uploader_id = auth.uid());

-- `and role = 'student'` so approving somebody's backlog cannot demote an
-- admin to trusted on the way past.
create or replace function approve_uploader(p_user uuid) returns int
language plpgsql security definer set search_path = public as $$
declare
  v_count int;
begin
  if not is_admin() then
    raise exception 'admin only';
  end if;

  update profiles set role = 'trusted' where id = p_user and role = 'student';

  update materials set status = 'visible'
   where uploader_id = p_user and status = 'pending';
  get diagnostics v_count = row_count;

  return v_count;
end;
$$;
