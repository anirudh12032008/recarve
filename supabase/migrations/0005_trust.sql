-- security definer because it writes profiles.trusted, which the
-- "edit own name only" policy forbids. It gates itself on is_admin() first.
create or replace function approve_uploader(p_user uuid) returns int
language plpgsql security definer set search_path = public as $$
declare
  v_count int;
begin
  if not is_admin() then
    raise exception 'admin only';
  end if;

  update profiles set trusted = true where id = p_user;

  update materials set status = 'visible'
   where uploader_id = p_user and status = 'pending';
  get diagnostics v_count = row_count;

  return v_count;
end;
$$;

revoke all on function approve_uploader(uuid) from public;
grant execute on function approve_uploader(uuid) to authenticated;
