-- Capture write order before dispatch, and reject late position writes (A03).
ALTER TABLE episodes ADD COLUMN position_updated_at REAL NOT NULL DEFAULT 0;
