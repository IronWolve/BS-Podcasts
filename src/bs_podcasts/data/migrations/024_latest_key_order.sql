-- A separator below '.' keeps whole-second dates before fractional dates,
-- matching the indexed published_at,id ordering used on recompute.
DROP TRIGGER shows_counters_ai;
CREATE TRIGGER shows_counters_ai AFTER INSERT ON episodes BEGIN
    UPDATE shows SET episode_count=episode_count+1, new_count=new_count+(NEW.is_new=1)
    WHERE id=NEW.show_id;
    UPDATE shows SET
        latest_key=COALESCE(NULLIF(NEW.published_at,''),'0') || printf('!%012d',NEW.id),
        latest_episode_title=NEW.title, latest_episode_published_at=NEW.published_at
    WHERE id=NEW.show_id AND
        (COALESCE(NULLIF(NEW.published_at,''),'0') || printf('!%012d',NEW.id)) > latest_key;
END;

DROP TRIGGER shows_counters_ad;
CREATE TRIGGER shows_counters_ad AFTER DELETE ON episodes BEGIN
    UPDATE shows SET episode_count=episode_count-1, new_count=new_count-(OLD.is_new=1)
    WHERE id=OLD.show_id;
    UPDATE shows SET (latest_key,latest_episode_title,latest_episode_published_at)=(
        SELECT COALESCE(NULLIF(published_at,''),'0') || printf('!%012d',id),title,published_at
        FROM (SELECT id,title,published_at FROM episodes WHERE show_id=OLD.show_id
              ORDER BY published_at DESC,id DESC LIMIT 1)
        UNION ALL SELECT '','','' LIMIT 1
    ) WHERE id=OLD.show_id AND
        latest_key=COALESCE(NULLIF(OLD.published_at,''),'0') || printf('!%012d',OLD.id);
END;

DROP TRIGGER shows_counters_au_latest;
CREATE TRIGGER shows_counters_au_latest AFTER UPDATE OF title,published_at,show_id ON episodes
WHEN NEW.title!=OLD.title OR NEW.published_at!=OLD.published_at OR NEW.show_id!=OLD.show_id BEGIN
    UPDATE shows SET (latest_key,latest_episode_title,latest_episode_published_at)=(
        SELECT COALESCE(NULLIF(published_at,''),'0') || printf('!%012d',id),title,published_at
        FROM (SELECT id,title,published_at FROM episodes WHERE show_id=shows.id
              ORDER BY published_at DESC,id DESC LIMIT 1)
        UNION ALL SELECT '','','' LIMIT 1
    ) WHERE id IN (NEW.show_id,OLD.show_id);
END;

UPDATE shows SET (latest_key,latest_episode_title,latest_episode_published_at)=(
    SELECT COALESCE(NULLIF(published_at,''),'0') || printf('!%012d',id),title,published_at
    FROM (SELECT id,title,published_at FROM episodes WHERE show_id=shows.id
          ORDER BY published_at DESC,id DESC LIMIT 1)
    UNION ALL SELECT '','','' LIMIT 1
);
