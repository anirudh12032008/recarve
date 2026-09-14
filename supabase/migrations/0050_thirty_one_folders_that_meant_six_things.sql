-- The seniors' portal files 581 documents into 31 folder names. Those 31 names
-- mean about six things:
--
--   "End Term Previous Year Questions"  "Previous Year End Term Papers"
--   "Previous Year End Term  Papers"    (two spaces -- a different folder)
--   "2024-2025 PYQs"                    "2024-25 PYQs"
--   "Notes"  "Class Notes"  "Notes and Slides"  "Detailed Slides"
--   "Books"  "Book"  "Books and PPTs"
--
-- Nobody decided that. It is what two years of "make a folder for it" looks
-- like, and it is why finding every End Term for Mathematics 1 on that site
-- means opening four folders and knowing which four. A folder name is a
-- filing decision frozen into a string; the same decision as columns is a
-- query.
--
-- ---------------------------------------------------------------------------
-- Why this is not four columns on `materials`, which was the first plan
--
-- Because 0042 scopes `materials` to one section:
--
--     using (is_approved() and section_id = my_section())
--
-- and it is right to. A recording of one section's Tuesday is not a thing the
-- other section may read, because the professors differ. But a 2023 End Term
-- paper is not one section's Tuesday. Every section sat it.
--
-- Put the archive in `materials` and nine sections out of ten cannot see it,
-- which is the entire feature lost to a clause that is correct where it
-- stands. Work around that clause -- "institute-wide if kind is not null" --
-- and any student who can set a column can post a row that leaves the section
-- boundary, which trades a visibility bug for a privilege escalation.
--
-- 0041 already wrote the rule this table follows:
--
--     Institute-wide, deliberately NOT in this list, because MANIT has one of
--     each and a second copy per section would be a worse copy:
--     clubs, events, places, academic_calendar, subjects, subject_sets,
--     sections
--
-- A past paper is one of each. So it belongs beside those, as its own
-- institute-wide table with `places`'s policy shape: every approved member
-- reads, only an admin writes. No section_id, and that absence is the design,
-- not an omission for a later migration to correct.

create table archive_documents (
  id           uuid primary key default gen_random_uuid(),
  subject_code text not null references subjects(code),

  -- What the document IS. Six values, because that is how many distinct
  -- things the 31 folders turned out to hold, not because six is tidy.
  kind         text not null check (kind in
                 ('paper','notes','slides','assignment','lab','syllabus','book')),

  -- Only a paper has an exam. Notes are not mid-term notes. Stated as one
  -- constraint rather than two so the impossible row -- slides marked 'end' --
  -- cannot be written at all, instead of being written and then filtered out
  -- by whichever query remembers to check kind first.
  exam         text,
  constraint archive_exam_only_on_papers check (
    exam is null or (kind = 'paper' and exam in ('mid','end','mini'))
  ),

  -- The academic year the document belongs to, stored as the year it opens
  -- in: 2025 means 2025-26. One int, because "2025-26" as text sorts fine and
  -- filters terribly, and every range question a student actually asks
  -- ("anything since 2023") is arithmetic on the opening year.
  --
  -- The bounds are not a guess at the institute's founding. Low enough to
  -- accept everything the archive holds (oldest found: March 2022), tight
  -- enough that a mistyped 1025 is refused at the door rather than sorting to
  -- the top of a list forever.
  year         int check (year is null or year between 2000 and 2100),

  -- Which section SAT this paper -- not which section may read it. Those are
  -- different questions and the second one has no answer here on purpose:
  -- every section reads every row in this table.
  --
  -- It earns its place on the mini tests, which are set per section. Maths 1
  -- alone has nine for 2025-26. Without this column they are nine
  -- indistinguishable rows called "Mini Test 2025-26".
  --
  -- One upper-case letter, and 0047 is why the range stops at J: the
  -- institute has ten sections and section I is one of them.
  set_for_section text check (set_for_section is null or set_for_section ~ '^[A-J]$'),

  title        text not null,

  -- Where the bytes are. A path under the archive root for anything with
  -- bytes; for kind = 'book' it is a URL, because the seniors host no
  -- textbooks -- all 30 of their "Books" entries are .url bookmarks pointing
  -- elsewhere, and a link is a document whose content happens to be an
  -- address. Null size_bytes goes with those.
  file_key     text not null,
  size_bytes   bigint,

  -- Provenance, in the document's own row rather than in a commit message
  -- nobody reading the archive will ever see. The first 581 rows all say the
  -- same thing and that is the point: when a student asks where a paper came
  -- from, the answer is in the table.
  source       text,

  created_at   timestamptz not null default now()
);

-- One copy of a document per subject. The 31 folders overlap -- the same End
-- Term sits in "2024-2025 PYQs" and in "End Term Previous Year Questions" --
-- so an importer run twice, or run once over folders that duplicate each
-- other, would otherwise shelve the same paper repeatedly. The constraint is
-- what makes the import re-runnable instead of merely careful.
create unique index archive_documents_one_copy
  on archive_documents (subject_code, file_key);

-- The query the table exists for: this subject, this kind, newest first.
create index archive_documents_shelf
  on archive_documents (subject_code, kind, year desc nulls last, exam);

-- ---------------------------------------------------------------------------
-- Policies: `places`'s shape exactly. Every approved member reads the whole
-- archive; only an admin changes it. There is no section clause and the file
-- above says why.

alter table archive_documents enable row level security;

create policy "members read the archive" on archive_documents for select
  using (is_approved());

create policy "admins add to the archive" on archive_documents for insert
  with check (is_admin());

create policy "admins edit the archive" on archive_documents for update
  using (is_admin()) with check (is_admin());

-- A past paper is nobody's words. Removing a row somebody imported with the
-- wrong year destroys nothing anyone wrote, so unlike a doubt or a confession
-- this one is a real delete rather than a status change.
create policy "admins remove from the archive" on archive_documents for delete
  using (is_admin());

grant select, insert, update, delete on archive_documents to authenticated;
