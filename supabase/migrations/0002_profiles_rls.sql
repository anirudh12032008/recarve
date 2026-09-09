-- These two are security definer by necessity: they read profiles, and if they
-- obeyed RLS the profiles policy would recurse into itself. They return only a
-- boolean about the caller, so they leak nothing.
create or replace function is_approved() returns boolean
language sql stable security definer set search_path = public as $$
  select exists (
    select 1 from profiles where id = auth.uid() and status = 'approved'
  );
$$;

create or replace function is_admin() returns boolean
language sql stable security definer set search_path = public as $$
  select exists (
    select 1 from profiles
    where id = auth.uid() and status = 'approved' and is_admin
  );
$$;

alter table profiles enable row level security;
alter table subjects enable row level security;

create policy "read own profile always"
  on profiles for select using (id = auth.uid());

create policy "approved members read the class"
  on profiles for select using (is_approved());

create policy "admins manage profiles"
  on profiles for update using (is_admin()) with check (is_admin());

-- A member may edit their own name and roll number. status, trusted and
-- is_admin are admin-only: the with-check clause pins them to their current
-- values, so any update that changes them fails.
create policy "edit own name only"
  on profiles for update
  using (id = auth.uid())
  with check (
    id = auth.uid()
    and status   = (select p.status   from profiles p where p.id = auth.uid())
    and trusted  = (select p.trusted  from profiles p where p.id = auth.uid())
    and is_admin = (select p.is_admin from profiles p where p.id = auth.uid())
  );

create policy "everyone approved reads subjects"
  on subjects for select using (is_approved());
