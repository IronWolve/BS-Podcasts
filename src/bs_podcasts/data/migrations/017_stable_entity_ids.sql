-- Retired show/episode IDs must never identify new data while old work drains.
-- Keep the existing tables/FKs: rebuilding them would risk cascading deletes.
CREATE TABLE entity_sequences (kind TEXT PRIMARY KEY, value INTEGER NOT NULL);
INSERT INTO entity_sequences SELECT 'show', COALESCE(MAX(id), 0) FROM shows;
INSERT INTO entity_sequences SELECT 'episode', COALESCE(MAX(id), 0) FROM episodes;
CREATE TRIGGER shows_sequence_ai AFTER INSERT ON shows BEGIN
    UPDATE entity_sequences SET value=MAX(value, NEW.id) WHERE kind='show';
END;
CREATE TRIGGER episodes_sequence_ai AFTER INSERT ON episodes BEGIN
    UPDATE entity_sequences SET value=MAX(value, NEW.id) WHERE kind='episode';
END;
