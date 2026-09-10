-- The class board, and the one place the weights are written down.
--
-- Ranking is a window function over the class, never a sort in Python: the
-- caller asks for the top of the board and Postgres decides who that is, so a
-- screen opened on mobile data carries twenty rows and not a hundred and ten.
--
-- security_invoker is not optional here for the same reason it is not optional
-- on points: without it this view runs as its owner and hands the whole class's
-- contributions to a pending stranger. With it, RLS on profiles/materials/
-- lectures/votes runs first and rank() is computed over exactly the rows the
-- person asking is allowed to see.
--
-- Two windows over the same counts. All-time is the honest record; a rolling
-- seven days is what keeps somebody who joined in month three from being
-- permanently locked out of the top. Rolling rather than calendar: a Monday
-- reset empties the board on Monday morning, which is when people look at it.
--
-- rank(), not row_number(): equal work shares a place, and 1,1,3 is what a tie
-- means. The window deliberately orders by score alone -- adding name to it
-- would turn a genuine tie into an alphabetical win -- so the caller breaks the
-- display order by name afterwards.
--
-- Independent subselects rather than joins, for the reason 0006 gives: two
-- recordings would otherwise multiply every vote on the same person's notes.
create view standings with (security_invoker = true) as
select id, name, role,
       uploads, recordings, votes_received, score,
       week_uploads, week_recordings, week_votes, week_score,
       rank() over (order by score desc)      as rank,
       rank() over (order by week_score desc) as week_rank
from (
  select id, name, role,
         uploads, recordings, votes_received,
         uploads * 5 + recordings * 10 + votes_received       as score,
         week_uploads, week_recordings, week_votes,
         week_uploads * 5 + week_recordings * 10 + week_votes as week_score
  from (
    select p.id, p.name, p.role,
      (select count(*) from materials m
        where m.uploader_id = p.id and m.status = 'visible')      as uploads,
      (select count(*) from lectures l
        where l.uploader_id = p.id and l.status = 'done')         as recordings,
      (select count(*) from votes v
         join materials m on m.id = v.material_id
        where m.uploader_id = p.id and m.status = 'visible')      as votes_received,
      -- The same three counts again, inside the window. now() is the
      -- transaction's clock, so the boundary moves with the request rather
      -- than with whenever this view was created.
      (select count(*) from materials m
        where m.uploader_id = p.id and m.status = 'visible'
          and m.created_at > now() - interval '7 days')           as week_uploads,
      (select count(*) from lectures l
        where l.uploader_id = p.id and l.status = 'done'
          and l.recorded_at > now() - interval '7 days')          as week_recordings,
      (select count(*) from votes v
         join materials m on m.id = v.material_id
        where m.uploader_id = p.id and m.status = 'visible'
          and v.created_at > now() - interval '7 days')           as week_votes
    from profiles p
  ) c
) t;

grant select on standings to authenticated;

-- points is a window onto standings now rather than a second copy of the
-- weights. Same columns, same types, same security_invoker, so everything that
-- already reads it keeps working -- and 5/10/1 is written down once, where a
-- change to it cannot make the profile and the board disagree.
create or replace view points with (security_invoker = true) as
select id, uploads, recordings, votes_received, score from standings;
