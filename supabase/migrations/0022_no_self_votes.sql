-- You cannot upvote your own upload.
--
-- Votes stopped being decoration the day the board went public: votes_received
-- is a third of the score, so an uploader who votes for themselves buys a point
-- per upload, and toggling that same vote off and on again re-dates it -- which
-- keeps them on the rolling seven-day board forever without adding anything.
-- One clause closes both, because a classmate's vote is then the only vote that
-- can move either window.
--
-- The referee stays in the database, next to the primary key that already
-- refuses the second vote. A check in the handler would be a check one leaked
-- connection string walks past.
--
-- `not exists` rather than `voter_id <> (select uploader_id ...)`: a vote for a
-- material that does not exist has to keep failing on the foreign key, so the
-- server can still answer "no such item" instead of "you cannot upvote your
-- own". Your own uploads are always visible to you, pending ones included, so
-- there is no row this subselect can be blinded to by RLS.
drop policy "vote as yourself" on votes;
create policy "vote as yourself" on votes for insert
  with check (
    is_approved() and voter_id = auth.uid()
    and not exists (select 1 from materials m
                     where m.id = material_id and m.uploader_id = voter_id)
  );
