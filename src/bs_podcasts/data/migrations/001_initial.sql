CREATE TABLE shows (
    id             INTEGER PRIMARY KEY,
    feed_url       TEXT NOT NULL UNIQUE,
    canonical_url  TEXT NOT NULL DEFAULT '',
    title          TEXT NOT NULL DEFAULT '',
    author         TEXT NOT NULL DEFAULT '',
    description    TEXT NOT NULL DEFAULT '',
    website_url    TEXT NOT NULL DEFAULT '',
    artwork_url    TEXT NOT NULL DEFAULT '',
    artwork_path   TEXT NOT NULL DEFAULT '',
    source         TEXT NOT NULL DEFAULT 'rss',
    health         TEXT NOT NULL DEFAULT 'unknown',
    fail_count     INTEGER NOT NULL DEFAULT 0,
    suspended      INTEGER NOT NULL DEFAULT 0,
    etag           TEXT NOT NULL DEFAULT '',
    last_modified  TEXT NOT NULL DEFAULT '',
    last_refresh   REAL,
    added_at       REAL NOT NULL
);

CREATE TABLE episodes (
    id                INTEGER PRIMARY KEY,
    show_id           INTEGER NOT NULL REFERENCES shows(id) ON DELETE CASCADE,
    external_id       TEXT NOT NULL,
    title             TEXT NOT NULL DEFAULT '',
    description       TEXT NOT NULL DEFAULT '',
    media_url         TEXT NOT NULL DEFAULT '',
    mime_type         TEXT NOT NULL DEFAULT 'audio/*',
    published_at      TEXT NOT NULL DEFAULT '',
    duration_seconds  INTEGER NOT NULL DEFAULT 0,
    position_seconds  REAL NOT NULL DEFAULT 0,
    played            INTEGER NOT NULL DEFAULT 0,
    is_new            INTEGER NOT NULL DEFAULT 1,
    downloaded_path   TEXT NOT NULL DEFAULT '',
    added_at          REAL NOT NULL,
    UNIQUE(show_id, external_id)
);

CREATE INDEX episodes_show_published ON episodes(show_id, published_at DESC);

CREATE TABLE queue (
    id          INTEGER PRIMARY KEY,
    episode_id  INTEGER NOT NULL UNIQUE REFERENCES episodes(id) ON DELETE CASCADE,
    position    INTEGER NOT NULL,
    added_at    REAL NOT NULL
);

CREATE INDEX queue_position ON queue(position);

CREATE TABLE settings (
    key    TEXT PRIMARY KEY,
    value  TEXT NOT NULL
);
