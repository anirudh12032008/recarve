-- The worker's single entry point. `for update skip locked` is what makes two
-- workers safe: the second steps over the row the first has locked rather than
-- blocking on it or claiming it twice.
create or replace function claim_lecture() returns setof lectures
language sql security definer set search_path = public as $$
  update lectures
     set status = 'transcribing', claimed_at = now(), attempts = attempts + 1
   where id = (
     select id from lectures
      where status = 'queued'
         or (status = 'transcribing' and claimed_at < now() - interval '2 hours')
      order by recorded_at
      limit 1
      for update skip locked
   )
  returning *;
$$;

revoke all on function claim_lecture() from public;
revoke all on function claim_lecture() from authenticated;
grant execute on function claim_lecture() to service_role;
