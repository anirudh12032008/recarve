-- The branch, which the roll list has always carried and this table dropped.
--
-- 0049 took three columns off the registrar's PDF -- scholar number, roll
-- number, name -- and left the fourth behind. The branch is the one fact a
-- student most wants to see about themselves that nothing here could say.
--
-- IT IS NOT IN THE SCHOLAR NUMBER, which is the whole reason it has to be a
-- column. The number looks like it encodes the branch and very nearly does:
-- 26113011101 and 26113021101 share all of 26113 and are Electrical
-- Engineering and B.Tech in Electrical Engineering with E-Mobility, two
-- different degrees. Guessing from a substring gets 28 students' degree wrong
-- with no way to notice. The list is the authority; this column is the list.
alter table roll_list add column branch text;

-- And a member may read their own line of it. roll_list has had row level
-- security and deliberately no policy since 0049 -- the Google door reads it
-- through a security definer function, so nothing needed one -- but the
-- profile screen asks as the student, and a policy that names exactly one row
-- is a smaller thing than another definer function.
--
-- Matched folded for the same reason db_section's missing list is: a roll
-- number typed by hand at the invite door is the same roll number the
-- registrar printed, whatever the case and spacing.
create policy "your own line of the roll list" on roll_list for select
  using (upper(btrim(roll_no)) =
         (select upper(btrim(roll_no)) from profiles where id = auth.uid()));
