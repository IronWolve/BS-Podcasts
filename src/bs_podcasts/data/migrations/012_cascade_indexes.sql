-- Indexes on the child side of the ON DELETE CASCADE foreign keys.
--
-- Without one, SQLite scans the whole child table for every parent row it
-- cascades, so removing a show became O(episodes-in-show x table-size).
-- bookmarks.episode_id had no index at all; folder_shows.show_id and
-- saved_playlist_items.episode_id were only the trailing column of their
-- composite primary keys, which cannot serve a lookup keyed on that column
-- alone.
--
-- transcript_episode is dropped as redundant: UNIQUE(episode_id,
-- segment_index) on the same table already creates an identical index, so
-- the explicit one only cost write time and disk.

CREATE INDEX IF NOT EXISTS bookmarks_episode ON bookmarks(episode_id);
CREATE INDEX IF NOT EXISTS folder_shows_show ON folder_shows(show_id);
CREATE INDEX IF NOT EXISTS saved_playlist_items_episode ON saved_playlist_items(episode_id);

DROP INDEX IF EXISTS transcript_episode;
