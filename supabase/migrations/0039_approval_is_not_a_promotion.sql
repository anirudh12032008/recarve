-- Approving a joiner lets them in. It does not hand them the keys.
--
-- 0009 folded two decisions into one statement: approve_uploader() published
-- whatever the person had waiting AND promoted them from student to trusted,
-- on the reasoning that anybody with a backlog was already an uploader. That
-- reasoning stopped being true the day the admin panel's Approve button became
-- the ordinary way a new member gets in. Every joiner goes through db_approve,
-- db_approve calls this function, and so every joiner came out trusted --
-- which is the role that can upload, record and spend money on Explain. The
-- class was one tap away from anyone who guessed an invite code.
--
-- profiles.role already defaults to 'student' (0009, line 13) and nothing else
-- in the schema overrides it, so removing the one update here is the whole
-- fix: a joiner is approved as a student, and trusted becomes what it should
-- always have been -- a thing an admin grants deliberately, one person at a
-- time, from the role dropdown in the admin panel.
--
-- Everything else the function did is kept exactly: the is_admin() gate (this
-- is security definer, so that gate is the only thing standing between any
-- logged-in student and publishing their own pending files), the flip of that
-- uploader's pending materials to visible, and the row count it returns, which
-- is what the handler logs and the panel shows.
--
-- No revoke/grant pair below on purpose: `create or replace` keeps the ACL
-- 0005 set on this function, and re-granting here would be a no-op that reads
-- like a privilege change.
create or replace function approve_uploader(p_user uuid) returns int
language plpgsql security definer set search_path = public as $$
declare
  v_count int;
begin
  if not is_admin() then
    raise exception 'admin only';
  end if;

  update materials set status = 'visible'
   where uploader_id = p_user and status = 'pending';
  get diagnostics v_count = row_count;

  return v_count;
end;
$$;
