-- Keep sparse episode covers off the whole-episode-table prune scan.
CREATE INDEX episodes_episode_artwork ON episodes(episode_artwork_path)
    WHERE episode_artwork_path != '';

-- Attempt rows are retry cache bookkeeping. Retain historical rows, but stop
-- new orphans when an episode is explicitly removed or its show cascades.
CREATE TRIGGER listening_attempts_episode_delete AFTER DELETE ON episodes BEGIN
    DELETE FROM listening_attempts WHERE episode_id=old.id;
END;
