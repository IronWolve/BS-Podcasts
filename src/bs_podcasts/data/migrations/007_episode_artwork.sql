-- Per-episode artwork file (itunes:image on items), distinct from the show cover.
ALTER TABLE episodes ADD COLUMN episode_artwork_path TEXT NOT NULL DEFAULT '';
