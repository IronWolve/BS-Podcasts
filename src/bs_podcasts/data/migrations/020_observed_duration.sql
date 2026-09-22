-- Playback duration is independent of missing/stale feed metadata (A30).
ALTER TABLE episodes ADD COLUMN observed_duration_seconds REAL NOT NULL DEFAULT 0;
