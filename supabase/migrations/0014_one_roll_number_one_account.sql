-- A roll number is one account, whatever the shift key was doing.
--
-- The table said roll numbers were unique; login said they were unique
-- ignoring case. Two different sentences, and the gap between them was an
-- account takeover: `roll_no text unique` let i60 register beside I60, and
-- db_login's `where upper(roll_no) = upper(%s)` then picked one of the two
-- rows by physical order. A classmate who typed the admin's roll in lower
-- case owned a password that answered for the admin's roll -- and the admin,
-- with the right password, was refused.
--
-- The two notions of identity are made one here rather than in the query,
-- because it is the registration that has to be refused. Fixing only the
-- lookup leaves the second row sitting in the table, still colliding, still
-- deciding by tuple order which of them the next reader sees.
--
-- The existing 409 at /join needs no change: it fires on UniqueViolation, and
-- this raises the same one for i60 that the old constraint raised for I60.
alter table profiles drop constraint profiles_roll_no_key;
create unique index profiles_roll_no_key on profiles (upper(roll_no));
