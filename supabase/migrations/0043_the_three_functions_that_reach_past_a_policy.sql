-- 0042 is not the whole boundary, because three functions run around it.
--
-- A SECURITY DEFINER function executes as the user that owns it, and the owner
-- of these tables is the user row level security does not apply to. That is
-- deliberate and necessary -- approve_uploader has to write a column the
-- "edit own name only" policy forbids -- but it means every one of them is a
-- hole in 0042 unless it says the section clause itself.
--
-- So this file is the audit. Every SECURITY DEFINER function in the schema was
-- read; these three touch rows that belong to somebody, and the rest do not:
--
--   is_approved, is_admin, is_trusted, my_section, my_subject_set,
--   default_section  -- one fact about the CALLER. Nothing to scope.
--   force_pending_for_untrusted, no_self_demotion, posts_daily_limit,
--   messages_rate_limit, votes_not_your_own_post
--                    -- triggers that look only at the row being written and
--                       its own author. A section clause would say nothing.
--   claim_lecture    -- the worker's, granted to service_role alone. It is
--                       SUPPOSED to reach every section: there is one worker
--                       and it transcribes for all of them. Left alone, and
--                       this is the deliberate exception.
--   is_teaching_day  -- reads academic_calendar, which is institute-wide.

-- ---------------------------------------------------------------------------
-- approve_uploader: an admin promotes somebody and publishes their backlog.
--
-- Without the two clauses below, an admin of Section I promotes a member of
-- Section II to trusted and publishes their pending uploads -- across a
-- boundary they cannot even see over, in a function that was written when
-- there was only one section to be an admin of. It takes a bare uuid, so the
-- only thing between it and any profile in the database is this sentence.
--
-- `and role = 'student'` stays for 0009's reason: approving somebody's backlog
-- must not demote an admin on the way past.
create or replace function approve_uploader(p_user uuid) returns int
language plpgsql security definer set search_path = public as $$
declare
  v_count int;
begin
  if not is_admin() then
    raise exception 'admin only';
  end if;

  update profiles set role = 'trusted'
   where id = p_user and role = 'student' and section_id = my_section();

  update materials set status = 'visible'
   where uploader_id = p_user and status = 'pending'
     and section_id = my_section();
  get diagnostics v_count = row_count;

  return v_count;
end;
$$;

-- ---------------------------------------------------------------------------
-- confession_author: the one deliberate road from a confession to a name.
--
-- 0035 keeps this admin-only and off every screen, because somebody will post
-- something vile and an admin has to be able to deal with the person. An admin
-- of ANOTHER section is not that admin. It takes a bare uuid too, and the
-- answer it gives is the single most private fact in this database.
create or replace function confession_author(p_id uuid) returns text
language sql stable security definer set search_path = public as $$
  select p.name from posts po join profiles p on p.id = po.author_id
   where po.id = p_id and po.kind = 'confession'
     and po.section_id = my_section() and is_admin();
$$;

-- ---------------------------------------------------------------------------
-- seed_timetable_for_new_profile: a new member's week, copied from the
-- template.
--
-- This one is not a leak, it is a wrong answer: there are as many templates as
-- there are sections now, and the unfiltered select would hand a new Section
-- II student every period of Section I's week -- and, because the rows land in
-- `timetable` whose default is the OWNER's fallback section rather than
-- theirs, hand it to them labelled Section I as well. Both fixed by saying the
-- new profile's own section twice: once to choose the template, once to file
-- what comes out of it.
create or replace function seed_timetable_for_new_profile() returns trigger
language plpgsql security definer set search_path = public as $$
begin
  insert into timetable (profile_id, section_id, day, period, subject_code)
  select new.id, new.section_id, s.day, s.period, s.subject_code
    from section_timetable s
   where s.section_id = new.section_id
  on conflict (profile_id, day, period) do nothing;
  return new;
end;
$$;
