-- Human-readable feed categories for podcast information and browsing context.
ALTER TABLE shows ADD COLUMN categories TEXT NOT NULL DEFAULT '';
