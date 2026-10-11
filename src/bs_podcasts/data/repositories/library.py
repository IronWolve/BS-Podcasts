"""Library persistence with no Qt dependencies."""

from pathlib import Path
import json
import logging
import sqlite3
import threading
import time

from ...domain import Episode, FeedData, Health, Show
from ...privacy import redact
from ..database import Database
from ..files import (
    FILE_IRREGULAR,
    FILE_MISSING,
    FILE_PRESENT,
    FILE_UNCONFIRMED,
    inspect_download_file,
)


_log = logging.getLogger("bs_podcasts")

MAX_REFRESH_FAILURES = 3
MAX_REFRESH_ERROR_BYTES = 2048


def _confirmed_path_list(confirmed_paths) -> list[str]:
    if isinstance(confirmed_paths, (str, Path)):
        confirmed_paths = [confirmed_paths]
    ordered = []
    seen = set()
    for item in confirmed_paths or ():
        if isinstance(item, (tuple, list)):
            if not item:
                continue
            item = item[0]
        text = str(item or "")
        if not text or text in seen:
            continue
        seen.add(text)
        ordered.append(text)
    return ordered


def _fold_text(value):
    """Case-insensitive form for text the database's LIKE cannot fold.

    LIKE folds ASCII letters only, so "Élan" never matched a search for "élan".
    """
    return value.casefold() if isinstance(value, str) else value


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
        self._settings_write_lock = threading.Lock()
        self._settings_pending = {}
        self._settings_revision = 0
        self._settings_generation = 0

    def add_show(
        self,
        feed_url: str,
        title: str = "",
        source: str = "rss",
        *,
        playback_speed: float | None = None,
        skip_back: int | None = None,
        skip_forward: int | None = None,
        auto_continue: bool | None = None,
        website_url: str = "",
    ) -> Show:
        settings = (playback_speed, skip_back, skip_forward, auto_continue)
        if any(value is not None for value in settings) and any(value is None for value in settings):
            raise ValueError("playback_speed, skip_back, skip_forward and auto_continue go together.")
        now = time.time()
        with self.database.connect() as connection:
            connection.execute(
                "INSERT OR IGNORE INTO shows(id, feed_url, title, source, added_at, website_url) "
                "VALUES ((SELECT value+1 FROM entity_sequences WHERE kind=\'show\'), ?, ?, ?, ?, ?)",
                (feed_url, title, source, now, website_url),
            )
            inserted = connection.execute("SELECT changes()").fetchone()[0]
            row = connection.execute(
                "SELECT id FROM shows WHERE feed_url=?", (feed_url,)
            ).fetchone()
            if inserted and playback_speed is not None:
                connection.execute(
                    "UPDATE shows SET playback_speed=?, skip_back=?, skip_forward=?, auto_continue=? WHERE id=?",
                    (
                        float(playback_speed),
                        int(skip_back),
                        int(skip_forward),
                        int(bool(auto_continue)),
                        row["id"],
                    ),
                )
        return self.get_show(row["id"])

    def get_show(self, show_id: int) -> Show | None:
        with self.database.connect() as connection:
            row = connection.execute(
                self._show_select() + " WHERE s.id=?",
                (show_id,),
            ).fetchone()
        return self._show(row) if row else None

    def list_shows(self, include_suspended: bool = True) -> list[Show]:
        where = "" if include_suspended else " WHERE s.suspended=0"
        with self.database.connect() as connection:
            rows = connection.execute(
                self._show_select() + where + " GROUP BY s.id ORDER BY s.title COLLATE NOCASE"
            ).fetchall()
        return [self._show(row) for row in rows]

    def list_episodes(
        self, show_id: int | None = None, limit: int | None = 500, offset: int = 0
    ) -> list[Episode]:
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
            if offset:
                # OFFSET is only meaningful under a LIMIT (limit=None already
                # reads everything), and SQLite rejects a bare OFFSET anyway.
                sql += " OFFSET ?"
                params.append(offset)
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

    def list_history(self, limit: int = 200, offset: int = 0) -> list[Episode]:
        with self.database.connect() as connection:
            rows = connection.execute(
                """SELECT e.*, s.title AS show_title, COALESCE(NULLIF(e.episode_artwork_path, ''), s.artwork_path) AS artwork_path FROM episodes e
                   JOIN shows s ON s.id=e.show_id
                   WHERE e.last_played IS NOT NULL
                   ORDER BY e.last_played DESC LIMIT ? OFFSET ?""",
                (limit, offset),
            ).fetchall()
        return [self._episode(row) for row in rows]

    def history_count(self) -> int:
        """Total played rows, so the History page can say how much it shows."""
        with self.database.connect() as connection:
            row = connection.execute(
                "SELECT COUNT(*) FROM episodes WHERE last_played IS NOT NULL"
            ).fetchone()
        return int(row[0])

    def list_favorites(self) -> list[Episode]:
        with self.database.connect() as connection:
            rows = connection.execute(
                """SELECT e.*, s.title AS show_title,
                   COALESCE(NULLIF(e.episode_artwork_path, ''), s.artwork_path) AS artwork_path
                   FROM episodes e JOIN shows s ON s.id=e.show_id
                   WHERE e.favorite=1 ORDER BY e.published_at DESC, e.id DESC"""
            ).fetchall()
        return [self._episode(row) for row in rows]

    def list_in_progress(self) -> list[Episode]:
        with self.database.connect() as connection:
            rows = connection.execute(
                "SELECT e.*, s.title AS show_title, "
                "COALESCE(NULLIF(e.episode_artwork_path, ''), s.artwork_path) AS artwork_path "
                "FROM episodes e JOIN shows s ON s.id=e.show_id "
                "WHERE e.played=0 AND e.position_seconds>0 "
                "ORDER BY e.last_played DESC, e.id DESC").fetchall()
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
                   AND EXISTS (SELECT 1 FROM downloads d WHERE d.episode_id=e.id AND d.state='complete' AND d.length_verified=1)
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
                    from datetime import datetime, timezone
                    parsed = datetime.fromisoformat(
                        episode.published_at.replace("Z", "+00:00")
                    )
                    if parsed.tzinfo is None:
                        # The parser stores dates normalized to UTC and then
                        # naive; .timestamp() on a naive datetime assumes
                        # LOCAL time, which skewed "delete after N days" by
                        # the machine's whole UTC offset.
                        parsed = parsed.replace(tzinfo=timezone.utc)
                    older = parsed.timestamp() < cutoff
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
            # must not be materialized (descriptions included) per refresh. The
            # descriptions are compared inside the query against the incoming
            # text, so only a yes/no per episode comes back.
            initial_import = connection.execute(
                "SELECT 1 FROM episodes WHERE show_id=? LIMIT 1", (show_id,)
            ).fetchone() is None
            select = (
                "WITH incoming(slot, external_id, description) AS (VALUES {}) SELECT i.slot, e.external_id, "
                + ", ".join(
                    "e.description IS i.description AS description_same" if column == "description" else "e." + column
                    for column in _EPISODE_SYNC_COLUMNS)
                + " FROM incoming i JOIN episodes e ON e.show_id=? AND e.external_id=i.external_id"
            )
            existing = {}
            existing_ids = set()
            for start in range(0, len(feed.episodes), 100):
                chunk = feed.episodes[start:start + 100]
                values = []
                for slot, episode in enumerate(chunk, start):
                    values += (slot, episode.external_id, episode.description)
                for row in connection.execute(
                    select.format(",".join(["(?,?,?)"] * len(chunk))), [*values, show_id]
                ).fetchall():
                    existing_ids.add(row["external_id"])
                    existing[row["slot"]] = tuple(
                        bool(row["description_same"]) if column == "description" else row[column]
                        for column in _EPISODE_SYNC_COLUMNS)
            imported = sum(episode.external_id not in existing_ids for episode in feed.episodes)
            new_flag = 0 if initial_import else 1
            for slot, episode in enumerate(feed.episodes):
                incoming = tuple(
                    True if column == "description" else
                    (None if episode.explicit is None else int(episode.explicit))
                    if column == "explicit" else getattr(episode, column)
                    for column in _EPISODE_SYNC_COLUMNS
                )
                if existing.get(slot) == incoming:
                    continue
                connection.execute(
                    """INSERT INTO episodes(
                       id, show_id, external_id, title, description, media_url,
                       mime_type, published_at, duration_seconds,
                       transcript_url, transcript_type, is_new, added_at,
                       chapters_url, artwork_url, website_url, author,
                       season_number, episode_number, episode_type, explicit,
                       enclosure_bytes)
                       VALUES ((SELECT value+1 FROM entity_sequences WHERE kind='episode'), ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
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
                       episode_artwork_path=CASE WHEN episodes.artwork_url!=excluded.artwork_url THEN '' ELSE episodes.episode_artwork_path END,
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
        """Fetch episodes in bounded batches for download/bookmark views."""
        ids = list(dict.fromkeys(int(value) for value in episode_ids if value))
        if not ids:
            return {}
        rows = []
        with self.database.connect() as connection:
            for start in range(0, len(ids), 500):
                batch = ids[start:start + 500]
                placeholders = ",".join("?" for _ in batch)
                rows.extend(connection.execute(
                    f"""SELECT e.*, s.title AS show_title,
                        COALESCE(NULLIF(e.episode_artwork_path, ''), s.artwork_path) AS artwork_path
                        FROM episodes e JOIN shows s ON s.id=e.show_id
                        WHERE e.id IN ({placeholders})""", batch).fetchall())
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

    def artwork_paths(self) -> set[str]:
        """Every artwork file the library still references."""
        with self.database.connect() as connection:
            rows = connection.execute(
                "SELECT artwork_path FROM shows WHERE artwork_path != '' "
                "UNION SELECT episode_artwork_path FROM episodes WHERE episode_artwork_path != ''"
            ).fetchall()
        return {row[0] for row in rows}

    def removal_preview(self, show_id: int, *, inspect_files: bool = True) -> dict:
        """Exact targets that removing a show would delete; nothing is touched.

        A download that cannot be checked (its folder is offline or gone, or
        no longer carries the root marker, or its path is not a regular file)
        never blocks the preview. It is listed under ``unchecked`` with its
        folders, kept out of ``files`` so it is never offered for deletion, and
        left where it is. ``inspect_files=False`` skips the file pass for a
        removal that keeps every file.
        """
        show = self.get_show(show_id)
        if show is None:
            return {}
        with self.database.connect() as connection:
            episode_count = connection.execute("SELECT COUNT(*) FROM episodes WHERE show_id=?", (show_id,)).fetchone()[0]
            rows = connection.execute(
                "SELECT d.target_path, d.partial_path, d.state FROM downloads d "
                "JOIN episodes e ON e.id=d.episode_id WHERE e.show_id=?",
                (show_id,),
            ).fetchall() if inspect_files else ()
            queued = connection.execute(
                "SELECT COUNT(*) FROM queue q JOIN episodes e ON e.id=q.episode_id WHERE e.show_id=?", (show_id,)
            ).fetchone()[0]
            bookmarks = connection.execute(
                "SELECT COUNT(*) FROM bookmarks b JOIN episodes e ON e.id=b.episode_id WHERE e.show_id=?", (show_id,)
            ).fetchone()[0]
        completed_by_path = {}
        ordered_paths = []
        for row in rows:
            row_complete = row["state"] == "complete"
            for candidate in (row["target_path"], row["partial_path"]):
                if not candidate:
                    continue
                is_completed = row_complete and candidate == row["target_path"]
                if candidate not in completed_by_path:
                    ordered_paths.append(candidate)
                    completed_by_path[candidate] = is_completed
                elif is_completed:
                    completed_by_path[candidate] = True
        candidates = [(path, completed_by_path[path]) for path in ordered_paths]
        roots = set()
        checked = []
        for candidate, completed in candidates:
            kind, size = inspect_download_file(candidate, roots, completed=completed)
            checked.append((candidate, completed, kind, size))
        files = []
        unchecked = []
        for candidate, completed, kind, size in checked:
            if kind == FILE_UNCONFIRMED:
                kind, size = inspect_download_file(candidate, roots, completed=False)
            if kind == FILE_PRESENT:
                files.append((candidate, size))
            elif kind == FILE_MISSING:
                continue
            elif kind == FILE_UNCONFIRMED and not completed:
                continue
            else:
                unchecked.append(candidate)
        # Artwork is shared cache data, not owned by this subscription. Leave
        # it to cache maintenance; a refresh may be about to attach a reference.
        return {
            "show": show,
            "episodes": episode_count,
            "queued": queued,
            "bookmarks": bookmarks,
            "files": files,
            "bytes": sum(size for _path, size in files),
            "unchecked": unchecked,
            "unchecked_folders": list(dict.fromkeys(str(Path(path).parent) for path in unchecked)),
        }

    ORPHAN_SETTING = "storage.orphans"
    ORPHAN_OWNER = "storage.orphans.owner"
    MAX_ORPHANS_PER_SWEEP = 16

    def orphaned_files(self) -> list[str]:
        """Paths a previous removal could not unlink."""
        try:
            stored = json.loads(self.get_setting(self.ORPHAN_SETTING, "") or "[]")
        except ValueError:
            return []
        if not isinstance(stored, list):
            return []
        return [path for path in stored if isinstance(path, str)]

    def orphan_previews(self):
        """Read exact retry targets only after this profile's ownership check."""
        from .downloads import DeletionPreview
        with self.database.connect() as connection:
            values = dict(connection.execute("SELECT key,value FROM settings WHERE key IN (?,?)",
                                             (self.ORPHAN_OWNER, self.ORPHAN_SETTING)))
        if values.get(self.ORPHAN_OWNER) != str(self.database.path.resolve()):
            return []  # Never stat paths belonging to a copied/legacy profile.
        try:
            stored = json.loads(values.get(self.ORPHAN_SETTING, "[]"))
        except (ValueError, TypeError):
            return []
        if not isinstance(stored, list):
            return []
        paths = list(dict.fromkeys(path for path in stored if isinstance(path, str) and path))
        previews = []
        for path in paths:
            kind, size = inspect_download_file(path)
            if kind in {FILE_PRESENT, FILE_MISSING}:
                previews.append(DeletionPreview(0, path, size))
        return previews

    def _remember_orphans(self, paths):
        with self.database.connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            row = connection.execute("SELECT value FROM settings WHERE key=?", (self.ORPHAN_SETTING,)).fetchone()
            try:
                existing = json.loads(row[0]) if row else []
            except (ValueError, TypeError):
                existing = []
            if not isinstance(existing, list):
                existing = []
            owner = connection.execute("SELECT value FROM settings WHERE key=?", (self.ORPHAN_OWNER,)).fetchone()
            current_owner = str(self.database.path.resolve())
            if existing and (owner is None or owner[0] != current_owner):
                # A copied/legacy database cannot authorize deletion at its old
                # absolute paths. Preserve those records for manual inspection.
                retired_row = connection.execute("SELECT value FROM settings WHERE key=?",
                                                 (self.ORPHAN_SETTING + ".retired",)).fetchone()
                try:
                    retired = json.loads(retired_row[0]) if retired_row else []
                except (ValueError, TypeError):
                    retired = []
                retired = retired if isinstance(retired, list) else []
                retained = list(dict.fromkeys(p for p in [*retired, *existing] if isinstance(p, str)))
                connection.execute("INSERT INTO settings(key,value) VALUES (?,?) "
                                   "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
                                   (self.ORPHAN_SETTING + ".retired", json.dumps(retained)))
                existing = []
            merged = list(dict.fromkeys([p for p in existing if isinstance(p, str)] + list(paths)))
            connection.execute("INSERT INTO settings(key,value) VALUES (?,?) "
                               "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
                               (self.ORPHAN_SETTING, json.dumps(merged)))
            connection.execute("INSERT INTO settings(key,value) VALUES (?,?) "
                               "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
                               (self.ORPHAN_OWNER, current_owner))
        self.invalidate_settings_cache()
        self.reload_settings()

    def sweep_orphans(self, *, confirmed_paths) -> list[str]:
        """Retry a bounded batch while protecting concurrent live references.

        The batch and the live-reference check are decided in one write
        transaction that is committed before any file is touched: checking and
        unlinking can stall on a slow or unplugged drive, and every other
        writer would wait on SQLite's writer lock for as long. A second short
        transaction then settles the records. Limiting each pass bounds the
        filesystem work without discarding retry records.
        """
        confirmed = set(_confirmed_path_list(confirmed_paths))
        cleared = []
        with self.database.connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            owner = connection.execute("SELECT value FROM settings WHERE key=?", (self.ORPHAN_OWNER,)).fetchone()
            if owner is None or owner[0] != str(self.database.path.resolve()):
                # Fail closed for old libraries and copies made outside the
                # migration tool too. Merely opening one must never erase its source.
                return []
            row = connection.execute("SELECT value FROM settings WHERE key=?", (self.ORPHAN_SETTING,)).fetchone()
            try:
                paths = json.loads(row[0]) if row else []
            except (ValueError, TypeError):
                paths = []
            if not isinstance(paths, list):
                paths = []
            paths = [path for path in paths if isinstance(path, str)]
            pending = [path for path in paths if path in confirmed][:self.MAX_ORPHANS_PER_SWEEP]
            if not pending:
                return []
            placeholders = ",".join("?" for _ in pending)
            referenced = {row[0] for row in connection.execute(
                f"SELECT artwork_path FROM shows WHERE artwork_path IN ({placeholders}) "
                f"UNION SELECT episode_artwork_path FROM episodes WHERE episode_artwork_path IN ({placeholders}) "
                f"UNION SELECT downloaded_path FROM episodes WHERE downloaded_path IN ({placeholders}) "
                f"UNION SELECT target_path FROM downloads WHERE target_path IN ({placeholders}) "
                f"UNION SELECT partial_path FROM downloads WHERE partial_path IN ({placeholders})",
                pending * 5) if row[0]}
        failed_paths = []
        for path in pending:
            if not isinstance(path, str):
                continue
            if path in referenced:
                cleared.append(path)  # tracking retired; the live file stays
                continue
            kind, _size = inspect_download_file(path, completed=False)
            candidate = Path(path)
            if kind == FILE_PRESENT:
                try:
                    candidate.unlink()
                except OSError:
                    failed_paths.append(path)
                else:
                    cleared.append(path)
            elif kind in {FILE_MISSING, FILE_IRREGULAR}:
                cleared.append(path)
            else:
                failed_paths.append(path)
        if cleared or failed_paths:
            with self.database.connect() as connection:
                connection.execute("BEGIN IMMEDIATE")
                owner = connection.execute("SELECT value FROM settings WHERE key=?", (self.ORPHAN_OWNER,)).fetchone()
                row = connection.execute("SELECT value FROM settings WHERE key=?", (self.ORPHAN_SETTING,)).fetchone()
                if owner is not None and owner[0] == str(self.database.path.resolve()):
                    try:
                        stored = json.loads(row[0]) if row else []
                    except (ValueError, TypeError):
                        stored = []
                    if not isinstance(stored, list):
                        stored = []
                    # Records added while the files were being handled stay.
                    # Failed entries rotate behind untouched ones, so one
                    # persistently locked file cannot starve cleanup of every
                    # later record.
                    settled = set(pending)
                    remaining = [path for path in stored if isinstance(path, str) and path not in settled] + failed_paths
                    connection.execute("UPDATE settings SET value=? WHERE key=?",
                                       (json.dumps(remaining), self.ORPHAN_SETTING))
            self.invalidate_settings_cache()
            self.reload_settings()
        return cleared

    def remove_show(self, show_id: int, delete_files: bool = True, confirmed_paths=None) -> dict:
        """Delete a show and its cascaded rows.

        Files named by the caller go first. ``confirmed_paths`` is the list
        already shown to the user; None unlinks nothing. A path this removal
        itself retired may be included in that list. An absent file counts as
        removed only when the download root is confirmed. Survivors are
        recorded as orphans. Orphan retry is not part of this call.
        """
        preview = self.removal_preview(show_id, inspect_files=delete_files)
        if not preview:
            return {}
        if preview["unchecked"]:
            _log.warning("%d download path(s) could not be checked while unsubscribing and were left in place: %s",
                         len(preview["unchecked"]), ", ".join(preview["unchecked_folders"][:3]))
        removed_files = []
        failed_files = []
        if delete_files and confirmed_paths is not None:
            for path in _confirmed_path_list(confirmed_paths):
                try:
                    Path(path).unlink()
                except FileNotFoundError:
                    kind, _size = inspect_download_file(path, completed=False)
                    if kind == FILE_MISSING:
                        removed_files.append(path)
                    else:
                        failed_files.append(path)
                except OSError as exc:
                    failed_files.append(path)
                    # The file name and the reason only: the error text and the
                    # path both carry the whole local folder.
                    _log.warning("Could not remove %s while unsubscribing: %s",
                                 Path(path).name, exc.strerror or type(exc).__name__)
                else:
                    removed_files.append(path)
        retention_key = f"retention.confirmed.{show_id}"
        with self.database.connect() as connection:
            connection.execute("DELETE FROM shows WHERE id=?", (show_id,))
            connection.execute("DELETE FROM settings WHERE key=?", (retention_key,))
        # That raw DELETE bypasses the settings cache. Show IDs are never
        # reused (migration 017's entity_sequences), so this only drops the
        # retired show's entry from the mirror.
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

    def reconcile_interrupted_refreshes(self) -> int:
        """Settle "loading" health that no running refresh owns.

        Refresh stores it before fetching. A quit that has to cut a fetch
        short leaves it behind, and the podcast would show Refreshing until
        something refreshed it again.
        """
        with self.database.connect() as connection:
            return connection.execute(
                "UPDATE shows SET health=CASE WHEN suspended=1 THEN 'suspended' "
                "WHEN fail_count>0 THEN 'error' ELSE 'unknown' END WHERE health='loading'"
            ).rowcount

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
                """UPDATE shows SET health=?, fail_count=0, suspended=0, last_refresh_error='',
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

    @staticmethod
    def _refresh_error_text(message) -> str:
        # Bound publisher/native text before regex work and durable storage.
        text = redact(str(message or "")[:4096]).strip()
        return text.encode("utf-8", "replace")[:MAX_REFRESH_ERROR_BYTES].decode("utf-8", "ignore")

    def record_refresh_error(self, show_id: int, message: str) -> bool:
        """Remember a transient error without consuming the failure budget."""
        text = self._refresh_error_text(message)
        if not text:
            return False
        with self.database.connect() as connection:
            changed = connection.execute("UPDATE shows SET last_refresh_error=? WHERE id=?", (text,show_id)).rowcount
        return bool(changed)

    def record_refresh_failure(self, show_id: int, message: str = "") -> Health:
        text = self._refresh_error_text(message)
        with self.database.connect() as connection:
            row = connection.execute(
                "UPDATE shows SET fail_count=fail_count+1, health='error' "
                ",last_refresh_error=CASE WHEN ?!='' THEN ? ELSE last_refresh_error END "
                "WHERE id=? RETURNING fail_count",
                (text,text,show_id),
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

    def set_artwork_path(self, show_id: int, path: str, *, expected_url=None):
        with self.database.connect() as connection:
            if expected_url is None:
                cursor = connection.execute("UPDATE shows SET artwork_path=? WHERE id=?", (path, show_id))
            else:
                cursor = connection.execute("UPDATE shows SET artwork_path=? WHERE id=? AND artwork_url=?", (path, show_id, expected_url))
            return bool(cursor.rowcount)

    def set_setting(self, key: str, value: str):
        values = {key: value}
        self.persist_settings(values, self.stage_settings(values))

    def invalidate_settings_cache(self):
        """Retain the usable mirror until a worker publishes fresh settings."""
        with self._settings_lock:
            self._settings_generation += 1

    def reload_settings(self) -> bool:
        """Read on a worker without holding the lock used by GUI lookups."""
        with self._settings_write_lock:
            with self._settings_lock:
                generation = self._settings_generation
            with self.database.connect() as connection:
                values = {row["key"]: row["value"]
                          for row in connection.execute("SELECT key, value FROM settings").fetchall()}
            with self._settings_lock:
                if generation != self._settings_generation:
                    return False
                self._settings_cache = values
        return True

    def stage_settings(self, values):
        """Mirror accepted UI settings immediately, without doing disk I/O."""
        with self._settings_lock:
            self._settings_revision += 1
            revision = self._settings_revision
            self._settings_pending.update({key: (revision, value) for key, value in values.items()})
            return {key: revision for key in values}

    def persist_settings(self, values, revisions=None):
        with self._settings_write_lock:
            with self._settings_lock:
                if revisions is not None:
                    values = {key: value for key, value in values.items()
                              if self._settings_pending.get(key) == (revisions.get(key), value)}
            try:
                with self.database.connect() as connection:
                    connection.executemany(
                        "INSERT INTO settings(key,value) VALUES (?,?) ON CONFLICT(key) DO UPDATE SET value=excluded.value",
                        values.items())
            except BaseException:
                with self._settings_lock:
                    for key, value in values.items():
                        if revisions is not None and self._settings_pending.get(key) == (revisions.get(key), value):
                            self._settings_pending.pop(key, None)
                raise
            with self._settings_lock:
                if self._settings_cache is not None:
                    self._settings_cache.update(values)
                for key, value in values.items():
                    if revisions is not None and self._settings_pending.get(key) == (revisions.get(key), value):
                        self._settings_pending.pop(key, None)

    def get_setting(self, key: str, default: str = "") -> str:
        # Settings are read on hot paths (startup wiring, every scheduler tick);
        # one read of the tiny table replaces a connection round-trip per call.
        with self._settings_lock:
            if key in self._settings_pending:
                return self._settings_pending[key][1]
            if self._settings_cache is not None:
                return self._settings_cache.get(key, default)
        # The first load precedes constructing the window. Later invalidations
        # retain this snapshot and are explicitly refreshed on a worker.
        self.reload_settings()
        with self._settings_lock:
            if key in self._settings_pending:
                return self._settings_pending[key][1]
            return (self._settings_cache or {}).get(key, default)

    def enqueue(self, episode_id: int) -> bool:
        with self.database.connect() as connection:
            # Position computed inside the INSERT: a separate SELECT let two
            # connections claim the same position.
            cursor = connection.execute(
                "INSERT OR IGNORE INTO queue(episode_id, position, added_at) "
                "SELECT ?, COALESCE(MAX(position), 0) + 1, ? FROM queue",
                (episode_id, time.time()),
            )
            return cursor.rowcount > 0

    def list_queue(self) -> list[Episode]:
        with self.database.connect() as connection:
            rows = connection.execute(
                """SELECT e.*, s.title AS show_title, COALESCE(NULLIF(e.episode_artwork_path, ''), s.artwork_path) AS artwork_path FROM queue q
                   JOIN episodes e ON e.id=q.episode_id
                   JOIN shows s ON s.id=e.show_id ORDER BY q.position"""
            ).fetchall()
        return [self._episode(row) for row in rows]

    def dequeue(self, episode_id: int):
        self.dequeue_many([episode_id])

    def dequeue_many(self, episode_ids) -> int:
        """Remove any number of episodes from the queue in one transaction.

        The rows go in batches of 500 ids and the rows left are renumbered
        once, writing only the positions that moved. One call per episode
        committed and renumbered the whole queue each time. Returns how many
        rows were removed."""
        ids = list(dict.fromkeys(int(episode_id) for episode_id in episode_ids))
        if not ids:
            return 0
        removed = 0
        with self.database.connect() as connection:
            for start in range(0, len(ids), 500):
                batch = ids[start:start + 500]
                removed += connection.execute(
                    "DELETE FROM queue WHERE episode_id IN (" + ",".join("?" for _ in batch) + ")", batch
                ).rowcount
            rows = connection.execute("SELECT id, position FROM queue ORDER BY position, id").fetchall()
            connection.executemany(
                "UPDATE queue SET position=? WHERE id=?",
                [(position, row["id"]) for position, row in enumerate(rows, start=1) if row["position"] != position],
            )
        return removed

    def reorder_queue(self, episode_ids: list[int]):
        """Arrange the named queued episodes in the given order, in place.

        The Up Next page passes the whole queue. A shorter list is not a
        move: the named rows swap among the positions they already hold and
        every other row keeps its position. Ids that are not queued are
        ignored, so a list that went stale while a row was added or removed
        still applies cleanly."""
        with self.database.connect() as connection:
            # The order is read inside the write, so a removal or an add that
            # commits meanwhile cannot leave a duplicate or a gap.
            connection.execute("BEGIN IMMEDIATE")
            existing = [
                row["episode_id"]
                for row in connection.execute("SELECT episode_id FROM queue ORDER BY position").fetchall()
            ]
            requested = list(dict.fromkeys(episode_id for episode_id in episode_ids if episode_id in existing))
            visible = set(requested)
            reordered = iter(requested)
            requested = [next(reordered) if episode_id in visible else episode_id for episode_id in existing]
            for position, episode_id in enumerate(requested, start=1):
                connection.execute(
                    "UPDATE queue SET position=? WHERE episode_id=?",
                    (position, episode_id),
                )

    def update_position(self, episode_id: int, seconds: float, stamp: float | None = None, *, finish: bool = False):
        """Save a playback position ordered by `stamp`.

        True: written. False: maintenance holds the database, so the playback
        writer keeps the save and retries. None: nothing was written because
        the episode is gone or a newer write already stands. A finished
        episode lands its end position even past a newer stamp, so
        mark_played does not run ahead of that write.
        """
        if self.database.maintenance_active:
            return False
        stamp = time.time() if stamp is None else stamp
        seconds = max(0.0, float(seconds))
        with self.database.connect() as connection:
            if self._write_position(connection, episode_id, seconds, stamp):
                return True
            if not finish:
                return None
            row = connection.execute(
                "SELECT position_updated_at FROM episodes WHERE id=?",
                (episode_id,),
            ).fetchone()
            if row is None:
                return None
            stored = float(row["position_updated_at"] or 0.0)
            if stored < stamp:
                return None
            return self._write_position(connection, episode_id, seconds, stored + 0.000001) or None

    @staticmethod
    def _write_position(connection, episode_id: int, seconds: float, stamp: float) -> bool:
        # Starting an episode is what makes it "not new" everywhere in the UI.
        # History shows real time; the ordering stamp can run ahead of it.
        cursor = connection.execute(
            "UPDATE episodes SET position_seconds=?, last_played=?, is_new=0, position_updated_at=? "
            "WHERE id=? AND position_updated_at<=?",
            (seconds, time.time(), stamp, episode_id, stamp),
        )
        return cursor.rowcount > 0

    def write_stamp_floor(self, episode_id: int | None = None) -> float:
        """Newest stored write-order stamp of the playback row and one episode.

        Stamps are wall-clock values kept across runs. A run must issue
        stamps above them, or after the clock moves back every save to those
        rows matches nothing until real time catches up.
        """
        with self.database.connect() as connection:
            row = connection.execute(
                "SELECT MAX(COALESCE((SELECT position_updated_at FROM episodes WHERE id=?), 0), "
                "COALESCE((SELECT updated_at FROM playback_state WHERE singleton_id=1), 0))",
                (episode_id,),
            ).fetchone()
        return float(row[0] or 0.0)

    def set_observed_duration(self, episode_id: int, seconds: float):
        import math
        if not math.isfinite(seconds) or not 0 < seconds <= 2**31:
            return
        with self.database.connect() as connection:
            connection.execute(
                "UPDATE episodes SET observed_duration_seconds=? WHERE id=?",
                (max(1, round(seconds)), episode_id))

    def set_episode_artwork_path(self, episode_id: int, path: str, expected_url: str | None = None):
        with self.database.connect() as connection:
            if expected_url is None:
                return connection.execute("UPDATE episodes SET episode_artwork_path=? WHERE id=?", (path, episode_id)).rowcount
            return connection.execute("UPDATE episodes SET episode_artwork_path=? WHERE id=? AND artwork_url=?",
                                      (path, episode_id, expected_url)).rowcount

    def set_favorite(self, episode_id: int, favorite: bool = True) -> bool:
        with self.database.connect() as connection:
            changed = connection.execute(
                "UPDATE episodes SET favorite=? WHERE id=?",
                (int(favorite), episode_id),
            ).rowcount
        return bool(changed)

    def set_favorites(self, episode_ids, favorite):
        with self.database.connect() as connection:
            return connection.executemany('UPDATE episodes SET favorite=? WHERE id=?',
                ((int(favorite), value) for value in dict.fromkeys(episode_ids))).rowcount

    def clear_new_with_undo(self) -> list:
        """Capture and clear exactly this transaction's unread rows."""
        with self.database.connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            rows = connection.execute("SELECT id,show_id,external_id,played,is_new,position_seconds,last_played "
                                      "FROM episodes WHERE is_new=1").fetchall()
            connection.execute("UPDATE episodes SET is_new=0 WHERE is_new=1")
        return [(tuple(row), (row["played"], False, row["position_seconds"], row["last_played"])) for row in rows]

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
                "UPDATE episodes SET position_updated_at=MAX(unixepoch('subsec'), position_updated_at+0.000001), "
                "position_seconds=CASE WHEN ?=0 AND duration_seconds>0 AND position_seconds>=duration_seconds-2 "
                "THEN 0 ELSE position_seconds END, played=?, is_new=0 WHERE show_id=? AND played!=?",
                (int(played), int(played), show_id, int(played)),
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

    _PLAYED_FIELDS = ("played", "is_new", "position_seconds", "last_played")

    def progress_targets(self, episode_ids, exclude_episode_id=None):
        """Snapshot exact saved positions for confirmation, in bounded queries."""
        ids = list(dict.fromkeys(int(value) for value in episode_ids
                                 if int(value) > 0 and int(value) != exclude_episode_id))
        targets = []
        with self.database.connect() as connection:
            for start in range(0, len(ids), 500):
                chunk = ids[start:start + 500]
                placeholders = ','.join('?' for _ in chunk)
                targets.extend(tuple(row) for row in connection.execute(
                    f'SELECT id,position_seconds,position_updated_at FROM episodes '
                    f'WHERE id IN ({placeholders}) AND position_seconds>0 ORDER BY id', chunk))
        return targets

    def clear_progress(self, targets, exclude_episode_id=None):
        """Reset only confirmed, unchanged positions; retain flags and history."""
        count = 0
        with self.database.connect() as connection:
            for episode_id, position, stamp in targets:
                if episode_id == exclude_episode_id:
                    continue
                count += connection.execute(
                    "UPDATE episodes SET position_seconds=0, "
                    "position_updated_at=MAX(unixepoch('subsec'),position_updated_at+0.000001) "
                    "WHERE id=? AND position_seconds>0 AND position_seconds IS ? AND position_updated_at IS ?",
                    (episode_id, position, stamp)).rowcount
        return count

    def new_episode_ids(self):
        with self.database.connect() as connection:
            return [row[0] for row in connection.execute("SELECT id FROM episodes WHERE is_new=1")]

    def mark_played_with_undo(self, episode_ids, played: bool = True, show_id=None):
        """Change and snapshot in one transaction; Undo never guesses old state."""
        with self.database.connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            played_flag = int(bool(played))
            if show_id is not None:
                ids = [row[0] for row in connection.execute(
                    "SELECT id FROM episodes WHERE show_id=? AND played!=?", (show_id, played_flag))]
            else:
                ids = list(dict.fromkeys(int(i) for i in episode_ids))
            changes = []
            for episode_id in ids:
                before = connection.execute(
                    "SELECT id, show_id, external_id, played, is_new, position_seconds, last_played "
                    "FROM episodes WHERE id=?", (episode_id,)).fetchone()
                if before is None or int(before["played"]) == played_flag:
                    continue
                if played:
                    connection.execute(
                        "UPDATE episodes SET position_updated_at=MAX(unixepoch('subsec'), position_updated_at+0.000001), played=1, is_new=0 WHERE id=?",
                        (episode_id,))
                else:
                    connection.execute(
                        "UPDATE episodes SET position_updated_at=MAX(unixepoch('subsec'), position_updated_at+0.000001), played=0, is_new=0, position_seconds=CASE "
                        "WHEN COALESCE(NULLIF(observed_duration_seconds,0),duration_seconds)>0 AND position_seconds>=COALESCE(NULLIF(observed_duration_seconds,0),duration_seconds)-2 "
                        "THEN 0 ELSE position_seconds END WHERE id=?", (episode_id,))
                after = connection.execute(
                    "SELECT played, is_new, position_seconds, last_played FROM episodes WHERE id=?",
                    (episode_id,)).fetchone()
                changes.append((tuple(before), tuple(after)))
        return changes

    def undo_played(self, changes) -> int:
        """Restore only rows still in the state this operation wrote."""
        restored = 0
        with self.database.connect() as connection:
            for before, after in changes:
                identity, values = before[:3], before[3:]
                restored += connection.execute(
                    "UPDATE episodes SET position_updated_at=MAX(unixepoch('subsec'), position_updated_at+0.000001), played=?, is_new=?, position_seconds=?, last_played=? "
                    "WHERE id=? AND show_id=? AND external_id=? "
                    "AND played IS ? AND is_new IS ? AND position_seconds IS ? AND last_played IS ?",
                    (*values, *identity, *after)).rowcount
        return restored

    def enqueue_many(self, episode_ids) -> int:
        """Append many episodes to the queue in one transaction, in order."""
        ids = [int(i) for i in episode_ids]
        if not ids:
            return 0
        now = time.time()
        with self.database.connect() as connection:
            for episode_id in ids:
                connection.execute(
                    "INSERT OR IGNORE INTO queue(episode_id, position, added_at) "
                    "SELECT ?, COALESCE(MAX(position), 0) + 1, ? FROM queue",
                    (episode_id, now),
                )
        return len(ids)

    def mark_played(self, episode_id: int, played: bool = True):
        with self.database.connect() as connection:
            if played:
                connection.execute(
                    "UPDATE episodes SET position_updated_at=MAX(unixepoch('subsec'), position_updated_at+0.000001), played=1, is_new=0, last_played=? WHERE id=?",
                    (time.time(), episode_id),
                )
            else:
                # A finished episode's saved position is its duration. Leaving
                # that behind when marking it unplayed hands the next Play a
                # position that immediately re-reaches EOF. Genuine partial
                # progress is preserved — only an at-the-end position resets.
                connection.execute(
                    """UPDATE episodes SET position_updated_at=MAX(unixepoch('subsec'), position_updated_at+0.000001), played=0, is_new=0,
                       position_seconds = CASE
                           WHEN COALESCE(NULLIF(observed_duration_seconds,0),duration_seconds) > 0
                                AND position_seconds >= COALESCE(NULLIF(observed_duration_seconds,0),duration_seconds) - 2
                           THEN 0 ELSE position_seconds END
                       WHERE id=?""",
                    (episode_id,),
                )

    def set_current_playback(self, episode_id: int | None, state: str, stamp: float | None = None):
        """Same results as update_position: True, False (retry) or None."""
        if self.database.maintenance_active:
            return False
        stamp = time.time() if stamp is None else stamp
        with self.database.connect() as connection:
            cursor = connection.execute(
                """UPDATE playback_state SET episode_id=?, state=?, updated_at=?
                   WHERE singleton_id=1 AND updated_at<=?""",
                (episode_id, state, stamp, stamp),
            )
        return True if cursor.rowcount > 0 else None

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
        values = {key: value for key, value in {
            "playback_speed": speed, "skip_back": skip_back,
            "skip_forward": skip_forward, "trim_level": trim_level,
            "auto_continue": None if auto_continue is None else int(auto_continue),
        }.items() if value is not None}
        for key, value in {
            "auto_download_override": auto_download_override,
            "auto_download_limit": auto_download_limit,
            "retention_keep": retention_keep, "retention_days": retention_days,
        }.items():
            if value is not ...:
                values[key] = int(value) if key == "auto_download_override" and value is not None else value
        if not values:
            return
        with self.database.connect() as connection:
            connection.execute(
                "UPDATE shows SET " + ", ".join(key + "=?" for key in values) + " WHERE id=?",
                (*values.values(), show_id),
            )

    @staticmethod
    def _search_pattern(query):
        escaped = query.strip().replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
        return f"%{escaped}%"

    @staticmethod
    def _case_variants(text):
        """Spellings of a short ``text`` that differ only in the case of its
        non-ASCII letters; LIKE already folds the ASCII ones."""
        variants = {""}
        for char in text:
            options = {char} if char.isascii() else {
                char, char.lower(), char.upper(), char.title(), char.casefold()}
            variants = {prefix + option for prefix in variants for option in options}
        return sorted(variants)

    @classmethod
    def _match_terms(cls, connection, columns, text):
        """SQL and parameters matching ``text`` inside any of ``columns``.

        LIKE folds ASCII case only, so a non-ASCII search must also match a
        capital accented letter typed in lowercase, and the reverse. ASCII and
        short text stays on LIKE, trying each case spelling of its non-ASCII
        letters, so the scan never calls back into Python per stored
        description. Longer non-ASCII text compares case-folded values (the
        index has already narrowed those rows).
        """
        if text.isascii() or len(text) < 3:
            patterns = tuple(cls._search_pattern(variant) for variant in cls._case_variants(text))
            return ("(" + " OR ".join(column + " LIKE ? ESCAPE '\\'" for column in columns for _ in patterns) + ")",
                    patterns * len(columns))
        try:
            connection.create_function("fold_text", 1, _fold_text, deterministic=True)
        except sqlite3.OperationalError:
            pass  # already registered on this handle while a statement was open
        return ("(" + " OR ".join(f"instr(fold_text({column}), ?) > 0" for column in columns) + ")",
                (_fold_text(text),) * len(columns))

    @classmethod
    def _episode_search_predicate(cls, connection, query):
        # Trigram MATCH only narrows candidates (it folds case for any
        # script); the title/description comparison remains the final
        # authority. Strings under three characters have no trigram to look up.
        text = query.strip()
        indexed = bool(len(text) >= 3 and "\0" not in text and
                       connection.execute("SELECT 1 FROM sqlite_master WHERE name='episode_search' AND type='table'").fetchone())
        candidate = "e.id IN (SELECT rowid FROM episode_search WHERE episode_search MATCH ?) AND " if indexed else ""
        parameters = ()
        if indexed:
            count = min(8, len(text) - 2)
            positions = [index * (len(text) - 3) // max(1, count - 1) for index in range(count)]
            # Raw slices: the tokenizer folds case itself, and lower() can
            # lengthen a letter, turning one trigram into a phrase that the
            # detail-less index rejects.
            trigrams = dict.fromkeys(text[position:position + 3] for position in positions)
            terms = ' AND '.join('"' + gram.replace('"', '""') + '"' for gram in trigrams)
            parameters = (terms,)
        match, match_parameters = cls._match_terms(connection, ("e.title", "e.description", "s.title"), text)
        return candidate + match, parameters + match_parameters

    def search(self, query: str, limit: int = 100) -> tuple[list[Show], list[Episode]]:
        with self.database.connect() as connection:
            show_match, show_parameters = self._match_terms(connection, ("s.title", "s.author"), query.strip())
            show_rows = connection.execute(
                self._show_select()
                + " WHERE " + show_match
                + " GROUP BY s.id ORDER BY s.title COLLATE NOCASE LIMIT ?",
                show_parameters + (limit,),
            ).fetchall()
            predicate, parameters = self._episode_search_predicate(connection, query)
            episode_rows = connection.execute(
                """SELECT e.*, s.title AS show_title, COALESCE(NULLIF(e.episode_artwork_path, ''), s.artwork_path) AS artwork_path FROM episodes e
                   JOIN shows s ON s.id=e.show_id WHERE """ + predicate + " ORDER BY e.published_at DESC,e.id DESC LIMIT ?",
                parameters + (limit,),
            ).fetchall()
        return (
            [self._show(row) for row in show_rows],
            [self._episode(row) for row in episode_rows],
        )

    def search_episodes(self, query: str, limit: int = 500, offset: int = 0) -> list[Episode]:
        """Page the actual matching corpus, not a newest-episodes fallback."""
        limit, offset = max(0, int(limit)), max(0, int(offset))
        if not limit:
            return []
        with self.database.connect() as connection:
            predicate, parameters = self._episode_search_predicate(connection, query)
            rows = connection.execute(
                "SELECT e.*,s.title AS show_title,COALESCE(NULLIF(e.episode_artwork_path,''),s.artwork_path) AS artwork_path "
                "FROM episodes e JOIN shows s ON s.id=e.show_id WHERE " + predicate
                + " ORDER BY e.published_at DESC,e.id DESC LIMIT ? OFFSET ?",
                parameters + (limit,offset)).fetchall()
        return [self._episode(row) for row in rows]

    def search_episode_count(self, query: str) -> int:
        with self.database.connect() as connection:
            predicate, parameters = self._episode_search_predicate(connection, query)
            return connection.execute("SELECT COUNT(*) FROM episodes e JOIN shows s ON s.id=e.show_id WHERE "
                                      + predicate, parameters).fetchone()[0]

    @staticmethod
    def _show_select() -> str:
        """Shows with their cached counters (migration 014).

        The counts and the latest episode used to be aggregated from every
        episode row on each call (85 ms / 280 MB of reads on a 175k-episode
        library per reload, audit F-051); triggers now keep them on the
        shows row.
        """
        return "SELECT s.* FROM shows s"

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
            last_refresh_error=row["last_refresh_error"],
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
            duration_seconds=(row["observed_duration_seconds"] if "observed_duration_seconds" in row.keys() else 0) or row["duration_seconds"],
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
            episode_artwork_path=row["episode_artwork_path"] if "episode_artwork_path" in row.keys() else "",
        )
