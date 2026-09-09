create table lectures (
  id           uuid primary key default gen_random_uuid(),
  subject_code text not null references subjects(code),
  uploader_id  uuid not null references profiles(id),
  title        text,
  audio_key    text not null,
  status       text not null default 'queued'
               check (status in ('queued','transcribing','done','failed')),
  claimed_at   timestamptz,
  attempts     int not null default 0,
  error        text,
  transcript   text,
  notes_md     text,
  recorded_at  timestamptz not null default now()
);

create table materials (
  id           uuid primary key default gen_random_uuid(),
  subject_code text not null references subjects(code),
  uploader_id  uuid not null references profiles(id),
  filename     text not null,
  file_key     text not null,
  size_bytes   bigint not null,
  status       text not null default 'visible'
               check (status in ('pending','visible','removed')),
  created_at   timestamptz not null default now()
);

create table votes (
  material_id uuid not null references materials(id) on delete cascade,
  voter_id    uuid not null references profiles(id) on delete cascade,
  created_at  timestamptz not null default now(),
  primary key (material_id, voter_id)
);

create table reports (
  id          uuid primary key default gen_random_uuid(),
  material_id uuid references materials(id) on delete cascade,
  reporter_id uuid not null references profiles(id),
  reason      text,
  created_at  timestamptz not null default now()
);

create index on lectures (subject_code, recorded_at desc);
create index on materials (subject_code, created_at desc);
create index on lectures (status) where status in ('queued','transcribing');

-- The client sends whatever status it likes; the database decides. A first
-- upload from an untrusted member is always pending, whatever the payload says.
create or replace function force_pending_for_untrusted() returns trigger
language plpgsql security definer set search_path = public as $$
begin
  if not exists (select 1 from profiles where id = new.uploader_id and trusted) then
    new.status := 'pending';
  end if;
  return new;
end;
$$;

create trigger materials_force_pending
  before insert on materials
  for each row execute function force_pending_for_untrusted();

alter table lectures  enable row level security;
alter table materials enable row level security;
alter table votes     enable row level security;
alter table reports   enable row level security;

create policy "members read lectures" on lectures for select using (is_approved());
create policy "members add lectures"  on lectures for insert
  with check (is_approved() and uploader_id = auth.uid());
create policy "own or admin edit lectures" on lectures for update
  using (is_admin() or uploader_id = auth.uid());
create policy "own or admin delete lectures" on lectures for delete
  using (is_admin() or uploader_id = auth.uid());

create policy "members read visible materials" on materials for select
  using (is_approved() and (status = 'visible' or uploader_id = auth.uid() or is_admin()));
create policy "members add materials" on materials for insert
  with check (is_approved() and uploader_id = auth.uid());
-- The uploader may edit their own row but not publish it: status is pinned to
-- its current value unless they are trusted. Without this the trigger above is
-- only an insert-time speed bump -- a newcomer would upload (forced pending),
-- then flip status to 'visible' with one more request. Same shape as the
-- "edit own name only" policy on profiles. Admins are exempt, which is how
-- approve_uploader's publish step stays possible from a normal session.
create policy "own or admin edit materials" on materials for update
  using (is_admin() or uploader_id = auth.uid())
  with check (
    is_admin()
    or (uploader_id = auth.uid()
        and (status = (select m.status from materials m where m.id = materials.id)
             or exists (select 1 from profiles p
                         where p.id = auth.uid() and p.trusted)))
  );
create policy "own or admin delete materials" on materials for delete
  using (is_admin() or uploader_id = auth.uid());

create policy "members read votes" on votes for select using (is_approved());
create policy "vote as yourself"   on votes for insert
  with check (is_approved() and voter_id = auth.uid());
create policy "unvote your own"    on votes for delete using (voter_id = auth.uid());

create policy "report as yourself"  on reports for insert
  with check (is_approved() and reporter_id = auth.uid());
create policy "admins read reports" on reports for select using (is_admin());
