-- Points are derived, never stored, so they cannot drift out of sync.
--
-- security_invoker is not optional: without it this view runs as its owner and
-- hands every member's score to anyone who asks, ignoring the policies above.
--
-- Three independent counts rather than three left joins: materials and lectures
-- joined on the same profile multiply each other's rows, so a member with two
-- recordings would have every vote on their notes counted twice.
create view points with (security_invoker = true) as
select id, uploads, recordings, votes_received,
       uploads * 5 + recordings * 10 + votes_received as score
from (
  select p.id,
         (select count(*) from materials m
           where m.uploader_id = p.id and m.status = 'visible')   as uploads,
         (select count(*) from lectures l
           where l.uploader_id = p.id and l.status = 'done')      as recordings,
         (select count(*) from votes v
            join materials m on m.id = v.material_id
           where m.uploader_id = p.id and m.status = 'visible')   as votes_received
  from profiles p
) t;

grant select on points to authenticated;
