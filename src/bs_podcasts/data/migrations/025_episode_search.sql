-- Derived candidate index only. The repository still applies its literal
-- substring predicate so result ordering and search semantics do not change.
CREATE VIRTUAL TABLE episode_search USING fts5(title,description,show_title,
    tokenize='trigram',content='',contentless_delete=1,detail=none,columnsize=1);
INSERT INTO episode_search(rowid,title,description,show_title)
SELECT e.id,e.title,e.description,s.title FROM episodes e JOIN shows s ON s.id=e.show_id;

CREATE TRIGGER episode_search_ai AFTER INSERT ON episodes BEGIN
    INSERT INTO episode_search(rowid,title,description,show_title)
    SELECT NEW.id,NEW.title,NEW.description,s.title FROM shows s WHERE s.id=NEW.show_id;
END;
CREATE TRIGGER episode_search_ad AFTER DELETE ON episodes BEGIN
    DELETE FROM episode_search WHERE rowid=OLD.id;
END;
CREATE TRIGGER episode_search_au AFTER UPDATE OF title,description,show_id ON episodes
WHEN NEW.title!=OLD.title OR NEW.description!=OLD.description OR NEW.show_id!=OLD.show_id BEGIN
    DELETE FROM episode_search WHERE rowid=OLD.id;
    INSERT INTO episode_search(rowid,title,description,show_title)
    SELECT NEW.id,NEW.title,NEW.description,s.title FROM shows s WHERE s.id=NEW.show_id;
END;
CREATE TRIGGER episode_search_show_title AFTER UPDATE OF title ON shows
WHEN NEW.title!=OLD.title BEGIN
    DELETE FROM episode_search WHERE rowid IN (SELECT id FROM episodes WHERE show_id=NEW.id);
    INSERT INTO episode_search(rowid,title,description,show_title)
    SELECT id,title,description,NEW.title FROM episodes WHERE show_id=NEW.id;
END;
