"""Advanced listening metadata: chapters, transcripts, bookmarks and totals.

The folder and saved-playlist tables are not read or written by any method.
No migration drops them, so rows an earlier build stored stay in the library."""

import math
import time

from ...domain import Bookmark, Chapter, TranscriptSegment
from ...domain.times import media_seconds, media_interval
from ..database import Database


def _chapter_from_row(row) -> Chapter:
    start_seconds, end_seconds = media_interval(row["start_seconds"], row["end_seconds"])
    return Chapter(
        id=row["id"], episode_id=row["episode_id"], index=row["chapter_index"],
        start_seconds=start_seconds, end_seconds=end_seconds,
        title=row["title"], artwork_url=row["artwork_url"],
    )


def _segment_from_row(row) -> TranscriptSegment:
    start_seconds, end_seconds = media_interval(row["start_seconds"], row["end_seconds"])
    return TranscriptSegment(
        id=row["id"], episode_id=row["episode_id"], index=row["segment_index"],
        text=row["text"], start_seconds=start_seconds, end_seconds=end_seconds,
    )


class ListeningRepository:
    def __init__(self, database: Database):
        self.database = database

    def replace_chapters(self, episode_id: int, chapters, *, source_url=None):
        with self.database.connect() as connection:
            if source_url is not None:
                connection.execute("BEGIN IMMEDIATE")
                row = connection.execute("SELECT chapters_url FROM episodes WHERE id=?", (episode_id,)).fetchone()
                if row is None or row[0] != source_url:
                    return False
            connection.execute("DELETE FROM chapters WHERE episode_id=?", (episode_id,))
            rows = []
            for index, chapter in enumerate(chapters):
                start, end = media_interval(chapter[0], chapter[1])
                if start is None:
                    continue
                rows.append((episode_id, index, start, end, chapter[2], chapter[3] if len(chapter) > 3 else ""))
            connection.executemany(
                """INSERT INTO chapters(
                   episode_id, chapter_index, start_seconds, end_seconds,
                   title, artwork_url) VALUES (?, ?, ?, ?, ?, ?)""", rows)

        return True

    def record_attempt(self, episode_id: int, kind: str, source_url: str):
        """Remember that `kind` ('chapters' or 'transcript') came back empty from `source_url`."""
        with self.database.connect() as connection:
            connection.execute(
                "INSERT OR REPLACE INTO listening_attempts(episode_id, kind, source_url, attempted_at) "
                "SELECT id, ?, ?, ? FROM episodes WHERE id=?",
                (kind, source_url, time.time(), episode_id),
            )

    def attempted_recently(self, episode_id: int, kind: str, source_url: str, max_age: float) -> bool:
        """True when the same source was tried, empty, within `max_age` seconds."""
        with self.database.connect() as connection:
            row = connection.execute(
                "SELECT source_url, attempted_at FROM listening_attempts WHERE episode_id=? AND kind=?",
                (episode_id, kind),
            ).fetchone()
        return bool(row) and row[0] == source_url and time.time() - row[1] < max_age

    def chapters(self, episode_id: int) -> list[Chapter]:
        with self.database.connect() as connection:
            rows = connection.execute(
                "SELECT * FROM chapters WHERE episode_id=? ORDER BY chapter_index",
                (episode_id,),
            ).fetchall()
        return [
            _chapter_from_row(row)
            for row in rows if media_seconds(row["start_seconds"]) is not None
        ]

    def replace_transcript(self, episode_id: int, segments, *, source_url=None, source_type=""):
        with self.database.connect() as connection:
            if source_url is not None:
                connection.execute("BEGIN IMMEDIATE")
                row = connection.execute("SELECT transcript_url, transcript_type FROM episodes WHERE id=?", (episode_id,)).fetchone()
                if row is None or tuple(row) != (source_url, source_type):
                    return False
            connection.execute(
                "DELETE FROM transcript_segments WHERE episode_id=?", (episode_id,)
            )
            rows = []
            for index, segment in enumerate(segments):
                start, end = media_interval(segment[0], segment[1])
                rows.append((episode_id, index, start, end, segment[2]))
            connection.executemany(
                """INSERT INTO transcript_segments(
                   episode_id, segment_index, start_seconds, end_seconds, text)
                   VALUES (?, ?, ?, ?, ?)""", rows)

        return True

    def transcript(self, episode_id: int, query: str = "") -> list[TranscriptSegment]:
        sql = "SELECT * FROM transcript_segments WHERE episode_id=?"
        params: list[object] = [episode_id]
        folded = ""
        if query.strip():
            if query.strip().isascii():
                sql += " AND text LIKE ? ESCAPE '\\'"
                escaped = query.strip().replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
                params.append(f"%{escaped}%")
            else:
                # LIKE folds ASCII case only; match this one episode's lines
                # case-insensitively here instead.
                folded = query.strip().casefold()
        sql += " ORDER BY segment_index"
        with self.database.connect() as connection:
            rows = connection.execute(sql, params).fetchall()
        if folded:
            rows = [row for row in rows if folded in row["text"].casefold()]
        return [_segment_from_row(row) for row in rows]

    def add_bookmark(self, episode_id: int, position: float, title: str = "") -> Bookmark:
        with self.database.connect() as connection:
            cursor = connection.execute(
                """INSERT INTO bookmarks(episode_id, position_seconds, title, created_at)
                   VALUES (?, ?, ?, ?)""",
                (episode_id, max(0.0, position), title, time.time()),
            )
            bookmark_id = cursor.lastrowid
        return self.bookmarks(bookmark_id=bookmark_id)[0]

    def restore_bookmarks(self, bookmarks) -> tuple[int, int]:
        """Put deleted bookmarks back; returns (restored, skipped).

        Each keeps its saved time, and its id when that id is still free (ids
        are reused once a row is gone). A bookmark whose episode no longer
        exists is skipped. One transaction: nothing is half restored.
        """
        chosen = list({item.id: item for item in bookmarks}.values())
        if not chosen:
            return 0, 0

        episode_ids = list(dict.fromkeys(int(item.episode_id) for item in chosen))
        bookmark_ids = [int(item.id) for item in chosen]
        restored_rows = []
        generated_rows = []
        with self.database.connect() as connection:
            # Hold the writer lock across the existence/collision snapshots and
            # inserts so concurrent bookmark edits cannot invalidate them.
            connection.execute("BEGIN IMMEDIATE")
            existing_episodes = set()
            existing_bookmarks = set()
            for start in range(0, len(episode_ids), 500):
                batch = episode_ids[start:start + 500]
                marks = ",".join("?" for _ in batch)
                existing_episodes.update(
                    row[0] for row in connection.execute(
                        f"SELECT id FROM episodes WHERE id IN ({marks})", batch
                    )
                )
            for start in range(0, len(bookmark_ids), 500):
                batch = bookmark_ids[start:start + 500]
                marks = ",".join("?" for _ in batch)
                existing_bookmarks.update(
                    row[0] for row in connection.execute(
                        f"SELECT id FROM bookmarks WHERE id IN ({marks})", batch
                    )
                )

            for bookmark in chosen:
                if bookmark.episode_id not in existing_episodes:
                    continue
                values = (
                    bookmark.episode_id,
                    max(0.0, bookmark.position_seconds),
                    bookmark.title,
                    bookmark.created_at,
                )
                if bookmark.id in existing_bookmarks:
                    generated_rows.append(values)
                else:
                    restored_rows.append((bookmark.id, *values))

            connection.executemany(
                """INSERT INTO bookmarks(id, episode_id, position_seconds, title, created_at)
                   VALUES (?, ?, ?, ?, ?)""",
                restored_rows,
            )
            # Reserve all still-free saved IDs first. A generated row cannot
            # consume an ID that a later item in this restore can retain.
            connection.executemany(
                """INSERT INTO bookmarks(id, episode_id, position_seconds, title, created_at)
                   VALUES (NULL, ?, ?, ?, ?)""",
                generated_rows,
            )
        restored = len(restored_rows) + len(generated_rows)
        return restored, len(chosen) - restored

    def delete_bookmark(self, bookmark_id: int):
        with self.database.connect() as connection:
            connection.execute("DELETE FROM bookmarks WHERE id=?", (bookmark_id,))

    def delete_bookmarks(self, bookmark_ids):
        with self.database.connect() as connection:
            return connection.executemany('DELETE FROM bookmarks WHERE id=?',
                ((value,) for value in dict.fromkeys(bookmark_ids))).rowcount

    def rename_bookmark(self, bookmark_id: int, title: str):
        with self.database.connect() as connection:
            connection.execute("UPDATE bookmarks SET title=? WHERE id=?", (title.strip(), bookmark_id))

    def bookmarks(self, episode_id: int | None = None, *, bookmark_id=None) -> list[Bookmark]:
        sql = (
            "SELECT b.*, e.title AS episode_title, s.title AS show_title "
            "FROM bookmarks b JOIN episodes e ON e.id=b.episode_id "
            "JOIN shows s ON s.id=e.show_id"
        )
        params = ()
        if bookmark_id is not None:
            sql += " WHERE b.id=?"
            params = (bookmark_id,)
        elif episode_id is not None:
            sql += " WHERE b.episode_id=?"
            params = (episode_id,)
        sql += " ORDER BY b.created_at DESC"
        with self.database.connect() as connection:
            rows = connection.execute(sql, params).fetchall()
        return [self._bookmark(row) for row in rows]

    def bookmarks_by_ids(self, bookmark_ids) -> list[Bookmark]:
        """Fetch bookmarks by ID, preserving deduplicated input order."""
        ids = list(dict.fromkeys(int(value) for value in bookmark_ids if value))
        if not ids:
            return []
        query = (
            "SELECT b.*, e.title AS episode_title, s.title AS show_title "
            "FROM bookmarks b JOIN episodes e ON e.id=b.episode_id "
            "JOIN shows s ON s.id=e.show_id WHERE b.id IN ({})"
        )
        found = {}
        with self.database.connect() as connection:
            for start in range(0, len(ids), 500):
                batch = ids[start:start + 500]
                sql = query.format(",".join("?" for _ in batch))
                found.update(
                    (bookmark.id, bookmark)
                    for bookmark in (self._bookmark(row) for row in connection.execute(sql, batch))
                )
        return [found[bookmark_id] for bookmark_id in ids if bookmark_id in found]

    @staticmethod
    def _bookmark(row) -> Bookmark:
        return Bookmark(
            id=row["id"],
            episode_id=row["episode_id"],
            position_seconds=row["position_seconds"],
            title=row["title"],
            created_at=row["created_at"],
            episode_title=row["episode_title"],
            show_title=row["show_title"],
        )

    def add_silence_saved(self, seconds: float):
        if not math.isfinite(seconds) or seconds <= 0:
            return
        with self.database.connect() as connection:
            connection.execute(
                "UPDATE playback_metrics SET silence_saved=silence_saved+? WHERE singleton_id=1",
                (float(seconds),),
            )

    def silence_saved(self) -> float:
        with self.database.connect() as connection:
            row = connection.execute(
                "SELECT silence_saved FROM playback_metrics WHERE singleton_id=1"
            ).fetchone()
        return float(row["silence_saved"] if row else 0)

    def add_listening(self, show_id: int, seconds: float):
        if not show_id or not math.isfinite(seconds) or seconds <= 0:
            return
        seconds = float(seconds)
        with self.database.connect() as connection:
            connection.execute(
                "UPDATE playback_metrics SET listened_seconds=listened_seconds+? WHERE singleton_id=1",
                (seconds,),
            )
            # The show may have been removed while its episode still played: the total above still counts,
            # the per-show row is skipped rather than failing the transaction on the foreign key.
            connection.execute(
                """INSERT INTO listening_stats(show_id, listened_seconds)
                   SELECT id, ? FROM shows WHERE id=?
                   ON CONFLICT(show_id) DO UPDATE SET
                   listened_seconds=listening_stats.listened_seconds+excluded.listened_seconds""",
                (seconds, show_id),
            )

    def increment_completed(self, show_id: int):
        if not show_id:
            return
        with self.database.connect() as connection:
            connection.execute(
                "UPDATE playback_metrics SET completed_episodes=completed_episodes+1 WHERE singleton_id=1"
            )
            connection.execute(
                """INSERT INTO listening_stats(show_id, completed_episodes)
                   SELECT id, 1 FROM shows WHERE id=? ON CONFLICT(show_id) DO UPDATE SET
                   completed_episodes=listening_stats.completed_episodes+1""",
                (show_id,),
            )

    def statistics(self) -> dict:
        with self.database.connect() as connection:
            total = connection.execute(
                "SELECT listened_seconds, completed_episodes, silence_saved FROM playback_metrics WHERE singleton_id=1"
            ).fetchone()
            top = connection.execute(
                """SELECT s.title, ls.listened_seconds, ls.completed_episodes
                   FROM listening_stats ls JOIN shows s ON s.id=ls.show_id
                   ORDER BY ls.listened_seconds DESC LIMIT 5"""
            ).fetchall()
        return {
            "listened_seconds": float(total["listened_seconds"] if total else 0),
            "completed_episodes": int(total["completed_episodes"] if total else 0),
            "silence_saved": float(total["silence_saved"] if total else 0),
            "top": [(row["title"], float(row["listened_seconds"]), int(row["completed_episodes"])) for row in top],
        }

