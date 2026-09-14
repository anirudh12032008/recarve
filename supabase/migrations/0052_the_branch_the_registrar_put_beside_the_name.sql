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

-- One seat, however it was typed. '26I060', 'I060' and 'I60' all come out I60.
-- immutable so a policy and an index may both use it.
create or replace function same_seat(roll text) returns text
language sql immutable as $$
  select regexp_replace(
           regexp_replace(upper(btrim(coalesce(roll, ''))), '^[0-9]{2}', ''),
           '^([A-Z])0*', '\1')
$$;

-- And a member may read their own line of it. roll_list has had row level
-- security and deliberately no policy since 0049 -- the Google door reads it
-- through a security definer function, so nothing needed one -- but the
-- profile screen asks as the student, and a policy that names exactly one row
-- is a smaller thing than another definer function.
--
-- Matched on the SEAT and not on the string, which is the same thing
-- roll_variants() says in Python: the registrar writes 26I060 and the people
-- who joined by invite before anybody had his file wrote I60. Strip the
-- two-digit intake, then the zeros the list pads with, and both are I60.
--
-- Two expressions of one rule is exactly the drift this schema has been bitten
-- by before, so test_the_policy_and_roll_variants_agree_on_a_seat holds them
-- together: it asks the database and the function about the same spellings and
-- fails the day either one moves.
create policy "your own line of the roll list" on roll_list for select
  using (same_seat(roll_no) =
         (select same_seat(roll_no) from profiles where id = auth.uid()));
