-- Moving an episode to another show keeps both shows' cached counters right.
-- The is_new trigger now covers only changes inside one show; a move adjusts
-- episode_count and new_count of the old and the new show in one trigger.
DROP TRIGGER shows_counters_au_new;
CREATE TRIGGER shows_counters_au_new AFTER UPDATE OF is_new ON episodes
WHEN NEW.is_new != OLD.is_new AND NEW.show_id = OLD.show_id BEGIN
    UPDATE shows SET new_count = new_count + (NEW.is_new = 1) - (OLD.is_new = 1)
    WHERE id = NEW.show_id;
END;

CREATE TRIGGER shows_counters_au_show AFTER UPDATE OF show_id ON episodes
WHEN NEW.show_id != OLD.show_id BEGIN
    UPDATE shows SET episode_count = episode_count - 1, new_count = new_count - (OLD.is_new = 1)
    WHERE id = OLD.show_id;
    UPDATE shows SET episode_count = episode_count + 1, new_count = new_count + (NEW.is_new = 1)
    WHERE id = NEW.show_id;
END;
