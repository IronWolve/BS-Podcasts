"""Persisted download state and exact cleanup previews."""

from dataclasses import dataclass
from pathlib import Path
import time

from ...domain import DownloadRecord, DownloadState
from ..database import Database
from ..files import (
    FILE_IRREGULAR,
    FILE_MISSING,
    FILE_PRESENT,
    FILE_UNAVAILABLE,
    FILE_UNCONFIRMED,
    inspect_download_file,
)


@dataclass(frozen=True)
class DeletionPreview:
    episode_id: int
    path: str
    bytes_reclaimed: int


class DownloadRepository:
    def __init__(self, database: Database):
        self.database = database

    def prepare(self, episode_id: int, source_url: str, target: Path, partial: Path):
        """Queue a download, keeping the paths and any live transfer an existing record already has.

        Overwriting the paths on every call made the "honor the prepared paths"
        resume branch in DownloadService dead code, and orphaned the old
        partial whenever a feed refresh changed the episode's media URL.
        A row that is downloading from the same URL keeps its state: a second
        prepare while the transfer runs (or a wait that timed out) must not
        turn a live download back into a queued one.
        """
        now = time.time()
        with self.database.connect() as connection:
            connection.execute(
                """INSERT INTO downloads(
                   episode_id, source_url, target_path, partial_path, created_at, updated_at)
                   VALUES (?, ?, ?, ?, ?, ?)
                   ON CONFLICT(episode_id) DO UPDATE SET
                   etag=CASE WHEN source_url=excluded.source_url THEN etag ELSE '' END,
                   last_modified=CASE WHEN source_url=excluded.source_url THEN last_modified ELSE '' END,
                   expected_total=CASE WHEN source_url=excluded.source_url THEN expected_total ELSE 0 END,
                   state=CASE WHEN source_url=excluded.source_url AND state='downloading'
                              THEN state ELSE 'queued' END,
                   error_message=CASE WHEN source_url=excluded.source_url AND state='downloading'
                                      THEN error_message ELSE '' END,
                   source_url=excluded.source_url,
                   updated_at=MAX(downloads.updated_at+0.000001, excluded.updated_at)
                   WHERE downloads.state != 'complete'""",
                (episode_id, source_url, str(target), str(partial), now, now),
            )
        return self.get(episode_id)

    def enqueue(self, episode_id: int, source_url: str, target: Path, partial: Path) -> bool:
        """Insert a queued row for an episode that has none; True when it inserted one.

        Insert-only, unlike prepare(): a late write from the queued-row writer
        can never change a row a transfer, a pause or a failure already owns.
        """
        now = time.time()
        with self.database.connect() as connection:
            return connection.execute(
                """INSERT INTO downloads(
                   episode_id, source_url, target_path, partial_path, created_at, updated_at)
                   VALUES (?, ?, ?, ?, ?, ?)
                   ON CONFLICT(episode_id) DO NOTHING""",
                (episode_id, source_url, str(target), str(partial), now, now),
            ).rowcount == 1

    def discard_queued(self, episode_id: int):
        """Remove the episode's row only while it is still queued (never a transfer's row)."""
        with self.database.connect() as connection:
            connection.execute("DELETE FROM downloads WHERE episode_id=? AND state='queued'", (episode_id,))

    def response_metadata(self, episode_id: int, etag: str, last_modified: str, total: int):
        with self.database.connect() as connection:
            connection.execute("UPDATE downloads SET etag=?, last_modified=?, expected_total=?, "
                               "updated_at=MAX(updated_at+0.000001, ?) WHERE episode_id=? AND state != 'complete'",
                               (etag, last_modified, max(0, total), time.time(), episode_id))

    def get(self, episode_id: int) -> DownloadRecord | None:
        with self.database.connect() as connection:
            row = connection.execute(self._select() + " WHERE d.episode_id=?", (episode_id,)).fetchone()
        return self._record(row) if row else None

    def records_by_ids(self, episode_ids) -> dict[int, DownloadRecord]:
        """Fetch download records in bounded batches, keyed by episode id."""
        ids = list(dict.fromkeys(int(value) for value in episode_ids if value))
        result = {}
        with self.database.connect() as connection:
            for start in range(0, len(ids), 500):
                batch = ids[start:start + 500]
                query = self._select() + " WHERE d.episode_id IN (" + ",".join("?" for _ in batch) + ")"
                for row in connection.execute(query, batch):
                    record = self._record(row)
                    result[record.episode_id] = record
        return result

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
        # A finished row is not demoted by a late write from the transfer that
        # finished it; it may only keep its state and take a message.
        with self.database.connect() as connection:
            connection.execute(
                """UPDATE downloads SET state=?, bytes_done=?, bytes_total=?,
                   error_message=?, updated_at=MAX(updated_at+0.000001, ?)
                   WHERE episode_id=? AND (state != 'complete' OR ?='complete')""",
                (
                    state.value,
                    max(0, int(bytes_done)),
                    max(0, int(bytes_total)),
                    error,
                    time.time(),
                    episode_id,
                    state.value,
                ),
            )

    def complete(self, episode_id: int, path: str, size: int, *, length_verified: bool):
        with self.database.connect() as connection:
            changed = connection.execute(
                """UPDATE downloads SET state='complete', target_path=?,
                   bytes_done=?, bytes_total=?, error_message='', length_verified=?, updated_at=MAX(updated_at+0.000001, ?)
                   WHERE episode_id=?""",
                (path, size, size, int(bool(length_verified)), time.time(), episode_id),
            ).rowcount
            if changed:
                # No downloads row means the record was deleted mid-transfer;
                # writing downloaded_path then would point at an unlinked file.
                connection.execute(
                    "UPDATE episodes SET downloaded_path=? WHERE id=?", (path, episode_id)
                )

    def mark_missing(self, record: DownloadRecord, message: str) -> bool:
        """Demote only the exact completed record whose file was inspected."""
        with self.database.connect() as connection:
            changed = connection.execute(
                """UPDATE downloads SET state='error', bytes_done=0, bytes_total=0,
                   error_message=?, updated_at=MAX(updated_at+0.000001, ?)
                   WHERE id=? AND episode_id=? AND created_at=? AND updated_at=?
                     AND target_path=? AND state='complete'""",
                (message, time.time(), record.id, record.episode_id, record.created_at,
                 record.updated_at, record.target_path),
            ).rowcount
            if changed:
                connection.execute(
                    "UPDATE episodes SET downloaded_path='' WHERE id=? AND downloaded_path=?",
                    (record.episode_id, record.target_path),
                )
        return bool(changed)

    def remove(self, episode_id: int):
        """Forget a download record and clear the episode's local path."""
        with self.database.connect() as connection:
            connection.execute("DELETE FROM downloads WHERE episode_id=?", (episode_id,))
            connection.execute("UPDATE episodes SET downloaded_path='' WHERE id=?", (episode_id,))

    def played_complete(self, episode_ids=None) -> list[int]:
        return [record.episode_id for record in self.played_complete_records(episode_ids)]

    def played_complete_records(self, episode_ids=None) -> list[DownloadRecord]:
        ids = None if episode_ids is None else list(dict.fromkeys(int(value) for value in episode_ids))
        if ids == []:
            return []
        result = []
        with self.database.connect() as connection:
            for batch in ([None] if ids is None else (ids[i:i + 500] for i in range(0, len(ids), 500))):
                query = self._select() + " WHERE d.state='complete' AND d.length_verified=1 AND e.played=1 AND e.favorite=0"
                if batch is not None:
                    query += " AND d.episode_id IN (" + ",".join("?" for _ in batch) + ")"
                result.extend(self._record(row) for row in connection.execute(query, batch or ()))
        return result

    def cleanup_preview(self, episode_id: int, *, record=None) -> DeletionPreview | None:
        record = self.get(episode_id) if record is None else record
        if record is None:
            return None
        completed = record.state == DownloadState.COMPLETE
        path = Path(record.target_path if completed else record.partial_path)
        kind, size = inspect_download_file(path, completed=completed)
        if not completed and kind in {FILE_MISSING, FILE_UNCONFIRMED} and record.target_path:
            # A row that never reached complete can still have the finished
            # file at its target (the rename happened, the write did not).
            # Delete refuses a file the preview did not list.
            target_kind, target_size = inspect_download_file(record.target_path)
            if target_kind == FILE_PRESENT:
                return DeletionPreview(episode_id, record.target_path, target_size)
        if kind == FILE_PRESENT:
            return DeletionPreview(episode_id, str(path), size)
        if kind == FILE_MISSING:
            return DeletionPreview(episode_id, str(path), 0)
        if kind == FILE_IRREGULAR:
            raise OSError("The download path is not a regular file.")
        if kind == FILE_UNAVAILABLE:
            raise OSError("The download folder is unavailable.")
        return None

    def discard_preview(self, episode_id: int):
        record = self.get(episode_id)
        if record is None:
            return None, []
        if record.state == DownloadState.COMPLETE:
            raise OSError("This download finished. Use Remove download to delete its file.")
        previews = []
        for candidate in dict.fromkeys(path for path in (record.partial_path, record.target_path) if path):
            kind, size = inspect_download_file(candidate)
            if kind == FILE_PRESENT:
                previews.append(DeletionPreview(episode_id, candidate, size))
            elif kind in {FILE_IRREGULAR, FILE_UNAVAILABLE}:
                raise OSError("A download path is unavailable or is not a regular file.")
        return record, previews

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
            etag=row["etag"], last_modified=row["last_modified"], expected_total=row["expected_total"],
            created_at=row["created_at"], updated_at=row["updated_at"],
            length_verified=bool(row["length_verified"]),
        )

    def verify_length(self, record: DownloadRecord) -> bool:
        """Promote only the exact completed file that the verifier inspected."""
        with self.database.connect() as connection:
            changed = connection.execute(
                "UPDATE downloads SET length_verified=1, updated_at=MAX(updated_at+0.000001, ?) "
                "WHERE id=? AND episode_id=? AND target_path=? AND created_at=? AND updated_at=? AND state='complete'",
                (time.time(), record.id, record.episode_id, record.target_path, record.created_at, record.updated_at)).rowcount
        return bool(changed)
