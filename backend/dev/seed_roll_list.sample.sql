-- The shape of the institute's section list. The real one is not in this
-- repository and never will be: it is a thousand real students' names and
-- institute ID numbers, and none of them agreed to be on the internet.
--
-- If you are running recarve for your own institute, this is the file to
-- write. One row per seat. `scholar_no` is whatever your registrar uses as
-- the unique per-student number and must match the local part of the
-- institute email address, because that is what the Google door looks up.
-- `roll_no` is the human-facing one people actually say out loud.
--
-- Copy this to seed_roll_list.sql, fill it in, and apply it to the live
-- database by hand. migrate.py does not apply it and the test suite does not
-- need it.

begin;

insert into roll_list (scholar_no, roll_no, name, section_id)
select v.scholar, v.roll, v.name, s.id
  from (values
    ('26416011101', '26A001', 'Asha Nair'),
    ('26416011102', '26A002', 'Rohit Verma'),
    ('26416011103', '26A003', 'Meera Iyer'),
    ('26117011161', '26H020', 'Kabir Das'),
    ('26117011162', '26H021', 'Sana Qureshi')
  ) as v(scholar, roll, name)
  join sections s
    on s.name = substring(v.roll from 3 for 1)
   and s.grad_year = 2030
on conflict (scholar_no) do nothing;

commit;
