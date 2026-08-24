-- Favorites, per-podcast download/retention overrides, and local listening stats.
ALTER TABLE episodes ADD COLUMN favorite INTEGER NOT NULL DEFAULT 0;

ALTER TABLE shows ADD COLUMN auto_download_override INTEGER;
ALTER TABLE shows ADD COLUMN auto_download_limit INTEGER;
ALTER TABLE shows ADD COLUMN retention_keep INTEGER;
ALTER TABLE shows ADD COLUMN retention_days INTEGER;

ALTER TABLE playback_metrics ADD COLUMN listened_seconds REAL NOT NULL DEFAULT 0;
ALTER TABLE playback_metrics ADD COLUMN completed_episodes INTEGER NOT NULL DEFAULT 0;

CREATE TABLE listening_stats (
    show_id             INTEGER PRIMARY KEY REFERENCES shows(id) ON DELETE CASCADE,
    listened_seconds    REAL NOT NULL DEFAULT 0,
    completed_episodes  INTEGER NOT NULL DEFAULT 0
);

CREATE INDEX episodes_favorite ON episodes(favorite, published_at DESC) WHERE favorite=1;
