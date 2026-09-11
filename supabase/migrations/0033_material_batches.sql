-- Several files, one title. A phone photographing eight pages of a handout
-- was always eight separate uploads before this, each named after whatever
-- the camera called it -- eight meaningless rows in a subject's shelf where
-- one, called "Unit 3 handout", was the actual thing somebody wanted to find.
--
-- Both columns are nullable and every existing material keeps them null. That
-- is not a placeholder for a later migration to fill in -- it is the whole
-- backward-compatible story: a lone upload (the only kind that has ever
-- existed until now) has no batch and no shared title, and the phone renders
-- it exactly as it always has, off its own filename. Grouping only happens
-- where a batch_id says several rows belong together, which is true of
-- nothing that existed before this file ran.
--
-- No new table for this. A material_groups table would need its own RLS, its
-- own grants, and a foreign key materials would have to earn its way onto --
-- three moving parts for what a shared opaque id already says on the rows
-- that already exist. The database is not asked to enforce that a batch's
-- rows agree on title or subject; the upload handler is the one place a batch
-- is ever created, and it is the one place that has to get that right.
alter table materials add column batch_id uuid;
alter table materials add column title text;

-- The one read a grouped view makes: everything in one batch, together.
create index on materials (batch_id) where batch_id is not null;
