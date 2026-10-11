-- Remember a listening-details fetch that came back empty, so a feed without
-- chapters or a transcript is not fetched again on every open. A changed source
-- URL, or the retry interval passing, lets the next open fetch again.
CREATE TABLE IF NOT EXISTS listening_attempts (
    episode_id INTEGER NOT NULL,
    kind TEXT NOT NULL CHECK (kind IN ('chapters', 'transcript')),
    source_url TEXT NOT NULL,
    attempted_at REAL NOT NULL,
    PRIMARY KEY (episode_id, kind)
);
