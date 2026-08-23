ALTER TABLE shows ADD COLUMN trim_level TEXT NOT NULL DEFAULT 'off';
ALTER TABLE episodes ADD COLUMN transcript_url TEXT NOT NULL DEFAULT '';
ALTER TABLE episodes ADD COLUMN transcript_type TEXT NOT NULL DEFAULT '';

CREATE TABLE chapters (
    id             INTEGER PRIMARY KEY,
    episode_id     INTEGER NOT NULL REFERENCES episodes(id) ON DELETE CASCADE,
    chapter_index  INTEGER NOT NULL,
    start_seconds  REAL NOT NULL,
    end_seconds    REAL,
    title          TEXT NOT NULL DEFAULT '',
    artwork_url    TEXT NOT NULL DEFAULT '',
    UNIQUE(episode_id, chapter_index)
);

CREATE TABLE transcript_segments (
    id             INTEGER PRIMARY KEY,
    episode_id     INTEGER NOT NULL REFERENCES episodes(id) ON DELETE CASCADE,
    segment_index  INTEGER NOT NULL,
    start_seconds  REAL,
    end_seconds    REAL,
    text           TEXT NOT NULL,
    UNIQUE(episode_id, segment_index)
);

CREATE INDEX transcript_episode ON transcript_segments(episode_id, segment_index);

CREATE TABLE bookmarks (
    id                INTEGER PRIMARY KEY,
    episode_id        INTEGER NOT NULL REFERENCES episodes(id) ON DELETE CASCADE,
    position_seconds  REAL NOT NULL,
    title             TEXT NOT NULL DEFAULT '',
    created_at        REAL NOT NULL
);

CREATE TABLE playback_metrics (
    singleton_id       INTEGER PRIMARY KEY CHECK(singleton_id = 1),
    silence_saved      REAL NOT NULL DEFAULT 0
);

INSERT INTO playback_metrics(singleton_id, silence_saved) VALUES (1, 0);

CREATE TABLE folders (
    id          INTEGER PRIMARY KEY,
    name        TEXT NOT NULL UNIQUE,
    created_at  REAL NOT NULL
);

CREATE TABLE folder_shows (
    folder_id  INTEGER NOT NULL REFERENCES folders(id) ON DELETE CASCADE,
    show_id    INTEGER NOT NULL REFERENCES shows(id) ON DELETE CASCADE,
    PRIMARY KEY(folder_id, show_id)
);

CREATE TABLE saved_playlists (
    id          INTEGER PRIMARY KEY,
    name        TEXT NOT NULL UNIQUE,
    created_at  REAL NOT NULL
);

CREATE TABLE saved_playlist_items (
    playlist_id  INTEGER NOT NULL REFERENCES saved_playlists(id) ON DELETE CASCADE,
    episode_id   INTEGER NOT NULL REFERENCES episodes(id) ON DELETE CASCADE,
    position     INTEGER NOT NULL,
    PRIMARY KEY(playlist_id, episode_id)
);

CREATE INDEX saved_playlist_order ON saved_playlist_items(playlist_id, position);
