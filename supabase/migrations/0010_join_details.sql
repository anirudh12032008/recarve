-- Joining is one form on a phone, and this is everything it now asks for.
--
-- The number is the only one of the three an admin cannot look up afterwards:
-- a roll number is on every list in the institute, a name is on the notes, and
-- a phone is how somebody gets told their upload is silent or their code has
-- expired. So it is collected at the door or not at all.
--
-- Nullable on purpose. Every profile that predates this column joined without
-- being asked, and a not-null column would have to invent a number for them or
-- lock them out of their own library. Not unique either: two siblings on one
-- handset is a real thing here, and a unique index would refuse the second one
-- with the wrong sentence about the wrong field.
--
-- Normalisation lives in the app (notes.normalise_phone), not in a check
-- constraint: the joiner has to be told what a good number looks like, and a
-- constraint can only say that theirs violated something.
alter table profiles add column phone text;

-- One function, not two. `create or replace` with a new argument would leave
-- the three-argument version standing beside this one, and a caller that never
-- heard about phone numbers would keep resolving to it and dropping the number
-- on the floor in silence. The default is what keeps a genuinely numberless
-- caller -- the CLI's own bootstrap, the older invite tests -- working.
drop function join_with_invite(text, text, text);

create or replace function join_with_invite(
  p_code text, p_name text, p_roll_no text, p_phone text default null
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

  insert into profiles (id, name, roll_no, phone)
  values (auth.uid(), p_name, p_roll_no, p_phone)
  on conflict (id) do nothing;

  return true;
end;
$$;

revoke all on function join_with_invite(text, text, text, text) from public;
grant execute on function join_with_invite(text, text, text, text) to authenticated;
