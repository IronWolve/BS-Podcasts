-- Repeated episode-page progress counts include played rows and need a
-- narrow positive-progress scan for both whole-library and per-show scopes.
CREATE INDEX episodes_saved_progress ON episodes(show_id)
    WHERE position_seconds>0;

-- New/downloaded global pages share date ordering but have sparse membership;
-- the partial indexes seek those rows directly without scanning every episode.
CREATE INDEX episodes_new_global ON episodes(published_at DESC, id DESC)
    WHERE is_new=1;
CREATE INDEX episodes_downloaded_recent ON episodes(published_at DESC, id DESC)
    WHERE downloaded_path!='';

-- Played/unplayed pages and counts need both the state filter and date order.
CREATE INDEX episodes_played_recent ON episodes(played, published_at DESC, id DESC);

-- Bounded completed-download reconciliation walks by episode ID; preserve
-- that order under the state filter instead of sorting all completed rows.
CREATE INDEX downloads_state_episode ON downloads(state, episode_id);
