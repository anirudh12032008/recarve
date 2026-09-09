-- The one rule the admin panel cannot be trusted to keep: an admin may not
-- demote or block themselves.
--
-- Not in the UI, and not only in db_set_role. The screen hides your own row and
-- the handler refuses your own id, but both of those are one request away from
-- being bypassed by curl -- and the cost is not a privilege escalation, it is
-- a class with no admin at all. Nobody left who can approve the next joiner,
-- mint an invite, or promote anybody back, and no way to fix it from inside the
-- app. So the last word is here.
--
-- A trigger rather than a policy clause: the "admins manage profiles" policy
-- would have to compare OLD and NEW to say this, and a with-check clause cannot
-- see OLD. This can, and it fires whatever statement reaches the table.
--
-- auth.uid() is null on the owning connection -- the CLI's bootstrap, the
-- first-admin promotion in db_join, backend/dev/migrate.py -- which is the one
-- caller that is not somebody acting on themselves. It passes through.
create or replace function no_self_demotion() returns trigger
language plpgsql set search_path = public as $$
begin
  if auth.uid() is null or new.id <> auth.uid() or old.role <> 'admin' then
    return new;            -- somebody else's row, or not an admin's to lose
  end if;
  if new.role <> 'admin' then
    raise exception 'an admin cannot change their own role';
  end if;
  if old.status = 'approved' and new.status <> 'approved' then
    raise exception 'an admin cannot block themselves';
  end if;
  return new;
end;
$$;

create trigger profiles_no_self_demotion
  before update on profiles
  for each row execute function no_self_demotion();
