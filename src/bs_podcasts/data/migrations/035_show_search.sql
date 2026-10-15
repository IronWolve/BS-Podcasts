-- Keep the podcast title indexed once per show. The literal episode-search
-- predicate still decides whether a result matches; this table only narrows
-- candidate episodes without rewriting each episode's full description when
-- a podcast title changes.
CREATE VIRTUAL TABLE show_search USING fts5(title,
    tokenize='trigram',content='',contentless_delete=1,detail=none,columnsize=1);
INSERT INTO show_search(rowid,title) SELECT id,title FROM shows;

DROP TRIGGER IF EXISTS episode_search_show_title;

CREATE TRIGGER show_search_ai AFTER INSERT ON shows BEGIN
    INSERT INTO show_search(rowid,title) VALUES(NEW.id,NEW.title);
END;
CREATE TRIGGER show_search_ad AFTER DELETE ON shows BEGIN
    DELETE FROM show_search WHERE rowid=OLD.id;
END;
CREATE TRIGGER show_search_au AFTER UPDATE OF title ON shows
WHEN NEW.title!=OLD.title BEGIN
    DELETE FROM show_search WHERE rowid=OLD.id;
    INSERT INTO show_search(rowid,title) VALUES(NEW.id,NEW.title);
END;
