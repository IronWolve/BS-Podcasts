ALTER TABLE shows ADD COLUMN playback_speed REAL NOT NULL DEFAULT 1.0;
ALTER TABLE shows ADD COLUMN skip_back INTEGER NOT NULL DEFAULT 15;
ALTER TABLE shows ADD COLUMN skip_forward INTEGER NOT NULL DEFAULT 30;
ALTER TABLE shows ADD COLUMN auto_continue INTEGER NOT NULL DEFAULT 1;

ALTER TABLE episodes ADD COLUMN last_played REAL;

CREATE TABLE playback_state (
    singleton_id  INTEGER PRIMARY KEY CHECK(singleton_id = 1),
    episode_id    INTEGER REFERENCES episodes(id) ON DELETE SET NULL,
    state         TEXT NOT NULL DEFAULT 'idle',
    updated_at    REAL NOT NULL
);

INSERT INTO playback_state(singleton_id, state, updated_at)
VALUES (1, 'idle', 0);
