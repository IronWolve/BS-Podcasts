-- Newest-first listing and history ordering must be index walks, not sorts.
CREATE INDEX IF NOT EXISTS episodes_published ON episodes(published_at DESC, id DESC);
CREATE INDEX IF NOT EXISTS episodes_last_played ON episodes(last_played DESC) WHERE last_played IS NOT NULL;
CREATE INDEX IF NOT EXISTS episodes_show_new ON episodes(show_id, is_new) WHERE is_new = 1;
