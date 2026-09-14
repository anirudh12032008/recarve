-- Two tables have been reachable in the test database by privileges their own
-- migrations never asked for, and two tests have been failing for it.
--
-- backend/dev/migrate.py runs `alter default privileges in schema public grant
-- all on tables to authenticated` BEFORE it applies any migration, because
-- Supabase grants that way and plain Postgres does not. Every table created
-- after that line therefore arrives fully granted -- select, insert, update,
-- delete -- and a migration's own `grant select, insert on ...` adds nothing
-- and takes nothing away. The grant is not the floor it reads as; it is a
-- no-op on top of a table that already has everything.
--
-- 0035 and 0036 already know this and each carry a `revoke all` before their
-- grants. 0025_doubts.sql and 0032_bookmarks.sql predate the lesson:
--
--   doubts    grants select, insert, update -- and DELETE was never revoked,
--             so test_nobody_may_destroy_a_doubt_even_their_own has been
--             deleting a doubt the schema says nobody may destroy.
--   bookmarks grants select, insert, delete -- and UPDATE was never revoked,
--             so test_one_student_never_sees_another has been rewriting a
--             bookmark title the schema says is rewritable by nobody, its
--             owner included.
--
-- THIS CHANGES NOTHING IN PRODUCTION, and that is the point rather than a
-- caveat. Production has no default privileges: a table arrives there with
-- nothing granted, the `grant` in 0025 and 0032 is the whole of what those
-- roles hold, and the delete and the update were already refused. The two
-- failures were the test database being MORE permissive than the real one --
-- the direction that hides a hole rather than inventing one. Applying this
-- file to live revokes privileges nobody has and re-grants the ones they
-- already had.
--
-- Written as a new migration rather than as two edits to 0025 and 0032,
-- because both of those have been applied to the live database. Editing an
-- applied migration changes no database anywhere and leaves the file
-- disagreeing with what actually ran.

-- doubts: a question may be asked, read and edited. It is taken down by
-- setting a flag (0025's own "deleting hides the row rather than destroying
-- it"), never by deleting the row, because a hundred and ten people may have
-- read it.
revoke all on doubts from authenticated;
grant select, insert, update on doubts to authenticated;

-- bookmarks: saved, listed and unsaved. Never edited -- a bookmark's title is
-- the lecture's title, and the row is replaced rather than rewritten.
revoke all on bookmarks from authenticated;
grant select, insert, delete on bookmarks to authenticated;
