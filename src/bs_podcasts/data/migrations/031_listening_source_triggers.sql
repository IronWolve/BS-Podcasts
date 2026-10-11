-- A trigger without a WHEN clause runs for every UPDATE that names one of its
-- columns, even when the value stays the same, so the feed refresh (which
-- assigns all three source columns on every changed episode) ran both
-- deletes for a title-only change. Each source gets its own trigger with a
-- WHEN; the content is deleted only when its source really changed.
DROP TRIGGER listening_sources_changed;
CREATE TRIGGER listening_chapters_changed AFTER UPDATE OF chapters_url ON episodes
WHEN OLD.chapters_url!=NEW.chapters_url BEGIN
    DELETE FROM chapters WHERE episode_id=NEW.id;
END;
CREATE TRIGGER listening_transcript_changed AFTER UPDATE OF transcript_url, transcript_type ON episodes
WHEN OLD.transcript_url!=NEW.transcript_url OR OLD.transcript_type!=NEW.transcript_type BEGIN
    DELETE FROM transcript_segments WHERE episode_id=NEW.id;
END;
