-- instaward-bot migration: repost identity columns.
--
-- HOW TO APPLY IN THE TURSO DASHBOARD: the dashboard SQL editor runs ONE
-- statement at a time, so paste and run EACH ALTER separately (do NOT paste
-- this whole file at once). A "duplicate column name" error simply means the
-- column is already there — safe to ignore, move on to the next one.
--
-- NOTE: the app also applies these automatically on every boot (init_db) and
-- logs "schema verify ok" / "schema verify FAILED". This file is only needed
-- if you want to migrate by hand.

ALTER TABLE processed_media ADD COLUMN repost_code TEXT;

ALTER TABLE processed_media ADD COLUMN repost_pk TEXT;
