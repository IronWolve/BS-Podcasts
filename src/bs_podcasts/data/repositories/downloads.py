"""Persisted download state and exact cleanup previews."""

from dataclasses import dataclass
from pathlib import Path
import time

from ...domain import DownloadRecord, DownloadState
from ..database import Database


@dataclass(frozen=True)
class DeletionPreview:
    episode_id: int
    path: str
    bytes_reclaimed: int


class DownloadRepository:
    def __init__(self, database: Database):
        self.database = database

    def prepare(self, episode_id: int, source_url: str, target: Path, partial: Path):
        now = time.time()
        with self.database.connect() as connection:
            connection.execute(
                """INSERT INTO downloads(
                   episode_id, source_url, target_path, partial_path, created_at, updated_at)
                   VALUES (?, ?, ?, ?, ?, ?)
                   ON CONFLICT(episode_id) DO UPDATE SET
                   source_url=excluded.source_url,
                   target_path=excluded.target_path,
                   partial_path=excluded.partial_path,
                   state='queued', error_message='', updated_at=excluded.updated_at""",
                (episode_id, source_url, str(target), str(partial), now, now),
            )
        return self.get(episode_id)

    def get(self, episode_id: int) -> DownloadRecord | None:
        with self.database.connect() as connection:
            row = connection.execute(self._select() + " WHERE d.episode_id=?", (episode_id,)).fetchone()
        return self._record(row) if row else None

    def list(self) -> list[DownloadRecord]:
        with self.database.connect() as connection:
            rows = connection.execute(self._select() + " ORDER BY d.updated_at DESC").fetchall()
        return [self._record(row) for row in rows]

    def progress(
        self,
        episode_id: int,
        state: DownloadState,
        bytes_done: int,
        bytes_total: int,
        error: str = "",
    ):
        with self.database.connect() as connection:
            connection.execute(
                """UPDATE downloads SET state=?, bytes_done=?, bytes_total=?,
                   error_message=?, updated_at=? WHERE episode_id=?""",
                (
                    state.value,
                    max(0, int(bytes_done)),
                    max(0, int(bytes_total)),
                    error,
                    time.time(),
                    episode_id,
                ),
            )

    def complete(self, episode_id: int, path: str, size: int):
        with self.database.connect() as connection:
            connection.execute(
                """UPDATE downloads SET state='complete', target_path=?,
                   bytes_done=?, bytes_total=?, error_message='', updated_at=?
                   WHERE episode_id=?""",
                (path, size, size, time.time(), episode_id),
            )
            connection.execute(
                "UPDATE episodes SET downloaded_path=? WHERE id=?", (path, episode_id)
            )

    def cleanup_preview(self, episode_id: int) -> DeletionPreview | None:
        record = self.get(episode_id)
        if record is None:
            return None
        path = Path(record.target_path if record.state == DownloadState.COMPLETE else record.partial_path)
        size = path.stat().st_size if path.is_file() else 0
        return DeletionPreview(episode_id, str(path), size)

    @staticmethod
    def _select():
        return (
            "SELECT d.*, e.title AS episode_title, s.title AS show_title "
            "FROM downloads d JOIN episodes e ON e.id=d.episode_id "
            "JOIN shows s ON s.id=e.show_id"
        )

    @staticmethod
    def _record(row):
        return DownloadRecord(
            id=row["id"],
            episode_id=row["episode_id"],
            source_url=row["source_url"],
            target_path=row["target_path"],
            partial_path=row["partial_path"],
            state=DownloadState(row["state"]),
            bytes_done=row["bytes_done"],
            bytes_total=row["bytes_total"],
            error_message=row["error_message"],
            episode_title=row["episode_title"],
            show_title=row["show_title"],
        )
