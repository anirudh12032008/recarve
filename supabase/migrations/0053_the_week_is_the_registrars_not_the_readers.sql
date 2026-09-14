-- A student's week is read by them and written by nobody they are.
--
-- 0008 made the timetable per-person and editable by its owner, because the
-- institute grid had never been typed in and each student filled their own in.
-- That is no longer true and has not been for a while: the grid lives in
-- section_timetable, one per section, an admin pastes it, the trigger on
-- profiles seeds a new member from it and db_reseed_section_weeks pushes a
-- correction onto everyone already there. A student editing their copy could
-- only ever drift AWAY from the published week, and a drifted copy files
-- lectures under the wrong subject in silence -- which is the one failure this
-- table was built to avoid.
--
-- So the editor and POST /timetable are gone from notes.py. This is the same
-- removal said where it is enforced: the route table is not a security
-- boundary, and a policy is. Without this file, the only thing standing
-- between a member and their own Monday is a handler that no longer exists --
-- and the next handler that touches this table inherits a write grant nobody
-- meant to give it.
--
-- Both halves, because they answer different questions. The grant decides
-- whether the role may reach the table at all; the policy decides which rows.
-- 0031 granted insert, update and delete here alongside the read, and 0051 is
-- the lesson about why revoking first is not optional: the test database
-- arrives fully granted by default privileges, so `grant select` on its own
-- adds nothing and takes nothing away, and the hole would stay open in exactly
-- the database where the tests run.
--
-- NOTHING THAT STILL WRITES GOES THROUGH EITHER. seed_timetable_for_new_profile
-- is security definer and runs as the owner (0020, rewritten in 0043), and both
-- callers of db_reseed_section_weeks are on notes.py's owning connection -- the
-- same connection that already replaces section_timetable. The authenticated
-- role is the only one narrowed here, and it is the only one that was reading.

revoke all on timetable from authenticated;
grant select on timetable to authenticated;

-- Same rows as 0042 -- your own, in your own section -- and only readable now.
-- `for select` rather than four policies minus three: there is one rule here,
-- and it is the same rule the read half always had.
drop policy if exists "your own timetable" on timetable;
create policy "your own timetable" on timetable for select
  using (profile_id = auth.uid() and section_id = my_section());
