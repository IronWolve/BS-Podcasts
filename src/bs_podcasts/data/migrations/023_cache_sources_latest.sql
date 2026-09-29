-- Source changes invalidate listening content in the same feed transaction.
CREATE TRIGGER listening_sources_changed AFTER UPDATE OF chapters_url, transcript_url, transcript_type ON episodes BEGIN
    DELETE FROM chapters WHERE episode_id=NEW.id AND OLD.chapters_url!=NEW.chapters_url;
    DELETE FROM transcript_segments WHERE episode_id=NEW.id
        AND (OLD.transcript_url!=NEW.transcript_url OR OLD.transcript_type!=NEW.transcript_type);
END;

DROP TRIGGER shows_counters_au_latest;
CREATE TRIGGER shows_counters_au_latest AFTER UPDATE OF title, published_at, show_id ON episodes
WHEN NEW.title!=OLD.title OR NEW.published_at!=OLD.published_at OR NEW.show_id!=OLD.show_id BEGIN
    UPDATE shows SET (latest_key, latest_episode_title, latest_episode_published_at)=(
        SELECT COALESCE(NULLIF(published_at,''),'0') || printf('~%012d',id), title, published_at
        FROM (SELECT id,title,published_at FROM episodes WHERE show_id=shows.id
              ORDER BY published_at DESC,id DESC LIMIT 1)
        UNION ALL SELECT '', '', '' LIMIT 1
    ) WHERE id IN (NEW.show_id, OLD.show_id);
END;
