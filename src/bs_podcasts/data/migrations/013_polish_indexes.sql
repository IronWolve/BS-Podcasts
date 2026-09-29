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
CREATE TEMP TABLE folder_renames AS
WITH RECURSIVE candidates(id, name) AS (
    SELECT f.id, f.name || ' (' || f.id || ')' FROM folders f
    WHERE EXISTS (SELECT 1 FROM folders other WHERE other.id < f.id AND other.name = f.name COLLATE NOCASE)
    UNION ALL
    SELECT c.id, c.name || ' (' || c.id || ')' FROM candidates c
    WHERE EXISTS (SELECT 1 FROM folders f WHERE f.name = c.name COLLATE NOCASE)
)
SELECT id, name FROM candidates c
WHERE NOT EXISTS (SELECT 1 FROM folders f WHERE f.name = c.name COLLATE NOCASE);
UPDATE folders SET name = (SELECT name FROM folder_renames r WHERE r.id = folders.id)
WHERE id IN (SELECT id FROM folder_renames);
DROP TABLE folder_renames;
CREATE UNIQUE INDEX folders_name_nocase ON folders(name COLLATE NOCASE);

CREATE TEMP TABLE playlist_renames AS
WITH RECURSIVE candidates(id, name) AS (
    SELECT p.id, p.name || ' (' || p.id || ')' FROM saved_playlists p
    WHERE EXISTS (SELECT 1 FROM saved_playlists other WHERE other.id < p.id AND other.name = p.name COLLATE NOCASE)
    UNION ALL
    SELECT c.id, c.name || ' (' || c.id || ')' FROM candidates c
    WHERE EXISTS (SELECT 1 FROM saved_playlists p WHERE p.name = c.name COLLATE NOCASE)
)
SELECT id, name FROM candidates c
WHERE NOT EXISTS (SELECT 1 FROM saved_playlists p WHERE p.name = c.name COLLATE NOCASE);
UPDATE saved_playlists SET name = (SELECT name FROM playlist_renames r WHERE r.id = saved_playlists.id)
WHERE id IN (SELECT id FROM playlist_renames);
DROP TABLE playlist_renames;
CREATE UNIQUE INDEX saved_playlists_name_nocase ON saved_playlists(name COLLATE NOCASE);
