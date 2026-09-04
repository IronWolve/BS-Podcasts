-- Cached per-show counters (audit F-051, F-056).
--
-- list_shows() aggregated every episode row on every library reload:
-- 85 ms and 280 MB of page reads on a 175k-episode library, per reload.
-- The counts and the "latest episode" now live on the shows row and are
-- kept current by triggers, so a reload is one indexed scan of shows.
-- latest_key is the same sort key the aggregate used: published_at (empty
-- sorts below every real date) followed by the zero-padded id tiebreaker.

ALTER TABLE shows ADD COLUMN episode_count INTEGER NOT NULL DEFAULT 0;
ALTER TABLE shows ADD COLUMN new_count INTEGER NOT NULL DEFAULT 0;
ALTER TABLE shows ADD COLUMN latest_episode_title TEXT NOT NULL DEFAULT '';
ALTER TABLE shows ADD COLUMN latest_episode_published_at TEXT NOT NULL DEFAULT '';
ALTER TABLE shows ADD COLUMN latest_key TEXT NOT NULL DEFAULT '';

-- Backfill in one pass over episodes (a correlated subquery per show
-- re-scanned the table 299 times on a 175k-episode library: 4.5 s).
CREATE TEMP TABLE show_agg AS
    SELECT show_id,
           COUNT(*) AS episode_count,
           SUM(is_new = 1) AS new_count,
           MAX(COALESCE(NULLIF(published_at, ''), '0') || printf('~%012d', id)) AS latest_key
    FROM episodes GROUP BY show_id;
CREATE TEMP TABLE show_latest AS
    SELECT e.show_id, e.title, e.published_at
    FROM episodes e JOIN show_agg a ON a.show_id = e.show_id
    WHERE COALESCE(NULLIF(e.published_at, ''), '0') || printf('~%012d', e.id) = a.latest_key;
UPDATE shows SET
    episode_count = COALESCE((SELECT episode_count FROM show_agg WHERE show_agg.show_id = shows.id), 0),
    new_count = COALESCE((SELECT new_count FROM show_agg WHERE show_agg.show_id = shows.id), 0),
    latest_key = COALESCE((SELECT latest_key FROM show_agg WHERE show_agg.show_id = shows.id), ''),
    latest_episode_title = COALESCE((SELECT title FROM show_latest WHERE show_latest.show_id = shows.id), ''),
    latest_episode_published_at = COALESCE((SELECT published_at FROM show_latest WHERE show_latest.show_id = shows.id), '');
DROP TABLE show_latest;
DROP TABLE show_agg;

CREATE TRIGGER shows_counters_ai AFTER INSERT ON episodes BEGIN
    UPDATE shows SET
        episode_count = episode_count + 1,
        new_count = new_count + (NEW.is_new = 1)
    WHERE id = NEW.show_id;
    UPDATE shows SET
        latest_key = COALESCE(NULLIF(NEW.published_at, ''), '0') || printf('~%012d', NEW.id),
        latest_episode_title = NEW.title,
        latest_episode_published_at = NEW.published_at
    WHERE id = NEW.show_id
      AND (COALESCE(NULLIF(NEW.published_at, ''), '0') || printf('~%012d', NEW.id)) > latest_key;
END;

CREATE TRIGGER shows_counters_ad AFTER DELETE ON episodes BEGIN
    UPDATE shows SET
        episode_count = episode_count - 1,
        new_count = new_count - (OLD.is_new = 1)
    WHERE id = OLD.show_id;
    UPDATE shows SET
        latest_key = COALESCE((
            SELECT MAX(COALESCE(NULLIF(e.published_at, ''), '0') || printf('~%012d', e.id))
            FROM episodes e WHERE e.show_id = OLD.show_id), '')
    WHERE id = OLD.show_id
      AND latest_key = COALESCE(NULLIF(OLD.published_at, ''), '0') || printf('~%012d', OLD.id);
    UPDATE shows SET
        latest_episode_title = COALESCE((
            SELECT e.title FROM episodes e WHERE e.show_id = OLD.show_id
            AND COALESCE(NULLIF(e.published_at, ''), '0') || printf('~%012d', e.id) = shows.latest_key), ''),
        latest_episode_published_at = COALESCE((
            SELECT e.published_at FROM episodes e WHERE e.show_id = OLD.show_id
            AND COALESCE(NULLIF(e.published_at, ''), '0') || printf('~%012d', e.id) = shows.latest_key), '')
    WHERE id = OLD.show_id
      AND latest_episode_title != COALESCE((
            SELECT e.title FROM episodes e WHERE e.show_id = OLD.show_id
            AND COALESCE(NULLIF(e.published_at, ''), '0') || printf('~%012d', e.id) = shows.latest_key), '');
END;

CREATE TRIGGER shows_counters_au_new AFTER UPDATE OF is_new ON episodes
WHEN NEW.is_new != OLD.is_new BEGIN
    UPDATE shows SET new_count = new_count + (NEW.is_new = 1) - (OLD.is_new = 1)
    WHERE id = NEW.show_id;
END;

CREATE TRIGGER shows_counters_au_latest AFTER UPDATE OF title, published_at, show_id ON episodes
WHEN NEW.title != OLD.title OR NEW.published_at != OLD.published_at OR NEW.show_id != OLD.show_id BEGIN
    UPDATE shows SET
        latest_key = COALESCE((
            SELECT MAX(COALESCE(NULLIF(e.published_at, ''), '0') || printf('~%012d', e.id))
            FROM episodes e WHERE e.show_id = shows.id), '')
    WHERE id IN (NEW.show_id, OLD.show_id);
    UPDATE shows SET
        latest_episode_title = COALESCE((
            SELECT e.title FROM episodes e WHERE e.show_id = shows.id
            AND COALESCE(NULLIF(e.published_at, ''), '0') || printf('~%012d', e.id) = shows.latest_key), ''),
        latest_episode_published_at = COALESCE((
            SELECT e.published_at FROM episodes e WHERE e.show_id = shows.id
            AND COALESCE(NULLIF(e.published_at, ''), '0') || printf('~%012d', e.id) = shows.latest_key), '')
    WHERE id IN (NEW.show_id, OLD.show_id);
END;
