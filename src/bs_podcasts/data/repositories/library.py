"""Library persistence with no Qt dependencies."""

from pathlib import Path
import json
import logging
import threading
import time

from ...domain import Episode, FeedData, Health, Show
from ..database import Database


_log = logging.getLogger("bs_podcasts")

MAX_REFRESH_FAILURES = 3

# Feed-synced episode columns: drives the change-detection SELECT and both
# comparison tuples in import_feed so the three can never drift apart.
_EPISODE_SYNC_COLUMNS = (
    "title", "description", "media_url", "mime_type", "published_at",
    "duration_seconds", "transcript_url", "transcript_type", "chapters_url",
    "artwork_url", "website_url", "author", "season_number", "episode_number",
    "episode_type", "explicit", "enclosure_bytes",
)


class LibraryRepository:
    def __init__(self, database: Database):
        self.database = database
        self._settings_cache: dict[str, str] | None = None
        self._settings_lock = threading.Lock()

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
            # The show filter is pushed into the aggregate as well as the
            # outer WHERE: an outer equality does not reliably reach inside a
            # LEFT JOINed GROUP BY subquery, so fetching one show was
            # aggregating every episode in the library — on a path reached
            # from update_show_playback, i.e. every per-show settings change.
            row = connection.execute(
                self._show_select("show_id=?") + " WHERE s.id=? GROUP BY s.id",
                (show_id, show_id),
            ).fetchone()
        return self._show(row) if row else None

    def list_shows(self, include_suspended: bool = True) -> list[Show]:
        where = "" if include_suspended else " WHERE s.suspended=0"
        with self.database.connect() as connection:
            rows = connection.execute(
                self._show_select() + where + " GROUP BY s.id ORDER BY s.title COLLATE NOCASE"
            ).fetchall()
        return [self._show(row) for row in rows]

    def list_episodes(self, show_id: int | None = None, limit: int | None = 500) -> list[Episode]:
        sql = (
            "SELECT e.*, s.title AS show_title, COALESCE(NULLIF(e.episode_artwork_path, ''), s.artwork_path) AS artwork_path FROM episodes e "
            "JOIN shows s ON s.id=e.show_id"
        )
        params: list[object] = []
        if show_id is not None:
            sql += " WHERE e.show_id=?"
            params.append(show_id)
        sql += " ORDER BY e.published_at DESC, e.id DESC"
        if limit is not None:
            sql += " LIMIT ?"
            params.append(limit)
        with self.database.connect() as connection:
            rows = connection.execute(sql, params).fetchall()
        return [self._episode(row) for row in rows]

    def get_episode(self, episode_id: int) -> Episode | None:
        with self.database.connect() as connection:
            row = connection.execute(
                """SELECT e.*, s.title AS show_title, COALESCE(NULLIF(e.episode_artwork_path, ''), s.artwork_path) AS artwork_path FROM episodes e
                   JOIN shows s ON s.id=e.show_id WHERE e.id=?""",
                (episode_id,),
            ).fetchone()
        return self._episode(row) if row else None

    def list_history(self, limit: int = 200) -> list[Episode]:
        with self.database.connect() as connection:
            rows = connection.execute(
                """SELECT e.*, s.title AS show_title, COALESCE(NULLIF(e.episode_artwork_path, ''), s.artwork_path) AS artwork_path FROM episodes e
                   JOIN shows s ON s.id=e.show_id
                   WHERE e.last_played IS NOT NULL
                   ORDER BY e.last_played DESC LIMIT ?""",
                (limit,),
            ).fetchall()
        return [self._episode(row) for row in rows]

    def list_favorites(self) -> list[Episode]:
        with self.database.connect() as connection:
            rows = connection.execute(
                """SELECT e.*, s.title AS show_title,
                   COALESCE(NULLIF(e.episode_artwork_path, ''), s.artwork_path) AS artwork_path
                   FROM episodes e JOIN shows s ON s.id=e.show_id
                   WHERE e.favorite=1 ORDER BY e.published_at DESC, e.id DESC"""
            ).fetchall()
        return [self._episode(row) for row in rows]

    def retention_candidates(
        self, show_id: int, keep_latest: int | None, older_than_days: int | None
    ) -> list[Episode]:
        """Downloaded episodes a feed rule may remove; favorites never qualify."""
        with self.database.connect() as connection:
            rows = connection.execute(
                """SELECT e.*, s.title AS show_title,
                   COALESCE(NULLIF(e.episode_artwork_path, ''), s.artwork_path) AS artwork_path
                   FROM episodes e JOIN shows s ON s.id=e.show_id
                   WHERE e.show_id=? AND e.downloaded_path != '' AND e.favorite=0
                   ORDER BY e.published_at DESC, e.id DESC""",
                (show_id,),
            ).fetchall()
        episodes = [self._episode(row) for row in rows]
        keep_latest = max(0, int(keep_latest or 0))
        cutoff = time.time() - max(0, int(older_than_days or 0)) * 86400
        candidates = []
        for index, episode in enumerate(episodes):
            outside_count = bool(keep_latest and index >= keep_latest)
            older = False
            if older_than_days:
                try:
                    from datetime import datetime
                    older = datetime.fromisoformat(
                        episode.published_at.replace("Z", "+00:00")
                    ).timestamp() < cutoff
                except (TypeError, ValueError):
                    older = False
            if outside_count or older:
                candidates.append(episode)
        return candidates

    def import_feed(self, show_id: int, feed: FeedData) -> int:
        now = time.time()
        with self.database.connect() as connection:
            connection.execute(
                """UPDATE shows SET
                   title=COALESCE(NULLIF(?, ''), title),
                   author=COALESCE(NULLIF(?, ''), author),
                   description=COALESCE(NULLIF(?, ''), description),
                   website_url=COALESCE(NULLIF(?, ''), website_url),
                   artwork_url=COALESCE(NULLIF(?, ''), artwork_url),
                   categories=COALESCE(NULLIF(?, ''), categories)
                   WHERE id=?""",
                (
                    feed.title,
                    feed.author,
                    feed.description,
                    feed.website_url,
                    feed.artwork_url,
                    ", ".join(feed.categories),
                    show_id,
                ),
            )
            # Most refreshes carry the same episodes again; comparing against the
            # stored rows keeps the transaction (and the write lock SQLite takes
            # for it) proportional to what actually changed. Only the rows the
            # feed still carries are fetched — a 3,000-episode back catalog
            # must not be materialized (descriptions included) per refresh.
            initial_import = connection.execute(
                "SELECT 1 FROM episodes WHERE show_id=? LIMIT 1", (show_id,)
            ).fetchone() is None
            incoming_ids = [episode.external_id for episode in feed.episodes]
            select = (
                "SELECT external_id, " + ", ".join(_EPISODE_SYNC_COLUMNS)
                + " FROM episodes WHERE show_id=? AND external_id IN ({})"
            )
            existing = {}
            for start in range(0, len(incoming_ids), 500):
                chunk = incoming_ids[start:start + 500]
                for row in connection.execute(
                    select.format(",".join("?" * len(chunk))), [show_id, *chunk]
                ).fetchall():
                    existing[row["external_id"]] = tuple(row[column] for column in _EPISODE_SYNC_COLUMNS)
            imported = sum(episode.external_id not in existing for episode in feed.episodes)
            new_flag = 0 if initial_import else 1
            for episode in feed.episodes:
                incoming = tuple(
                    (None if episode.explicit is None else int(episode.explicit))
                    if column == "explicit" else getattr(episode, column)
                    for column in _EPISODE_SYNC_COLUMNS
                )
                if existing.get(episode.external_id) == incoming:
                    continue
                connection.execute(
                    """INSERT INTO episodes(
                       show_id, external_id, title, description, media_url,
                       mime_type, published_at, duration_seconds,
                       transcript_url, transcript_type, is_new, added_at,
                       chapters_url, artwork_url, website_url, author,
                       season_number, episode_number, episode_type, explicit,
                       enclosure_bytes)
                       VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
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
                       artwork_url=excluded.artwork_url,
                       website_url=excluded.website_url,
                       author=excluded.author,
                       season_number=excluded.season_number,
                       episode_number=excluded.episode_number,
                       episode_type=excluded.episode_type,
                       explicit=excluded.explicit,
                       enclosure_bytes=excluded.enclosure_bytes""",
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
                        episode.website_url,
                        episode.author,
                        episode.season_number,
                        episode.episode_number,
                        episode.episode_type,
                        None if episode.explicit is None else int(episode.explicit),
                        episode.enclosure_bytes,
                    ),
                )
        return imported

    def episodes_by_ids(self, episode_ids) -> dict[int, Episode]:
        """Fetch a set of episodes in one query for download/bookmark views."""
        ids = list(dict.fromkeys(int(value) for value in episode_ids if value))
        if not ids:
            return {}
        placeholders = ",".join("?" for _ in ids)
        with self.database.connect() as connection:
            rows = connection.execute(
                f"""SELECT e.*, s.title AS show_title,
                    COALESCE(NULLIF(e.episode_artwork_path, ''), s.artwork_path) AS artwork_path
                    FROM episodes e JOIN shows s ON s.id=e.show_id
                    WHERE e.id IN ({placeholders})""",
                ids,
            ).fetchall()
        return {row["id"]: self._episode(row) for row in rows}

    def episode_count(self) -> int:
        """Every stored episode — the page subtitle must not report the
        capped size of the newest-N read as if it were the library total."""
        with self.database.connect() as connection:
            return connection.execute("SELECT COUNT(*) FROM episodes").fetchone()[0]

    def new_episode_count(self) -> int:
        """Library-wide unseen-episode total for badges; cheap during batches."""
        with self.database.connect() as connection:
            return connection.execute(
                "SELECT COUNT(*) FROM episodes WHERE is_new=1"
            ).fetchone()[0]

    def artwork_paths(self, exclude_show_id: int | None = None) -> set[str]:
        """Every artwork file the library still references.

        `exclude_show_id` ignores one show's own rows, so a removal can ask
        what would remain referenced *once this show is gone* while the show
        is still present — which is what lets files be unlinked before the
        records naming them are dropped.
        """
        shows_clause = "" if exclude_show_id is None else " AND id != ?"
        episodes_clause = "" if exclude_show_id is None else " AND show_id != ?"
        params = () if exclude_show_id is None else (exclude_show_id, exclude_show_id)
        with self.database.connect() as connection:
            rows = connection.execute(
                f"SELECT artwork_path FROM shows WHERE artwork_path != ''{shows_clause} "
                f"UNION SELECT episode_artwork_path FROM episodes "
                f"WHERE episode_artwork_path != ''{episodes_clause}",
                params,
            ).fetchall()
        return {row[0] for row in rows}

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
        with self.database.connect() as connection:
            artwork_rows = connection.execute(
                "SELECT DISTINCT episode_artwork_path FROM episodes WHERE show_id=? AND episode_artwork_path != ''",
                (show_id,),
            ).fetchall()
        files = []
        artwork = []
        for row in rows:
            for candidate in (row["target_path"], row["partial_path"]):
                path = Path(candidate) if candidate else None
                if path is not None and path.is_file() and str(path) not in {f[0] for f in files}:
                    files.append((str(path), path.stat().st_size))
        # Artwork is cache-shared by URL: tracked separately so removal can
        # keep any file another show still references.
        for candidate in [show.artwork_path] + [row[0] for row in artwork_rows]:
            path = Path(candidate) if candidate else None
            if path is not None and path.is_file() and str(path) not in {f[0] for f in artwork}:
                artwork.append((str(path), path.stat().st_size))
        files.extend(entry for entry in artwork if entry not in files)
        return {
            "show": show,
            "episodes": episode_count,
            "queued": queued,
            "bookmarks": bookmarks,
            "files": files,
            "bytes": sum(size for _path, size in files),
        }

    ORPHAN_SETTING = "storage.orphans"
    MAX_ORPHANS = 200

    def orphaned_files(self) -> list[str]:
        """Paths a previous removal could not unlink."""
        try:
            stored = json.loads(self.get_setting(self.ORPHAN_SETTING, "") or "[]")
        except ValueError:
            return []
        if not isinstance(stored, list):
            return []
        return [path for path in stored if isinstance(path, str)]

    def _remember_orphans(self, paths):
        """Keep a file that survived deletion findable.

        Once a show's rows are gone nothing else names its files, so a failed
        unlink would otherwise become untracked disk usage no sweep could
        ever reach.
        """
        merged = list(dict.fromkeys([*self.orphaned_files(), *paths]))
        self.set_setting(self.ORPHAN_SETTING, json.dumps(merged[-self.MAX_ORPHANS :]))

    def sweep_orphans(self) -> list[str]:
        """Retry previously-stuck deletions; returns the paths cleared."""
        remaining = []
        cleared = []
        for path in self.orphaned_files():
            candidate = Path(path)
            try:
                if candidate.is_file():
                    candidate.unlink()
                cleared.append(path)
            except OSError:
                remaining.append(path)
        if cleared:
            self.set_setting(self.ORPHAN_SETTING, json.dumps(remaining))
        return cleared

    def remove_show(self, show_id: int, delete_files: bool = True) -> dict:
        """Delete a show, its episodes and dependent rows (cascade), and its files.

        Files go first. Dropping the rows first — as this used to — meant a
        file that could not be removed (locked by the player, an in-flight
        download, a scanner; guaranteed on Windows) was left on disk with no
        row anywhere still naming it, so nothing could find or retry it and
        the reclaimed-bytes figure quietly under-reported. Whatever survives
        the unlink is recorded as an orphan rather than forgotten.
        """
        preview = self.removal_preview(show_id)
        if not preview:
            return {}
        self.sweep_orphans()  # each removal retries what an earlier one left
        removed_files = []
        failed_files = []
        if delete_files:
            # Artwork the REMAINING library still references must survive: the
            # cache is keyed by URL, so two shows can share one file. Asking
            # while this show still exists means excluding its own rows.
            still_referenced = self.artwork_paths(exclude_show_id=show_id)
            for path, _size in preview["files"]:
                if path in still_referenced:
                    continue
                try:
                    Path(path).unlink()
                except FileNotFoundError:
                    removed_files.append(path)  # already gone; nothing to keep
                except OSError as exc:
                    failed_files.append(path)
                    _log.warning("Could not remove %s while unsubscribing: %s", path, exc)
                else:
                    removed_files.append(path)
        retention_key = f"retention.confirmed.{show_id}"
        with self.database.connect() as connection:
            connection.execute("DELETE FROM shows WHERE id=?", (show_id,))
            connection.execute("DELETE FROM settings WHERE key=?", (retention_key,))
        # That raw DELETE bypasses the settings cache, and SQLite reuses a
        # deleted show's rowid — a stale entry would be read back as the NEXT
        # show's retention confirmation and skip its delete prompt entirely.
        with self._settings_lock:
            if self._settings_cache is not None:
                self._settings_cache.pop(retention_key, None)
        if failed_files:
            self._remember_orphans(failed_files)
        preview["removed_files"] = removed_files
        preview["failed_files"] = failed_files
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
        with self._settings_lock:
            if self._settings_cache is not None:
                self._settings_cache[key] = value

    def invalidate_settings_cache(self):
        """Drop the cache after anything replaces the database file (repair)."""
        with self._settings_lock:
            self._settings_cache = None

    def get_setting(self, key: str, default: str = "") -> str:
        # Settings are read on hot paths (startup wiring, every scheduler tick);
        # one read of the tiny table replaces a connection round-trip per call.
        with self._settings_lock:
            if self._settings_cache is None:
                with self.database.connect() as connection:
                    self._settings_cache = {
                        row["key"]: row["value"]
                        for row in connection.execute("SELECT key, value FROM settings").fetchall()
                    }
            return self._settings_cache.get(key, default)

    def enqueue(self, episode_id: int):
        with self.database.connect() as connection:
            # Position computed inside the INSERT: a separate SELECT let two
            # connections claim the same position.
            connection.execute(
                "INSERT OR IGNORE INTO queue(episode_id, position, added_at) "
                "SELECT ?, COALESCE(MAX(position), 0) + 1, ? FROM queue",
                (episode_id, time.time()),
            )

    def list_queue(self) -> list[Episode]:
        with self.database.connect() as connection:
            rows = connection.execute(
                """SELECT e.*, s.title AS show_title, COALESCE(NULLIF(e.episode_artwork_path, ''), s.artwork_path) AS artwork_path FROM queue q
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
        # Position ticks come from the playback event thread while it holds
        # the playback lock; blocking here for a whole VACUUM would freeze
        # every playback control. Skipping a 5-second save is harmless.
        if self.database.maintenance_active:
            return
        # Starting an episode is what makes it "not new" everywhere in the UI.
        with self.database.connect() as connection:
            connection.execute(
                "UPDATE episodes SET position_seconds=?, last_played=?, is_new=0 WHERE id=?",
                (max(0.0, float(seconds)), time.time(), episode_id),
            )

    def set_episode_artwork_path(self, episode_id: int, path: str):
        with self.database.connect() as connection:
            connection.execute("UPDATE episodes SET episode_artwork_path=? WHERE id=?", (path, episode_id))

    def set_favorite(self, episode_id: int, favorite: bool = True) -> bool:
        with self.database.connect() as connection:
            changed = connection.execute(
                "UPDATE episodes SET favorite=? WHERE id=?",
                (int(favorite), episode_id),
            ).rowcount
        return bool(changed)

    def mark_show_seen(self, show_id: int) -> int:
        """Opening a show clears its new-episode badge."""
        with self.database.connect() as connection:
            return connection.execute("UPDATE episodes SET is_new=0 WHERE show_id=? AND is_new=1", (show_id,)).rowcount

    def clear_all_new(self, played: bool = False) -> int:
        with self.database.connect() as connection:
            if played:
                return connection.execute(
                    "UPDATE episodes SET is_new=0, played=1 WHERE is_new=1"
                ).rowcount
            return connection.execute("UPDATE episodes SET is_new=0 WHERE is_new=1").rowcount

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
            if played:
                connection.execute(
                    "UPDATE episodes SET played=1, is_new=0, last_played=? WHERE id=?",
                    (time.time(), episode_id),
                )
            else:
                # A finished episode's saved position is its duration. Leaving
                # that behind when marking it unplayed hands the next Play a
                # position that immediately re-reaches EOF. Genuine partial
                # progress is preserved — only an at-the-end position resets.
                connection.execute(
                    """UPDATE episodes SET played=0, is_new=0,
                       position_seconds = CASE
                           WHEN duration_seconds > 0
                                AND position_seconds >= duration_seconds - 2
                           THEN 0 ELSE position_seconds END
                       WHERE id=?""",
                    (episode_id,),
                )

    def set_current_playback(self, episode_id: int | None, state: str):
        if self.database.maintenance_active:
            return  # written again on the next state change
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
        auto_download_override: bool | None | object = ...,
        auto_download_limit: int | None | object = ...,
        retention_keep: int | None | object = ...,
        retention_days: int | None | object = ...,
    ):
        show = self.get_show(show_id)
        if show is None:
            return
        with self.database.connect() as connection:
            connection.execute(
                """UPDATE shows SET playback_speed=?, skip_back=?,
                   skip_forward=?, auto_continue=?, trim_level=?,
                   auto_download_override=?, auto_download_limit=?,
                   retention_keep=?, retention_days=? WHERE id=?""",
                (
                    speed if speed is not None else show.playback_speed,
                    skip_back if skip_back is not None else show.skip_back,
                    skip_forward if skip_forward is not None else show.skip_forward,
                    int(auto_continue if auto_continue is not None else show.auto_continue),
                    trim_level if trim_level is not None else show.trim_level,
                    show.auto_download_override if auto_download_override is ... else (None if auto_download_override is None else int(auto_download_override)),
                    show.auto_download_limit if auto_download_limit is ... else auto_download_limit,
                    show.retention_keep if retention_keep is ... else retention_keep,
                    show.retention_days if retention_days is ... else retention_days,
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
                """SELECT e.*, s.title AS show_title, COALESCE(NULLIF(e.episode_artwork_path, ''), s.artwork_path) AS artwork_path FROM episodes e
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
    def _show_select(episode_filter: str = "") -> str:
        # One pass over episodes per query instead of an aggregate join plus two
        # correlated subqueries per show. With exactly one max() aggregate,
        # SQLite documents that the bare columns (title, published_at) come from
        # the row where that max was reached; the appended '~'-prefixed id
        # breaks published_at ties the same way ORDER BY ... , id DESC did.
        return (
            "SELECT s.*, COALESCE(agg.episode_count, 0) AS episode_count, "
            "COALESCE(agg.new_count, 0) AS new_count, "
            "COALESCE(agg.latest_episode_title, '') AS latest_episode_title, "
            "COALESCE(agg.latest_episode_published_at, '') AS latest_episode_published_at "
            "FROM shows s LEFT JOIN ("
            "SELECT show_id, COUNT(*) AS episode_count, "
            "COALESCE(SUM(CASE WHEN is_new=1 THEN 1 ELSE 0 END), 0) AS new_count, "
            # An empty published_at must sort BELOW every real date ('~' alone
            # would outrank digit-leading ISO strings and pin an undated
            # episode as the show's 'latest' forever).
            "MAX(COALESCE(NULLIF(published_at, ''), '0') || printf('~%012d', id)) AS latest_key, "
            "title AS latest_episode_title, "
            "published_at AS latest_episode_published_at "
            "FROM episodes"
            + (f" WHERE {episode_filter}" if episode_filter else "")
            + " GROUP BY show_id"
            ") agg ON agg.show_id=s.id"
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
            auto_download_override=(bool(row["auto_download_override"]) if row["auto_download_override"] is not None else None),
            auto_download_limit=row["auto_download_limit"],
            retention_keep=row["retention_keep"],
            retention_days=row["retention_days"],
            categories=row["categories"],
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
            website_url=row["website_url"] if "website_url" in row.keys() else "",
            author=row["author"] if "author" in row.keys() else "",
            season_number=row["season_number"] if "season_number" in row.keys() else None,
            episode_number=row["episode_number"] if "episode_number" in row.keys() else None,
            episode_type=row["episode_type"] if "episode_type" in row.keys() else "",
            explicit=(bool(row["explicit"]) if "explicit" in row.keys() and row["explicit"] is not None else None),
            enclosure_bytes=row["enclosure_bytes"] if "enclosure_bytes" in row.keys() else 0,
            favorite=bool(row["favorite"]) if "favorite" in row.keys() else False,
        )
