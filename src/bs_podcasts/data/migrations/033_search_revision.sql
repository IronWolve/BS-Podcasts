-- Derived search cache invalidation; listening/download changes do not affect matches.
CREATE TABLE episode_search_revision (
    singleton_id INTEGER PRIMARY KEY CHECK(singleton_id=1),
    epoch TEXT NOT NULL,
    revision INTEGER NOT NULL DEFAULT 0
);
INSERT INTO episode_search_revision(singleton_id,epoch) VALUES(1,lower(hex(randomblob(16))));
CREATE TRIGGER episode_search_revision_insert AFTER INSERT ON episodes BEGIN
    UPDATE episode_search_revision SET revision=revision+1 WHERE singleton_id=1;
END;
CREATE TRIGGER episode_search_revision_delete AFTER DELETE ON episodes BEGIN
    UPDATE episode_search_revision SET revision=revision+1 WHERE singleton_id=1;
END;
CREATE TRIGGER episode_search_revision_update AFTER UPDATE OF title,description,show_id,published_at ON episodes
WHEN NEW.title IS NOT OLD.title OR NEW.description IS NOT OLD.description
    OR NEW.show_id IS NOT OLD.show_id OR NEW.published_at IS NOT OLD.published_at BEGIN
    UPDATE episode_search_revision SET revision=revision+1 WHERE singleton_id=1;
END;
CREATE TRIGGER episode_search_revision_show AFTER UPDATE OF title ON shows
WHEN NEW.title IS NOT OLD.title BEGIN
    UPDATE episode_search_revision SET revision=revision+1 WHERE singleton_id=1;
END;
