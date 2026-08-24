-- Episode-level links and common RSS/iTunes metadata used by Episode info.
ALTER TABLE episodes ADD COLUMN website_url TEXT NOT NULL DEFAULT '';
ALTER TABLE episodes ADD COLUMN author TEXT NOT NULL DEFAULT '';
ALTER TABLE episodes ADD COLUMN season_number INTEGER;
ALTER TABLE episodes ADD COLUMN episode_number INTEGER;
ALTER TABLE episodes ADD COLUMN episode_type TEXT NOT NULL DEFAULT '';
ALTER TABLE episodes ADD COLUMN explicit INTEGER;
ALTER TABLE episodes ADD COLUMN enclosure_bytes INTEGER NOT NULL DEFAULT 0;
