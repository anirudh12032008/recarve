-- A past-paper question that was printed with a figure (a circuit, a truss,
-- a beam, a drawing) carries a crop of that figure, cut from the scanned
-- page and kept beside the archive, so the bank and the lesson can show the
-- question whole without opening the PDF. A path under the library, like
-- archive_documents.file_key; null when the question has no figure.
alter table pyq_questions add column figure text;
