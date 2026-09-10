-- A password is picked at the door, so nobody ever exists in a claimable state.
--
-- Roll numbers are public here: they are on every list in the institute. Until
-- now a member who had joined and not yet logged in had a null password, and
-- login accepted their roll number for it -- so the first classmate to type it
-- became them, set a password, and locked the real person out for good. The
-- admin's own account was reachable that way too.
--
-- The fix is that the window never opens. join_with_invite takes the password
-- and writes it in the same insert that creates the row, and there is no
-- default on it: a caller that has not been taught about passwords fails loudly
-- rather than quietly creating an account anybody can claim.
--
-- roll_login is what is left of the old behaviour, and it is opt-in per row
-- rather than "wherever the password happens to be null". It is true in exactly
-- two circumstances: the accounts that already existed when this migration ran,
-- and somebody an admin has just reset. Both are a person the class already
-- knows about and is expecting. A new joiner cannot reach it -- default false
-- and a non-null password each rule it out on their own -- and setting a
-- password is the only thing that clears it.
--
-- Passwords stay as typed. That is the owner's decision and 0012 records it.
alter table profiles add column roll_login boolean not null default false;

-- The accounts that predate the door.
update profiles set roll_login = true where password is null;

-- One function, not two, for the reason 0010 gives: `create or replace` with a
-- new argument leaves the old arity standing beside it, and the old arity is
-- the one that creates an account with no password at all. p_phone loses its
-- default so that p_password can go without one -- Postgres will not let a
-- required parameter follow an optional one, and of the two it is the password
-- that must never be omissible.
drop function join_with_invite(text, text, text, text);

create or replace function join_with_invite(
  p_code text, p_name text, p_roll_no text, p_phone text, p_password text
) returns boolean
language plpgsql security definer set search_path = public as $$
declare
  v_ok boolean;
begin
  if auth.uid() is null then
    return false;
  end if;

  -- Raised, not `return false`: a blank password is not a wrong invite code,
  -- and the joiner must not be told it was. How long a password has to be and
  -- what it may not equal is the app's to say (notes.check_password), because
  -- the rule has to be on screen before anybody types. That one exists at all
  -- is this table's business, and it is said here.
  if p_password is null or length(btrim(p_password)) = 0 then
    raise exception 'a password is required to join';
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

  insert into profiles (id, name, roll_no, phone, password)
  values (auth.uid(), p_name, p_roll_no, p_phone, p_password)
  on conflict (id) do nothing;

  return true;
end;
$$;

revoke all on function join_with_invite(text, text, text, text, text) from public;
grant execute on function join_with_invite(text, text, text, text, text) to authenticated;
