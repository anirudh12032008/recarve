-- Say the grants out loud, and take back the three nobody needs.
--
-- Two problems, one cause. Production's privileges were applied by hand during
-- setup -- a blanket `grant all on all tables` -- and never written down here.
-- backend/dev/migrate.py sets its own default privileges before it runs, so the
-- test database was reachable too, and neither one was reachable BECAUSE of a
-- migration. Which meant:
--
-- 1. A migration could create a table, write its policies, forget its grant,
--    pass every test and then tell the first student "permission denied for
--    table doubts". That is not hypothetical; it is what 0025 did, in
--    production, minutes after it was deployed.
--
-- 2. Rebuilding production from these files -- after a restore, or on a second
--    machine -- would bring up almost every table unreachable, because the
--    hand-typed grant is not in here to be replayed.
--
-- TRUNCATE is the one worth taking back today. Row level security does not
-- apply to it: a policy that lets a student see only their own attendance does
-- nothing to stop `truncate attendance` from removing all 110 people's. The app
-- never truncates anything, so this costs nothing and closes the distance
-- between "a member can read one row they should not" and "a member can empty
-- the table". No member can reach raw SQL today -- the web app is the only way
-- in, and it sets role authenticated on its own connection -- so this is depth,
-- not a hole being plugged.
--
-- REFERENCES and TRIGGER go the same way and for the same reason: an
-- application role has no business adding a foreign key or a trigger, and both
-- were swept in by the same `grant all`.
--
-- What is deliberately NOT changed here is which tables a member may write.
-- Those grants are what the app runs on right now, and narrowing them is a
-- separate change that needs its own reading of every handler. This migration
-- makes the grants reproducible and drops the three that are never used; it
-- does not re-litigate the rest.
-- information_schema.tables rather than pg_tables, because pg_tables holds no
-- views and the board is drawn from two of them. A view cannot be truncated in
-- any case, but a grant that means nothing is still a grant somebody has to
-- reason about later.
do $$
declare t record;
begin
  for t in select table_name from information_schema.tables
            where table_schema = 'public' loop
    execute format('revoke truncate, references, trigger on public.%I from authenticated',
                   t.table_name);
  end loop;
end $$;

-- And now state the rest, so a rebuild from these files lands where production
-- already is. Idempotent: granting what is already granted is a no-op, which is
-- what makes this safe to run against the live database.
grant select, insert, update, delete on
  profiles, invites, lectures, materials, votes, reports,
  timetable, section_timetable, announcements, announcement_reads
  to authenticated;

-- Read-only, because these are computed or published rather than written by a
-- member: the two views the board is drawn from, the subject list, and the
-- institute's calendar.
grant select on points, standings, subjects, academic_calendar to authenticated;

-- The three that already say their own grants, restated so this file alone is
-- enough to rebuild the schema's privileges.
grant select, insert, update, delete on attendance to authenticated;
grant select, insert, delete on cancelled_classes to authenticated;
grant select, insert, update on doubts to authenticated;
