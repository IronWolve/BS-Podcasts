"""Library persistence with no Qt dependencies."""

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
            "SELECT e.*, s.title AS show_title FROM episodes e "
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
            for episode in feed.episodes:
                connection.execute(
                    """INSERT INTO episodes(
                       show_id, external_id, title, description, media_url,
                       mime_type, published_at, duration_seconds, added_at)
                       VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                       ON CONFLICT(show_id, external_id) DO UPDATE SET
                       title=excluded.title,
                       description=excluded.description,
                       media_url=excluded.media_url,
                       mime_type=excluded.mime_type,
                       published_at=excluded.published_at,
                       duration_seconds=excluded.duration_seconds""",
                    (
                        show_id,
                        episode.external_id,
                        episode.title,
                        episode.description,
                        episode.media_url,
                        episode.mime_type,
                        episode.published_at,
                        episode.duration_seconds,
                        now,
                    ),
                )
        return len(feed.episodes)

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
                """SELECT e.*, s.title AS show_title FROM queue q
                   JOIN episodes e ON e.id=q.episode_id
                   JOIN shows s ON s.id=e.show_id ORDER BY q.position"""
            ).fetchall()
        return [self._episode(row) for row in rows]

    @staticmethod
    def _show_select() -> str:
        return (
            "SELECT s.*, COUNT(e.id) AS episode_count, "
            "COALESCE(SUM(CASE WHEN e.is_new=1 THEN 1 ELSE 0 END), 0) AS new_count "
            "FROM shows s LEFT JOIN episodes e ON e.show_id=s.id"
        )

    @staticmethod
    def _show(row) -> Show:
        return Show(
            id=row["id"],
            feed_url=row["feed_url"],
            title=row["title"],
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
        )
