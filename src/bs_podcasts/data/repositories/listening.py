"""Advanced listening metadata and saved collections."""

import time

from ...domain import Bookmark, Chapter, TranscriptSegment
from ...domain.times import media_seconds, media_interval
from ..database import Database


class ListeningRepository:
    def __init__(self, database: Database):
        self.database = database

    def replace_chapters(self, episode_id: int, chapters):
        with self.database.connect() as connection:
            connection.execute("DELETE FROM chapters WHERE episode_id=?", (episode_id,))
            for index, chapter in enumerate(chapters):
                start, end = media_interval(chapter[0], chapter[1])
                if start is None:
                    continue
                connection.execute(
                    """INSERT INTO chapters(
                       episode_id, chapter_index, start_seconds, end_seconds,
                       title, artwork_url) VALUES (?, ?, ?, ?, ?, ?)""",
                    (
                        episode_id,
                        index,
                        start,
                        end,
                        chapter[2],
                        chapter[3] if len(chapter) > 3 else "",
                    ),
                )

    def chapters(self, episode_id: int) -> list[Chapter]:
        with self.database.connect() as connection:
            rows = connection.execute(
                "SELECT * FROM chapters WHERE episode_id=? ORDER BY chapter_index",
                (episode_id,),
            ).fetchall()
        return [
            Chapter(
                row["id"],
                row["episode_id"],
                row["chapter_index"],
                *media_interval(row["start_seconds"], row["end_seconds"]),
                row["title"],
                row["artwork_url"],
            )
            for row in rows if media_seconds(row["start_seconds"]) is not None
        ]

    def replace_transcript(self, episode_id: int, segments):
        with self.database.connect() as connection:
            connection.execute(
                "DELETE FROM transcript_segments WHERE episode_id=?", (episode_id,)
            )
            for index, segment in enumerate(segments):
                start, end = media_interval(segment[0], segment[1])
                connection.execute(
                    """INSERT INTO transcript_segments(
                       episode_id, segment_index, start_seconds, end_seconds, text)
                       VALUES (?, ?, ?, ?, ?)""",
                    (episode_id, index, start, end, segment[2]),
                )

    def transcript(self, episode_id: int, query: str = "") -> list[TranscriptSegment]:
        sql = "SELECT * FROM transcript_segments WHERE episode_id=?"
        params: list[object] = [episode_id]
        if query.strip():
            sql += " AND text LIKE ? ESCAPE '\\'"
            escaped = query.strip().replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
            params.append(f"%{escaped}%")
        sql += " ORDER BY segment_index"
        with self.database.connect() as connection:
            rows = connection.execute(sql, params).fetchall()
        return [
            TranscriptSegment(
                row["id"],
                row["episode_id"],
                row["segment_index"],
                row["text"],
                *media_interval(row["start_seconds"], row["end_seconds"]),
            )
            for row in rows
        ]

    def add_bookmark(self, episode_id: int, position: float, title: str = "") -> Bookmark:
        with self.database.connect() as connection:
            cursor = connection.execute(
                """INSERT INTO bookmarks(episode_id, position_seconds, title, created_at)
                   VALUES (?, ?, ?, ?)""",
                (episode_id, max(0.0, position), title, time.time()),
            )
            bookmark_id = cursor.lastrowid
        return next(bookmark for bookmark in self.bookmarks() if bookmark.id == bookmark_id)

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

    def bookmarks(self, episode_id: int | None = None) -> list[Bookmark]:
        sql = (
            "SELECT b.*, e.title AS episode_title, s.title AS show_title "
            "FROM bookmarks b JOIN episodes e ON e.id=b.episode_id "
            "JOIN shows s ON s.id=e.show_id"
        )
        params = ()
        if episode_id is not None:
            sql += " WHERE b.episode_id=?"
            params = (episode_id,)
        sql += " ORDER BY b.created_at DESC"
        with self.database.connect() as connection:
            rows = connection.execute(sql, params).fetchall()
        return [
            Bookmark(
                row["id"],
                row["episode_id"],
                row["position_seconds"],
                row["title"],
                row["created_at"],
                row["episode_title"],
                row["show_title"],
            )
            for row in rows
        ]

    def add_silence_saved(self, seconds: float):
        if seconds <= 0:
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
        if not show_id or seconds <= 0:
            return
        seconds = float(seconds)
        with self.database.connect() as connection:
            connection.execute(
                "UPDATE playback_metrics SET listened_seconds=listened_seconds+? WHERE singleton_id=1",
                (seconds,),
            )
            connection.execute(
                """INSERT INTO listening_stats(show_id, listened_seconds)
                   VALUES (?, ?) ON CONFLICT(show_id) DO UPDATE SET
                   listened_seconds=listening_stats.listened_seconds+excluded.listened_seconds""",
                (show_id, seconds),
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
                   VALUES (?, 1) ON CONFLICT(show_id) DO UPDATE SET
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

    def create_folder(self, name: str) -> int:
        with self.database.connect() as connection:
            connection.execute(
                "INSERT OR IGNORE INTO folders(name, created_at) VALUES (?, ?)",
                (name.strip(), time.time()),
            )
            row = connection.execute("SELECT id FROM folders WHERE name=? COLLATE NOCASE", (name.strip(),)).fetchone()
        return row["id"]

    def assign_show(self, folder_id: int, show_id: int):
        with self.database.connect() as connection:
            connection.execute(
                "INSERT OR IGNORE INTO folder_shows(folder_id, show_id) VALUES (?, ?)",
                (folder_id, show_id),
            )

    def create_playlist(self, name: str) -> int:
        with self.database.connect() as connection:
            connection.execute(
                "INSERT OR IGNORE INTO saved_playlists(name, created_at) VALUES (?, ?)",
                (name.strip(), time.time()),
            )
            row = connection.execute(
                "SELECT id FROM saved_playlists WHERE name=? COLLATE NOCASE", (name.strip(),)
            ).fetchone()
        return row["id"]

    def set_playlist_items(self, playlist_id: int, episode_ids: list[int]):
        with self.database.connect() as connection:
            connection.execute(
                "DELETE FROM saved_playlist_items WHERE playlist_id=?", (playlist_id,)
            )
            for position, episode_id in enumerate(episode_ids, start=1):
                connection.execute(
                    """INSERT INTO saved_playlist_items(playlist_id, episode_id, position)
                       VALUES (?, ?, ?)""",
                    (playlist_id, episode_id, position),
                )

    def playlist_items(self, playlist_id: int) -> list[int]:
        with self.database.connect() as connection:
            rows = connection.execute(
                """SELECT episode_id FROM saved_playlist_items
                   WHERE playlist_id=? ORDER BY position""",
                (playlist_id,),
            ).fetchall()
        return [row["episode_id"] for row in rows]
