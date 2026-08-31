-- Index and collation polish (audit P3 data/schema items).

-- downloads.list() orders by updated_at alone; downloads_state leads on
-- state and cannot serve that sort.
CREATE INDEX downloads_updated ON downloads(updated_at DESC);

-- Favorites are read ORDER BY published_at DESC, id DESC; the partial index
-- lacked the id tiebreaker, so equal-date rows fell back to a sort pass.
DROP INDEX episodes_favorite;
CREATE INDEX episodes_favorite ON episodes(favorite, published_at DESC, id DESC) WHERE favorite=1;

-- Folder/playlist names were only case-sensitively unique ("News" and
-- "news" could coexist and read as duplicates in every picker). A collated
-- unique index adds case-insensitive uniqueness WITHOUT rebuilding the
-- tables — a rebuild would DROP the parent and cascade-delete memberships.
-- Existing case-insensitive duplicates are renamed, never dropped.
UPDATE folders SET name = name || ' (' || id || ')'
WHERE EXISTS (
    SELECT 1 FROM folders f2
    WHERE f2.id < folders.id AND f2.name = folders.name COLLATE NOCASE
);
CREATE UNIQUE INDEX folders_name_nocase ON folders(name COLLATE NOCASE);

UPDATE saved_playlists SET name = name || ' (' || id || ')'
WHERE EXISTS (
    SELECT 1 FROM saved_playlists p2
    WHERE p2.id < saved_playlists.id AND p2.name = saved_playlists.name COLLATE NOCASE
);
CREATE UNIQUE INDEX saved_playlists_name_nocase ON saved_playlists(name COLLATE NOCASE);
