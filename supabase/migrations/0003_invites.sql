create table invites (
  code       text primary key,
  created_by uuid references profiles(id),
  expires_at timestamptz not null,
  max_uses   int not null default 200,
  uses       int not null default 0
);

alter table invites enable row level security;

-- Admins only. There is deliberately no select policy for members, so codes
-- cannot be listed by anyone holding a normal session.
create policy "admins manage invites" on invites for all
  using (is_admin()) with check (is_admin());

-- Signup cannot use RLS: the caller has no profile yet. This function is the
-- single doorway in. It returns only true/false, never invite data, so codes
-- cannot be discovered by probing.
create or replace function join_with_invite(
  p_code text, p_name text, p_roll_no text
) returns boolean
language plpgsql security definer set search_path = public as $$
declare
  v_ok boolean;
begin
  if auth.uid() is null then
    return false;
  end if;

  update invites
     set uses = uses + 1
   where code = p_code
     and expires_at > now()
     and uses < max_uses
  returning true into v_ok;

  if not coalesce(v_ok, false) then
    return false;
  end if;

  insert into profiles (id, name, roll_no)
  values (auth.uid(), p_name, p_roll_no)
  on conflict (id) do nothing;

  return true;
end;
$$;

revoke all on function join_with_invite(text, text, text) from public;
grant execute on function join_with_invite(text, text, text) to authenticated;
