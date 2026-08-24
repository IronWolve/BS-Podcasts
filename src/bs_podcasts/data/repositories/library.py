"""Library persistence with no Qt dependencies."""

from pathlib import Path
import time

from ...domain import Episode, FeedData, Health, Show
from ..database import Database


MAX_REFRESH_FAILURES = 3


class LibraryRepository:
    def __init__(self, database: Database):
        self.database = database

    def add_show(self, feed_url: str, title: str = "", source: str = "rss") -> Show:
        now = time.time()
        with self.database.connect() as connection:
            connection.execute(
                "INSERT OR IGNORE INTO shows(feed_url, title, source, added_at) "
                "VALUES (?, ?, ?, ?)",
                (feed_url, title, source, now),
            )
            row = connection.execute(
                "SELECT id FROM shows WHERE feed_url=?", (feed_url,)
            ).fetchone()
        return self.get_show(row["id"])

    def get_show(self, show_id: int) -> Show | None:
        with self.database.connect() as connection:
            row = connection.execute(
                self._show_select() + " WHERE s.id=? GROUP BY s.id", (show_id,)
            ).fetchone()
        return self._show(row) if row else None

    def list_shows(self, include_suspended: bool = True) -> list[Show]:
        where = "" if include_suspended else " WHERE s.suspended=0"
        with self.database.connect() as connection:
            rows = connection.execute(
                self._show_select() + where + " GROUP BY s.id ORDER BY s.title COLLATE NOCASE"
            ).fetchall()
        return [self._show(row) for row in rows]

    def list_episodes(self, show_id: int | None = None, limit: int = 500) -> list[Episode]:
        sql = (
            "SELECT e.*, s.title AS show_title, s.artwork_path AS artwork_path FROM episodes e "
            "JOIN shows s ON s.id=e.show_id"
        )
        params: list[object] = []
        if show_id is not None:
            sql += " WHERE e.show_id=?"
            params.append(show_id)
        sql += " ORDER BY e.published_at DESC, e.id DESC LIMIT ?"
        params.append(limit)
        with self.database.connect() as connection:
            rows = connection.execute(sql, params).fetchall()
        return [self._episode(row) for row in rows]

    def get_episode(self, episode_id: int) -> Episode | None:
        with self.database.connect() as connection:
            row = connection.execute(
                """SELECT e.*, s.title AS show_title, s.artwork_path AS artwork_path FROM episodes e
                   JOIN shows s ON s.id=e.show_id WHERE e.id=?""",
                (episode_id,),
            ).fetchone()
        return self._episode(row) if row else None

    def list_history(self, limit: int = 200) -> list[Episode]:
        with self.database.connect() as connection:
            rows = connection.execute(
                """SELECT e.*, s.title AS show_title, s.artwork_path AS artwork_path FROM episodes e
                   JOIN shows s ON s.id=e.show_id
                   WHERE e.last_played IS NOT NULL
                   ORDER BY e.last_played DESC LIMIT ?""",
                (limit,),
            ).fetchall()
        return [self._episode(row) for row in rows]

    def import_feed(self, show_id: int, feed: FeedData) -> int:
        now = time.time()
        with self.database.connect() as connection:
            connection.execute(
                """UPDATE shows SET
                   title=COALESCE(NULLIF(?, ''), title),
                   author=COALESCE(NULLIF(?, ''), author),
                   description=COALESCE(NULLIF(?, ''), description),
                   website_url=COALESCE(NULLIF(?, ''), website_url),
                   artwork_url=COALESCE(NULLIF(?, ''), artwork_url)
                   WHERE id=?""",
                (
                    feed.title,
                    feed.author,
                    feed.description,
                    feed.website_url,
                    feed.artwork_url,
                    show_id,
                ),
            )
            initial_import = connection.execute(
                "SELECT 1 FROM episodes WHERE show_id=? LIMIT 1", (show_id,)
            ).fetchone() is None
            new_flag = 0 if initial_import else 1
            for episode in feed.episodes:
                connection.execute(
                    """INSERT INTO episodes(
                       show_id, external_id, title, description, media_url,
                       mime_type, published_at, duration_seconds,
                       transcript_url, transcript_type, is_new, added_at,
                       chapters_url, artwork_url)
                       VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                       ON CONFLICT(show_id, external_id) DO UPDATE SET
                       title=excluded.title,
                       description=excluded.description,
                       media_url=excluded.media_url,
                       mime_type=excluded.mime_type,
                       published_at=excluded.published_at,
                       duration_seconds=excluded.duration_seconds,
                       transcript_url=excluded.transcript_url,
                       transcript_type=excluded.transcript_type,
                       chapters_url=excluded.chapters_url,
                       artwork_url=excluded.artwork_url""",
                    (
                        show_id,
                        episode.external_id,
                        episode.title,
                        episode.description,
                        episode.media_url,
                        episode.mime_type,
                        episode.published_at,
                        episode.duration_seconds,
                        episode.transcript_url,
                        episode.transcript_type,
                        new_flag,
                        now,
                        episode.chapters_url,
                        episode.artwork_url,
                    ),
                )
        return len(feed.episodes)

    def removal_preview(self, show_id: int) -> dict:
        """Exact targets that removing a show would delete; nothing is touched."""
        show = self.get_show(show_id)
        if show is None:
            return {}
        with self.database.connect() as connection:
            episode_count = connection.execute("SELECT COUNT(*) FROM episodes WHERE show_id=?", (show_id,)).fetchone()[0]
            rows = connection.execute(
                "SELECT d.target_path, d.partial_path, d.state FROM downloads d "
                "JOIN episodes e ON e.id=d.episode_id WHERE e.show_id=?",
                (show_id,),
            ).fetchall()
            queued = connection.execute(
                "SELECT COUNT(*) FROM queue q JOIN episodes e ON e.id=q.episode_id WHERE e.show_id=?", (show_id,)
            ).fetchone()[0]
            bookmarks = connection.execute(
                "SELECT COUNT(*) FROM bookmarks b JOIN episodes e ON e.id=b.episode_id WHERE e.show_id=?", (show_id,)
            ).fetchone()[0]
        files = []
        for row in rows:
            for candidate in (row["target_path"], row["partial_path"]):
                path = Path(candidate) if candidate else None
                if path is not None and path.is_file() and str(path) not in {f[0] for f in files}:
                    files.append((str(path), path.stat().st_size))
        if show.artwork_path and Path(show.artwork_path).is_file():
            files.append((show.artwork_path, Path(show.artwork_path).stat().st_size))
        return {
            "show": show,
            "episodes": episode_count,
            "queued": queued,
            "bookmarks": bookmarks,
            "files": files,
            "bytes": sum(size for _path, size in files),
        }

    def remove_show(self, show_id: int, delete_files: bool = True) -> dict:
        """Delete a show, its episodes and dependent rows (cascade), and its files."""
        preview = self.removal_preview(show_id)
        if not preview:
            return {}
        removed_files = []
        if delete_files:
            for path, _size in preview["files"]:
                try:
                    Path(path).unlink()
                    removed_files.append(path)
                except OSError:
                    pass
        with self.database.connect() as connection:
            connection.execute("DELETE FROM shows WHERE id=?", (show_id,))
        preview["removed_files"] = removed_files
        return preview

    def set_health(self, show_id: int, health: Health):
        with self.database.connect() as connection:
            connection.execute("UPDATE shows SET health=? WHERE id=?", (health.value, show_id))

    def record_refresh_success(
        self,
        show_id: int,
        health: Health,
        etag: str = "",
        last_modified: str = "",
        canonical_url: str = "",
    ):
        with self.database.connect() as connection:
            connection.execute(
                """UPDATE shows SET health=?, fail_count=0, suspended=0,
                   etag=COALESCE(NULLIF(?, ''), etag),
                   last_modified=COALESCE(NULLIF(?, ''), last_modified),
                   canonical_url=COALESCE(NULLIF(?, ''), canonical_url),
                   last_refresh=? WHERE id=?""",
                (
                    health.value,
                    etag,
                    last_modified,
                    canonical_url,
                    time.time(),
                    show_id,
                ),
            )

    def record_refresh_failure(self, show_id: int) -> Health:
        with self.database.connect() as connection:
            row = connection.execute(
                "UPDATE shows SET fail_count=fail_count+1, health='error' "
                "WHERE id=? RETURNING fail_count",
                (show_id,),
            ).fetchone()
            if row is None:
                return Health.UNKNOWN
            if row["fail_count"] >= MAX_REFRESH_FAILURES:
                connection.execute(
                    "UPDATE shows SET suspended=1, health='suspended' WHERE id=?",
                    (show_id,),
                )
                return Health.SUSPENDED
        return Health.ERROR

    def rearm_show(self, show_id: int):
        with self.database.connect() as connection:
            connection.execute(
                "UPDATE shows SET suspended=0, fail_count=0, health='unknown' WHERE id=?",
                (show_id,),
            )

    def set_artwork_path(self, show_id: int, path: str):
        with self.database.connect() as connection:
            connection.execute("UPDATE shows SET artwork_path=? WHERE id=?", (path, show_id))

    def set_setting(self, key: str, value: str):
        with self.database.connect() as connection:
            connection.execute(
                "INSERT INTO settings(key, value) VALUES (?, ?) "
                "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
                (key, value),
            )

    def get_setting(self, key: str, default: str = "") -> str:
        with self.database.connect() as connection:
            row = connection.execute("SELECT value FROM settings WHERE key=?", (key,)).fetchone()
        return row["value"] if row else default

    def enqueue(self, episode_id: int):
        with self.database.connect() as connection:
            row = connection.execute("SELECT COALESCE(MAX(position), 0) + 1 AS next FROM queue").fetchone()
            connection.execute(
                "INSERT OR IGNORE INTO queue(episode_id, position, added_at) VALUES (?, ?, ?)",
                (episode_id, row["next"], time.time()),
            )

    def list_queue(self) -> list[Episode]:
        with self.database.connect() as connection:
            rows = connection.execute(
                """SELECT e.*, s.title AS show_title, s.artwork_path AS artwork_path FROM queue q
                   JOIN episodes e ON e.id=q.episode_id
                   JOIN shows s ON s.id=e.show_id ORDER BY q.position"""
            ).fetchall()
        return [self._episode(row) for row in rows]

    def dequeue(self, episode_id: int):
        with self.database.connect() as connection:
            connection.execute("DELETE FROM queue WHERE episode_id=?", (episode_id,))
            rows = connection.execute("SELECT id FROM queue ORDER BY position, id").fetchall()
            for position, row in enumerate(rows, start=1):
                connection.execute(
                    "UPDATE queue SET position=? WHERE id=?", (position, row["id"])
                )

    def reorder_queue(self, episode_ids: list[int]):
        with self.database.connect() as connection:
            existing = {
                row["episode_id"]
                for row in connection.execute("SELECT episode_id FROM queue").fetchall()
            }
            requested = [episode_id for episode_id in episode_ids if episode_id in existing]
            requested.extend(sorted(existing - set(requested)))
            for position, episode_id in enumerate(requested, start=1):
                connection.execute(
                    "UPDATE queue SET position=? WHERE episode_id=?",
                    (position, episode_id),
                )

    def update_position(self, episode_id: int, seconds: float):
        # Starting an episode is what makes it "not new" everywhere in the UI.
        with self.database.connect() as connection:
            connection.execute(
                "UPDATE episodes SET position_seconds=?, last_played=?, is_new=0 WHERE id=?",
                (max(0.0, float(seconds)), time.time(), episode_id),
            )

    def set_episode_artwork_path(self, episode_id: int, path: str):
        with self.database.connect() as connection:
            connection.execute("UPDATE episodes SET artwork_path=? WHERE id=?", (path, episode_id))

    def mark_show_seen(self, show_id: int) -> int:
        """Opening a show clears its new-episode badge."""
        with self.database.connect() as connection:
            return connection.execute("UPDATE episodes SET is_new=0 WHERE show_id=? AND is_new=1", (show_id,)).rowcount

    def mark_show_played(self, show_id: int, played: bool = True) -> int:
        with self.database.connect() as connection:
            cursor = connection.execute(
                "UPDATE episodes SET played=?, is_new=0 WHERE show_id=? AND played!=?",
                (int(played), show_id, int(played)),
            )
            return cursor.rowcount

    def clear_history(self, episode_id: int | None = None) -> int:
        with self.database.connect() as connection:
            if episode_id is None:
                cursor = connection.execute("UPDATE episodes SET last_played=NULL WHERE last_played IS NOT NULL")
            else:
                cursor = connection.execute("UPDATE episodes SET last_played=NULL WHERE id=?", (episode_id,))
            return cursor.rowcount

    def clear_queue(self) -> int:
        with self.database.connect() as connection:
            return connection.execute("DELETE FROM queue").rowcount

    def queue_to_front(self, episode_id: int):
        ids = [episode.id for episode in self.list_queue()]
        if episode_id in ids:
            ids.remove(episode_id)
        self.reorder_queue([episode_id] + ids)

    def mark_played(self, episode_id: int, played: bool = True):
        with self.database.connect() as connection:
            connection.execute(
                "UPDATE episodes SET played=?, is_new=0, last_played=? WHERE id=?",
                (int(played), time.time(), episode_id),
            )

    def set_current_playback(self, episode_id: int | None, state: str):
        with self.database.connect() as connection:
            connection.execute(
                """UPDATE playback_state SET episode_id=?, state=?, updated_at=?
                   WHERE singleton_id=1""",
                (episode_id, state, time.time()),
            )

    def current_playback(self) -> tuple[int | None, str]:
        with self.database.connect() as connection:
            row = connection.execute(
                "SELECT episode_id, state FROM playback_state WHERE singleton_id=1"
            ).fetchone()
        return (row["episode_id"], row["state"]) if row else (None, "idle")

    def update_show_playback(
        self,
        show_id: int,
        speed: float | None = None,
        skip_back: int | None = None,
        skip_forward: int | None = None,
        auto_continue: bool | None = None,
        trim_level: str | None = None,
    ):
        show = self.get_show(show_id)
        if show is None:
            return
        with self.database.connect() as connection:
            connection.execute(
                """UPDATE shows SET playback_speed=?, skip_back=?,
                   skip_forward=?, auto_continue=?, trim_level=? WHERE id=?""",
                (
                    speed if speed is not None else show.playback_speed,
                    skip_back if skip_back is not None else show.skip_back,
                    skip_forward if skip_forward is not None else show.skip_forward,
                    int(auto_continue if auto_continue is not None else show.auto_continue),
                    trim_level if trim_level is not None else show.trim_level,
                    show_id,
                ),
            )

    def search(self, query: str, limit: int = 100) -> tuple[list[Show], list[Episode]]:
        escaped = query.strip().replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
        pattern = f"%{escaped}%"
        with self.database.connect() as connection:
            show_rows = connection.execute(
                self._show_select()
                + " WHERE (s.title LIKE ? ESCAPE '\\' OR s.author LIKE ? ESCAPE '\\')"
                + " GROUP BY s.id ORDER BY s.title COLLATE NOCASE LIMIT ?",
                (pattern, pattern, limit),
            ).fetchall()
            episode_rows = connection.execute(
                """SELECT e.*, s.title AS show_title, s.artwork_path AS artwork_path FROM episodes e
                   JOIN shows s ON s.id=e.show_id
                   WHERE e.title LIKE ? ESCAPE '\\'
                      OR e.description LIKE ? ESCAPE '\\'
                      OR s.title LIKE ? ESCAPE '\\'
                   ORDER BY e.published_at DESC, e.id DESC LIMIT ?""",
                (pattern, pattern, pattern, limit),
            ).fetchall()
        return (
            [self._show(row) for row in show_rows],
            [self._episode(row) for row in episode_rows],
        )

    @staticmethod
    def _show_select() -> str:
        return (
            "SELECT s.*, COUNT(e.id) AS episode_count, "
            "COALESCE(SUM(CASE WHEN e.is_new=1 THEN 1 ELSE 0 END), 0) AS new_count, "
            "COALESCE((SELECT e2.title FROM episodes e2 WHERE e2.show_id=s.id "
            "ORDER BY e2.published_at DESC, e2.id DESC LIMIT 1), '') "
            "AS latest_episode_title, "
            "COALESCE((SELECT e3.published_at FROM episodes e3 WHERE e3.show_id=s.id "
            "ORDER BY e3.published_at DESC, e3.id DESC LIMIT 1), '') "
            "AS latest_episode_published_at "
            "FROM shows s LEFT JOIN episodes e ON e.show_id=s.id"
        )

    @staticmethod
    def _show(row) -> Show:
        return Show(
            id=row["id"],
            feed_url=row["feed_url"],
            title=row["title"],
            canonical_url=row["canonical_url"],
            author=row["author"],
            description=row["description"],
            website_url=row["website_url"],
            artwork_url=row["artwork_url"],
            artwork_path=row["artwork_path"],
            source=row["source"],
            health=Health(row["health"]),
            fail_count=row["fail_count"],
            suspended=bool(row["suspended"]),
            etag=row["etag"],
            last_modified=row["last_modified"],
            last_refresh=row["last_refresh"],
            episode_count=row["episode_count"],
            new_count=row["new_count"],
            latest_episode_title=row["latest_episode_title"],
            latest_episode_published_at=row["latest_episode_published_at"],
            playback_speed=row["playback_speed"],
            skip_back=row["skip_back"],
            skip_forward=row["skip_forward"],
            auto_continue=bool(row["auto_continue"]),
            trim_level=row["trim_level"],
        )

    @staticmethod
    def _episode(row) -> Episode:
        return Episode(
            id=row["id"],
            show_id=row["show_id"],
            external_id=row["external_id"],
            title=row["title"],
            show_title=row["show_title"],
            description=row["description"],
            media_url=row["media_url"],
            mime_type=row["mime_type"],
            published_at=row["published_at"],
            duration_seconds=row["duration_seconds"],
            position_seconds=row["position_seconds"],
            played=bool(row["played"]),
            is_new=bool(row["is_new"]),
            downloaded_path=row["downloaded_path"],
            last_played=row["last_played"],
            chapters_url=row["chapters_url"] if "chapters_url" in row.keys() else "",
            artwork_url=row["artwork_url"] if "artwork_url" in row.keys() else "",
            transcript_url=row["transcript_url"],
            transcript_type=row["transcript_type"],
            artwork_path=row["artwork_path"],
        )
