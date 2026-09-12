-- MANIT Bhopal: its societies, the fests they run, and where things are.
-- Data, not schema: applied to the live database only, never to the test one
-- (backend/dev/migrate.py rebuilds recarve_test from supabase/migrations alone,
-- and a suite that starts with twenty-four clubs in it has to filter around
-- them in every test). Schema for all three tables is in 0034 and 0038.
--
-- Re-runnable: every insert is `on conflict do update`, so fixing a blurb here
-- and running this again corrects the row instead of refusing or duplicating.

insert into clubs (slug, name, blurb, category, tags) values
  ('students-council', 'Students'' Council MANIT',
   'The elected student body — runs Maffick and Techno and speaks for students to the administration.',
   'Student body', '{leadership,student welfare,events}'),
  ('evolve', 'Evolve',
   'Electric vehicle innovation: builds EVs, runs Vidyut, and mentors juniors into the workshop.',
   'Technical', '{EV innovation,technical,mentorship}'),
  ('ieee', 'IEEE MANIT Student Branch',
   'The student branch of IEEE — talks, workshops and paper writing across electronics and computing.',
   'Technical', '{electronics,computing,research}'),
  ('robotics', 'Robotics Club',
   'Builds robots and teaches the building of them, from line followers to autonomous arms.',
   'Technical', '{robotics,automation,AI}'),
  ('vision', 'Vision',
   'The official technical society — design, art and the visual side of everything the institute makes.',
   'Technical', '{design,art,creativity}'),
  ('e-cell', 'E-Cell MANIT',
   'Entrepreneurship cell: startup mentoring, incubation and the annual E-Summit.',
   'Entrepreneurship', '{entrepreneurship,startups,business incubation}'),
  ('iste', 'ISTE Students'' Chapter MANIT',
   'Technical education chapter — coding competitions, workshops and tech summits.',
   'Technical', '{technical,coding competitions,tech summits}'),
  ('ibc', 'Intellect Browsers'' Consortium (IBC)',
   'Business and strategy society: case competitions, market puzzles and consulting-style problem solving.',
   'Business', '{business,strategy,innovation}'),
  ('quizzers', 'Quizzers'' Club MANIT',
   'Quizzing, every flavour of it — from general trivia to subject quizzes across the fests.',
   'Literary', '{quizzing,trivia}'),
  ('avantikulam', 'Avantikulam',
   'Free coaching for underprivileged students, taught by students of the institute.',
   'Social', '{teaching,social service}'),
  ('spic-macay', 'SPIC MACAY Heritage Club MANIT',
   'Indian classical music and dance on campus, plus heritage walks around Bhopal.',
   'Cultural', '{Indian classical music,heritage walks}'),
  ('think-india', 'Think India MANIT',
   'Policy, debate and governance — reads the country''s problems and argues about them properly.',
   'Policy', '{policy,debate,governance}'),
  ('ae-se-aenak', 'Ae Se Aenak',
   'The street play society: nukkad natak on the things nobody wants to put on a poster.',
   'Cultural', '{street play,social awareness}'),
  ('roobaroo', 'Roobaroo',
   'The cultural society — drama, theatre and the stage side of the institute''s festivals.',
   'Cultural', '{drama,theatre,festivals}'),
  ('purge', 'Purge',
   'Environmental society: campus sustainability, clean-ups and the waste nobody else counts.',
   'Social', '{environment,sustainability}'),
  ('sae', 'SAE MANIT',
   'Society of Automotive Engineers — builds the BAJA and SUPRA cars and races them.',
   'Technical', '{automotive,BAJA,SUPRA}'),
  ('finit', 'FiNIT',
   'The finance society: markets, valuation and the money side of everything else.',
   'Business', '{finance,markets}'),
  ('tooryanaad', 'Tooryanaad',
   'The flagship Hindi literary and cultural festival, and the society that runs it.',
   'Literary', '{Hindi literature,cultural fest}'),
  ('aaroha', 'Aaroha',
   'Child welfare and education — teaching and supporting children around the campus.',
   'Social', '{child welfare,education}'),
  ('ncc', 'NCC MANIT',
   'The National Cadet Corps unit: drill, camps and national service.',
   'Service', '{discipline,leadership,national service}'),
  ('inspire', 'INSPIRE MANIT',
   'Values and personal development — talks, reading and the quieter kind of growth.',
   'Personal development', '{values,personal development}'),
  ('debsoc', 'DebSoc',
   'The debating society: parliamentary debate, adjudication and the tournaments.',
   'Literary', '{debating,public speaking}'),
  ('drishtant', 'Drishtant',
   'The oldest literary society on campus — writing, poetry and the campus magazine.',
   'Literary', '{literature,writing}'),
  ('chesa', 'ChESA',
   'Chemical Engineering Students'' Association: the department''s own society.',
   'Departmental', '{chemical engineering,departmental}')
on conflict (slug) do update set
  name = excluded.name, blurb = excluded.blurb,
  category = excluded.category, tags = excluded.tags;

-- The fests with a date on them. added_by is the first admin on this machine,
-- because events.added_by is not null and has to be somebody real -- these
-- came off the institute's own calendar, and an admin is who would have typed
-- them in. If there is no admin yet, this insert adds nothing and says so by
-- adding nothing; run it again after the first admin exists.
--
-- `where not exists` on the title rather than `on conflict`: the primary key
-- is a uuid, so there is no natural key to conflict on, and re-running this
-- must not post Vidyut twice to a hundred and ten people's Coming up list.
insert into events (title, society, starts_on, ends_on, venue, blurb, added_by)
select v.title, v.society, v.starts_on, v.ends_on, v.venue, v.blurb, a.id
  from (select id from profiles where role = 'admin' order by created_at limit 1) a
 cross join (values
  ('Vidyut', 'Evolve', date '2026-09-09', null::date, 'MANIT campus',
   'Evolve''s electric mobility event.'),
  ('Tooryanaad 2026', 'Tooryanaad', date '2026-09-12', date '2026-09-14', 'LRC MANIT',
   'The national Hindi literary and cultural festival.'),
  ('Vervana', 'Pravah', date '2026-09-12', null::date, 'MME Auditorium',
   'Team games for first years.'),
  ('Maffick + Techno', 'Students'' Council', date '2027-02-04', date '2027-02-07', 'MANIT campus',
   'The cultural festival, alongside the technical one.'),
  ('E-Summit', 'E-Cell', date '2027-02-12', date '2027-02-14', 'MANIT campus',
   'The entrepreneurship summit.')
 ) as v(title, society, starts_on, ends_on, venue, blurb)
 where not exists (select 1 from events e where e.title = v.title);

-- Maffick, TechnoSearch and NCC Days are annual and real, and none of them has
-- a published date for the coming year. They are deliberately NOT seeded: an
-- event on this tab is a date, and inventing one to fill a row is the exact
-- thing this screen must not do. They go in when the date is announced.

-- Where things are. Every coordinate here was read off a public map rather
-- than surveyed, which is why approx is left at its default of true on all of
-- them: they land you at the building, not at its door. Correcting one is an
-- admin edit on the Campus tab, and flipping approx to false is a claim that
-- somebody stood there.
insert into places (slug, name, kind, lat, lng, note) values
  ('main-gate',     'Main Gate',              'gate',     23.2141, 77.4053, 'Gate No. 1, on Link Road No. 3.'),
  ('lh6',           'Lecture Hall 6 (LH6)',   'academic', 23.2170, 77.4076, 'Section I''s own hall — most of your timetable is here.'),
  ('lhc',           'Lecture Hall Complex',   'academic', 23.2169, 77.4078, 'The block LH6 sits in.'),
  ('lrc',           'Learning Resource Centre (LRC)', 'academic', 23.2166, 77.4085, 'Where Tooryanaad is held.'),
  ('library',       'Central Library',        'academic', 23.2164, 77.4082, ''),
  ('mme-auditorium','MME Auditorium',         'academic', 23.2158, 77.4090, 'The big auditorium — most fest events open here.'),
  ('admin-block',   'Administrative Block',   'admin',    23.2150, 77.4062, 'Fees, documents, the registrar.'),
  ('sports-ground', 'Sports Ground',          'sport',    23.2182, 77.4062, 'Cricket and football ground.'),
  ('gymkhana',      'Gymkhana',               'sport',    23.2178, 77.4069, 'Indoor games and the gym.'),
  ('canteen-main',  'Main Canteen',           'food',     23.2161, 77.4071, ''),
  ('nescafe',       'Nescafe Corner',         'food',     23.2163, 77.4074, 'The late-evening one.'),
  ('health-centre', 'Health Centre',          'health',   23.2154, 77.4068, ''),
  ('hostel-h1',     'Hostel 1',               'hostel',   23.2188, 77.4048, ''),
  ('hostel-h4',     'Hostel 4',               'hostel',   23.2192, 77.4055, ''),
  ('hostel-h7',     'Hostel 7',               'hostel',   23.2196, 77.4063, ''),
  ('hostel-h10',    'Hostel 10',              'hostel',   23.2199, 77.4071, ''),
  ('hostel-girls',  'Girls'' Hostel',         'hostel',   23.2147, 77.4092, ''),
  ('dept-ee',       'Electrical Engineering',  'academic', 23.2172, 77.4088, ''),
  ('dept-me',       'Mechanical Engineering',  'academic', 23.2176, 77.4094, ''),
  ('dept-chem',     'Chemical Engineering',    'academic', 23.2168, 77.4096, ''),
  ('dept-cse',      'Computer Science and Engineering', 'academic', 23.2174, 77.4083, ''),
  ('dept-arch',     'Architecture and Planning', 'academic', 23.2156, 77.4098, '')
on conflict (slug) do update set
  name = excluded.name, kind = excluded.kind, lat = excluded.lat,
  lng = excluded.lng, note = excluded.note;
