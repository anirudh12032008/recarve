-- Ask AI used to leave nothing behind: the answer lived on one phone until the
-- thread redrew. Now it is an answer on the thread like any other, marked as
-- the machine's, and there is at most one live one per question -- so the
-- first tap answers it for the whole section and nobody spends the quota twice.
--
-- The row is written under whoever pressed the button, because author_id is a
-- profile and the insert policy wants it to be the caller. by_ai is what the
-- page shows instead of their name.

alter table doubts add column by_ai boolean not null default false;

alter table doubts add constraint doubts_ai_only_answers
  check (not by_ai or parent_id is not null);

-- Two taps in the same second: the second insert fails here and its caller
-- reads back the answer the first one wrote. Hiding it frees the slot again.
create unique index doubts_one_ai_answer on doubts (parent_id)
  where by_ai and deleted_at is null;

-- A classmate's answer is still 2000 characters; the machine's runs longer.
alter table doubts drop constraint doubts_body_check;
alter table doubts add constraint doubts_body_check
  check (length(btrim(body)) between 1 and case when by_ai then 8000 else 2000 end);
