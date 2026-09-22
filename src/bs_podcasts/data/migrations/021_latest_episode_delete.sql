-- Refresh all cached metadata together, even when consecutive titles match.
CREATE INDEX IF NOT EXISTS episodes_show_latest ON episodes(show_id, published_at DESC, id DESC);
DROP TRIGGER shows_counters_ad;
CREATE TRIGGER shows_counters_ad AFTER DELETE ON episodes BEGIN
    UPDATE shows SET episode_count=episode_count-1, new_count=new_count-(OLD.is_new=1)
    WHERE id=OLD.show_id;
    UPDATE shows SET (latest_key, latest_episode_title, latest_episode_published_at)=(
        SELECT COALESCE(NULLIF(published_at,''),'0') || printf('~%012d',id), title, published_at
        FROM (SELECT id,title,published_at FROM episodes WHERE show_id=OLD.show_id
              ORDER BY published_at DESC,id DESC LIMIT 1)
        UNION ALL SELECT '', '', '' LIMIT 1
    ) WHERE id=OLD.show_id
      AND latest_key=COALESCE(NULLIF(OLD.published_at,''),'0') || printf('~%012d',OLD.id);
END;
-- Repair already-stale rows from the old trigger.
UPDATE shows SET (latest_key, latest_episode_title, latest_episode_published_at)=(
    SELECT COALESCE(NULLIF(published_at,''),'0') || printf('~%012d',id), title, published_at
    FROM (SELECT id,title,published_at FROM episodes WHERE show_id=shows.id
          ORDER BY published_at DESC,id DESC LIMIT 1)
    UNION ALL SELECT '', '', '' LIMIT 1
);
