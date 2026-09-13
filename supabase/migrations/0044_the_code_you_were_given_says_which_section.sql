-- The door. Everything above this file scopes rows that already belong to
-- somebody; this is how somebody comes to belong to a section in the first
-- place.
--
-- profiles.section_id is not null as of 0041, so joining has to produce one,
-- and there is exactly one signup path: join_with_invite. The question is only
-- where it should read the answer from, and the invite is the honest place --
-- it is the one thing the joiner was given by a person who knew which section
-- they meant. An admin mints a code for their own section (0041 gave invites
-- the column, 0042 made an admin's codes their own section's), the code
-- travels by WhatsApp, and whoever types it lands where the admin intended.
--
-- The spec does not name invites in either list, because it is thinking about
-- content and an invite is not content. But the not-null column forces the
-- question, and the alternatives are worse: a section chosen by the app is a
-- section a request can choose for itself, and a section defaulted to the
-- oldest one puts every future joiner into Section I forever.
--
-- Note what this does NOT change: the signature, the password rule, the
-- returns-boolean-only contract. The function still tells a wrong code nothing
-- but false, because a code that exists and a code that does not must stay
-- indistinguishable from outside.
create or replace function join_with_invite(
  p_code text, p_name text, p_roll_no text, p_phone text, p_password text
) returns boolean
language plpgsql security definer set search_path = public as $$
declare
  v_section uuid;
begin
  if auth.uid() is null then
    return false;
  end if;

  if p_password is null or length(btrim(p_password)) = 0 then
    raise exception 'a password is required to join';
  end if;

  -- The section comes back with the use, in the same statement, so there is no
  -- window in which the invite could be read twice and answer differently.
  -- It is not null on the table, so "we got a section" and "the code was good"
  -- are the same fact and one variable holds both.
  update invites
     set uses = uses + 1
   where code = p_code
     and expires_at > now()
     and uses < max_uses
  returning section_id into v_section;

  if v_section is null then
    return false;
  end if;

  -- section_id said out loud rather than left to the column default. The
  -- default is default_section(), which for a caller who has no profile yet --
  -- which is every caller of this function, by definition -- falls back to the
  -- oldest section. That is the right answer for the CLI bootstrap and the
  -- wrong one for the second section's first student.
  insert into profiles (id, name, roll_no, phone, password, section_id)
  values (auth.uid(), p_name, p_roll_no, p_phone, p_password, v_section)
  on conflict (id) do nothing;

  return true;
end;
$$;

-- Restated, because `create or replace` does not touch privileges and a file
-- that stands alone has to leave the function reachable by the one role that
-- calls it -- a brand new member, holding nothing else.
revoke all on function join_with_invite(text, text, text, text, text) from public;
grant execute on function join_with_invite(text, text, text, text, text) to authenticated;
