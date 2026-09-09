create table subjects (
  code text primary key,
  name text not null,
  sort int  not null default 0
);

insert into subjects (code, name, sort) values
  ('MC1101', 'Mathematics 1', 1),
  ('CY1107', 'Engineering Chemistry', 2),
  ('EE1108', 'Basic Electrical & Electronics Engineering', 3),
  ('ME1109', 'Manufacturing Science', 4),
  ('CY1110', 'Environmental Science', 5),
  ('BS1111', 'Biology for Engineers', 6),
  ('HS1112', 'Indian Knowledge Systems and Cognitive Well being', 7),
  ('EE1125', 'BEEE Laboratory', 8),
  ('CY1126', 'Engineering Chemistry Laboratory', 9),
  ('ME1127', 'Manufacturing Science Laboratory', 10),
  ('SA1143', 'NSS / Yoga / UHV', 11),
  ('NC1151', 'National Cadet Corps I', 12);

create table profiles (
  id         uuid primary key references auth.users on delete cascade,
  name       text not null,
  roll_no    text unique,
  status     text not null default 'pending'
             check (status in ('pending', 'approved', 'blocked')),
  trusted    boolean not null default false,
  is_admin   boolean not null default false,
  created_at timestamptz not null default now()
);
