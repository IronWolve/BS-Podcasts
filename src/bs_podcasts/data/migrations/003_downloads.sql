CREATE TABLE downloads (
    id             INTEGER PRIMARY KEY,
    episode_id     INTEGER NOT NULL UNIQUE REFERENCES episodes(id) ON DELETE CASCADE,
    source_url     TEXT NOT NULL,
    target_path    TEXT NOT NULL,
    partial_path   TEXT NOT NULL,
    state          TEXT NOT NULL DEFAULT 'queued',
    bytes_done     INTEGER NOT NULL DEFAULT 0,
    bytes_total    INTEGER NOT NULL DEFAULT 0,
    error_message  TEXT NOT NULL DEFAULT '',
    created_at     REAL NOT NULL,
    updated_at     REAL NOT NULL
);

CREATE INDEX downloads_state ON downloads(state, updated_at DESC);
