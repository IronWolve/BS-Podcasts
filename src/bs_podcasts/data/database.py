"""Connection factory and numbered transactional migrations."""

from contextlib import contextmanager
from pathlib import Path
from datetime import datetime
import os
import shutil
import sqlite3
import threading


class DatabaseIntegrityError(RuntimeError):
    pass


class _SharedExclusiveLock:
    """Many concurrent shared holders, or one exclusive holder.

    Normal queries take `shared`: WAL lets readers run alongside one writer,
    and `busy_timeout` serializes concurrent writers, so Python-level mutual
    exclusion would only add contention. `exclusive` is for maintenance that
    must see no other open connection (VACUUM, file replacement). A pending
    exclusive request bars new shared holders so it cannot starve; a thread
    that already holds shared re-enters without waiting.
    """

    def __init__(self):
        self._cond = threading.Condition()
        self._shared = 0
        self._exclusive = False
        self._exclusive_waiting = 0
        self._held = threading.local()

    @contextmanager
    def shared(self):
        depth = getattr(self._held, "depth", 0)
        if depth:
            self._held.depth = depth + 1
            try:
                yield
            finally:
                self._held.depth -= 1
            return
        with self._cond:
            while self._exclusive or self._exclusive_waiting:
                self._cond.wait()
            self._shared += 1
        self._held.depth = 1
        try:
            yield
        finally:
            self._held.depth = 0
            with self._cond:
                self._shared -= 1
                self._cond.notify_all()

    @contextmanager
    def exclusive(self):
        if getattr(self._held, "depth", 0):
            # Waiting on exclusive while holding shared would deadlock this
            # thread AND (via writer preference) freeze every other one.
            raise RuntimeError("exclusive() requested while holding a shared database lock")
        with self._cond:
            self._exclusive_waiting += 1
            try:
                while self._exclusive or self._shared:
                    self._cond.wait()
                self._exclusive = True
            finally:
                self._exclusive_waiting -= 1
        try:
            yield
        finally:
            with self._cond:
                self._exclusive = False
                self._cond.notify_all()


class Database:
    def __init__(self, path: str | Path):
        self.path = Path(path)
        self._lock = _SharedExclusiveLock()
        self._preexisting = self.path.is_file() and self.path.stat().st_size > 0
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._initialize()

    def _new_connection(self) -> sqlite3.Connection:
        connection = sqlite3.connect(self.path, timeout=10.0)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA foreign_keys=ON")
        connection.execute("PRAGMA busy_timeout=10000")
        return connection

    @property
    def maintenance_active(self) -> bool:
        """True while exclusive maintenance holds (or waits for) the lock."""
        return bool(self._lock._exclusive or self._lock._exclusive_waiting)

    def check_integrity(self) -> str:
        """Run SQLite's quick_check; raise with the report when the file is damaged."""
        with self.connect() as connection:
            result = connection.execute("PRAGMA quick_check").fetchone()[0]
        if result != "ok":
            raise DatabaseIntegrityError(result)
        return result

    @contextmanager
    def connect(self):
        with self._lock.shared():
            connection = self._new_connection()
            try:
                yield connection
                connection.commit()
            except Exception:
                connection.rollback()
                raise
            finally:
                connection.close()

    def _initialize(self):
        with self.connect() as connection:
            # WAL is persistent in the database file; setting it once here keeps
            # every later connection to the cheap per-connection pragmas only.
            connection.execute("PRAGMA journal_mode=WAL")
            connection.execute(
                "CREATE TABLE IF NOT EXISTS schema_migrations "
                "(version INTEGER PRIMARY KEY, applied_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP)"
            )

        migrations_dir = Path(__file__).with_name("migrations")
        migrations = sorted(migrations_dir.glob("[0-9][0-9][0-9]_*.sql"))
        with self.connect() as connection:
            applied_versions = {
                row["version"]
                for row in connection.execute("SELECT version FROM schema_migrations").fetchall()
            }
        pending = [migration for migration in migrations if int(migration.name.split("_", 1)[0]) not in applied_versions]
        if pending and self._preexisting:
            self._backup_before_migration()
        for migration in pending:
            version = int(migration.name.split("_", 1)[0])
            with self.connect() as connection:
                script = migration.read_text(encoding="utf-8")
                connection.executescript(
                    "BEGIN IMMEDIATE;\n"
                    + script
                    + f"\nINSERT INTO schema_migrations(version) VALUES ({version});\n"
                    + "COMMIT;"
                )

    def _backup_before_migration(self):
        """Keep one recoverable snapshot of the database before schema changes."""
        target = self.path.with_suffix(self.path.suffix + ".pre-migration.bak")
        temporary = target.with_suffix(target.suffix + ".tmp")
        temporary.unlink(missing_ok=True)
        source = sqlite3.connect(self.path)
        destination = sqlite3.connect(temporary)
        try:
            source.backup(destination)
        finally:
            destination.close()
            source.close()
        os.replace(temporary, target)

    def backup(self) -> Path:
        directory = self.path.parent / "backups"
        directory.mkdir(parents=True, exist_ok=True)
        stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
        target = directory / f"library-{stamp}.db"
        with self._lock.shared():
            source = sqlite3.connect(self.path)
            destination = sqlite3.connect(target)
            try:
                source.backup(destination)
            finally:
                destination.close()
                source.close()
        return target

    def reindex(self):
        with self._lock.exclusive():
            connection = self._new_connection()
            try:
                connection.execute("REINDEX")
                connection.commit()
            finally:
                connection.close()

    def optimize(self):
        with self._lock.exclusive():
            connection = self._new_connection()
            try:
                connection.execute("ANALYZE")
                connection.execute("PRAGMA optimize")
                connection.commit()
                connection.execute("VACUUM")
            finally:
                connection.close()

    def repair(self) -> Path:
        """Rebuild into a fresh SQLite file. Call only after integrity failure."""
        with self._lock.exclusive():
            directory = self.path.parent / "backups"
            directory.mkdir(parents=True, exist_ok=True)
            stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
            damaged = directory / f"library-damaged-{stamp}.db"
            shutil.copy2(self.path, damaged)
            temporary = self.path.with_suffix(self.path.suffix + ".repair.tmp")
            temporary.unlink(missing_ok=True)
            source = sqlite3.connect(self.path)
            destination = sqlite3.connect(temporary)
            succeeded = False
            try:
                script = "\n".join(source.iterdump())
                destination.executescript(script)
                destination.commit()
                # The rebuilt file must stay in WAL mode: the shared lock lets
                # readers run alongside a writer on exactly that premise.
                destination.execute("PRAGMA journal_mode=WAL")
                report = destination.execute("PRAGMA quick_check").fetchone()[0]
                if report != "ok":
                    raise DatabaseIntegrityError(report)
                succeeded = True
            finally:
                destination.close()
                source.close()
                if not succeeded:
                    temporary.unlink(missing_ok=True)
            for sidecar in ("-wal", "-shm"):
                Path(str(self.path) + sidecar).unlink(missing_ok=True)
            os.replace(temporary, self.path)
            return damaged
