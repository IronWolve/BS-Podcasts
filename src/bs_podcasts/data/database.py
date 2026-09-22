"""Connection factory and numbered transactional migrations."""

from contextlib import contextmanager
from pathlib import Path
from datetime import datetime
import os
import shutil
import sqlite3
import threading
import weakref
import tempfile


class _Connection(sqlite3.Connection):
    """Weak-referenceable handle, still used by only one query thread."""


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
        # One connection per thread instead of one per query. Reopening meant
        # a fresh sqlite3.connect plus two PRAGMAs on every read, and settings
        # alone are read dozens of times at startup. The generation counter is
        # how a cached handle learns the file underneath it was replaced
        # (repair does an os.replace): each thread notices and reopens its own,
        # maintenance closes idle handles under the exclusive lock.
        self._connections = threading.local()
        self._handles = weakref.WeakSet()
        self._handles_lock = threading.Lock()
        self._generation = 0
        self._preexisting = self.path.is_file() and self.path.stat().st_size > 0
        if self.path.is_file() and not self._preexisting:
            # An existing but empty file is damage (full disk, failed copy,
            # quarantine placeholder), not a first launch. Opening it as a
            # fresh library silently hid every subscription.
            # Only when no backup exists at all is "start empty" the safe call.
            backup = self._recovery_backup()
            if backup is not None:
                raise sqlite3.DatabaseError(
                    f"The library file is empty (0 bytes): {self.path}\n\n"
                    f"A backup exists: {backup}\n"
                    "To recover, close the app, rename the backup to library.db in the same folder, "
                    "and start again. To start with an empty library instead, delete the empty file."
                )
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._initialize()

    def _recovery_backup(self) -> Path | None:
        """Newest non-empty backup next to the library, if any."""
        candidates = [self.path.with_suffix(self.path.suffix + ".pre-migration.bak")]
        backups = self.path.parent / "backups"
        if backups.is_dir():
            candidates.extend(p for p in backups.glob("library-*.db") if not p.name.startswith("library-damaged-"))
        usable = [c for c in candidates if c.is_file() and c.stat().st_size > 0]
        if not usable:
            return None
        return max(usable, key=lambda c: c.stat().st_mtime)

    def _new_connection(self) -> sqlite3.Connection:
        connection = sqlite3.connect(self.path, timeout=10.0, check_same_thread=False, factory=_Connection)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA foreign_keys=ON")
        connection.execute("PRAGMA busy_timeout=10000")
        # WAL + NORMAL: a commit appends to the WAL without an fsync; the
        # sync happens at checkpoint. An app crash loses nothing; only an
        # OS crash or power loss can drop the last few transactions, and
        # nothing here is worth a disk sync per download-progress tick
        #.
        connection.execute("PRAGMA synchronous=NORMAL")
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

    def _thread_connection(self) -> sqlite3.Connection:
        cached = getattr(self._connections, "connection", None)
        if cached is not None:
            if getattr(self._connections, "generation", -1) == self._generation:
                return cached
            try:
                cached.close()
            except sqlite3.Error:
                pass
        connection = self._new_connection()
        with self._handles_lock:
            self._handles.add(connection)
        self._connections.connection = connection
        self._connections.generation = self._generation
        return connection

    def _drop_thread_connection(self):
        cached = getattr(self._connections, "connection", None)
        self._connections.connection = None
        if cached is not None:
            try:
                cached.close()
            except sqlite3.Error:
                pass

    def _retire_connections(self):
        # Caller holds exclusive: no thread can be using a handle here.
        with self._handles_lock:
            handles = list(self._handles)
            self._handles.clear()
        self._generation += 1
        self._connections.connection = None
        for connection in handles:
            connection.close()

    def close(self):
        with self._lock.exclusive():
            self._retire_connections()

    @contextmanager
    def connect(self):
        with self._lock.shared():
            connection = self._thread_connection()
            try:
                yield connection
                connection.commit()
            except Exception:
                try:
                    connection.rollback()
                except sqlite3.Error:
                    # The handle itself is unusable; drop it so the next call
                    # on this thread opens a fresh one rather than reusing a
                    # broken connection forever.
                    self._drop_thread_connection()
                raise

    def _initialize(self):
        migrations_dir = Path(__file__).with_name("migrations")
        migrations = sorted(migrations_dir.glob("[0-9][0-9][0-9]_*.sql"))
        # The pre-migration backup must be the FIRST thing that touches a
        # preexisting file: the WAL pragma and the schema_migrations CREATE
        # below both mutate it, so backing up after them snapshots a file the
        # about-to-run migration has already partly shaped. Peeking read-only
        # decides whether a backup is due without writing anything.
        if self._preexisting and migrations:
            applied = self._applied_versions_readonly()
            if any(int(m.name.split("_", 1)[0]) not in applied for m in migrations):
                self._backup_before_migration()
        with self.connect() as connection:
            # WAL is persistent in the database file; setting it once here keeps
            # every later connection to the cheap per-connection pragmas only.
            connection.execute("PRAGMA journal_mode=WAL")
            connection.execute(
                "CREATE TABLE IF NOT EXISTS schema_migrations "
                "(version INTEGER PRIMARY KEY, applied_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP)"
            )

        with self.connect() as connection:
            applied_versions = {
                row["version"]
                for row in connection.execute("SELECT version FROM schema_migrations").fetchall()
            }
        pending = [migration for migration in migrations if int(migration.name.split("_", 1)[0]) not in applied_versions]
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

    @classmethod
    def migration_pending(cls, path) -> bool:
        """True when opening `path` would back up and migrate an existing
        library — the caller can show something before that blocks."""
        path = Path(path)
        try:
            if not path.is_file() or path.stat().st_size == 0:
                return False
        except OSError:
            return False
        migrations_dir = Path(__file__).with_name("migrations")
        versions = {int(m.name.split("_", 1)[0]) for m in migrations_dir.glob("[0-9][0-9][0-9]_*.sql")}
        probe = cls.__new__(cls)
        probe.path = path
        return bool(versions - probe._applied_versions_readonly())

    def _applied_versions_readonly(self) -> set:
        """Applied migration versions, read without writing to the file."""
        try:
            connection = sqlite3.connect(f"file:{self.path}?mode=ro", uri=True)
        except sqlite3.Error:
            return set()
        try:
            return {
                row[0]
                for row in connection.execute("SELECT version FROM schema_migrations").fetchall()
            }
        except sqlite3.Error:
            # No schema_migrations table: everything is pending.
            return set()
        finally:
            connection.close()

    def _backup_before_migration(self):
        """Keep one recoverable snapshot of the database before schema changes."""
        target = self.path.with_suffix(self.path.suffix + ".pre-migration.bak")
        descriptor, name = tempfile.mkstemp(prefix=".library-backup-", suffix=".tmp", dir=target.parent)
        os.close(descriptor)
        temporary = Path(name)
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
        stamp = datetime.now().strftime("%Y%m%d-%H%M%S-%f")
        target = directory / f"library-{stamp}.db"
        # Write to a sidecar and replace on success: a backup() that failed
        # halfway used to leave a truncated file with a valid backup name —
        # exactly the file a user would later restore from.
        temporary = target.with_suffix(target.suffix + ".tmp")
        with self._lock.shared():
            source = sqlite3.connect(self.path)
            destination = sqlite3.connect(temporary)
            try:
                source.backup(destination)
            except BaseException:
                destination.close()
                temporary.unlink(missing_ok=True)
                raise
            finally:
                try:
                    destination.close()
                except sqlite3.Error:
                    pass
                source.close()
        os.replace(temporary, target)
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
            self._retire_connections()
            directory = self.path.parent / "backups"
            directory.mkdir(parents=True, exist_ok=True)
            stamp = datetime.now().strftime("%Y%m%d-%H%M%S-%f")
            damaged = directory / f"library-damaged-{stamp}.db"
            # backup() folds the WAL into the snapshot; a raw file copy of a
            # live WAL database can silently miss recently committed pages,
            # making the advertised "recoverable original" unrecoverable.
            try:
                snapshot_source = sqlite3.connect(self.path)
                snapshot_target = sqlite3.connect(damaged)
                try:
                    snapshot_source.backup(snapshot_target)
                finally:
                    snapshot_target.close()
                    snapshot_source.close()
            except sqlite3.Error:
                # The file may be too damaged for the backup API; a raw copy
                # is then still better than nothing.
                shutil.copy2(self.path, damaged)
                for suffix in ("-wal", "-shm"):
                    sidecar = Path(str(self.path) + suffix)
                    if sidecar.is_file():
                        shutil.copy2(sidecar, Path(str(damaged) + suffix))
            descriptor, name = tempfile.mkstemp(prefix=".library-repair-", suffix=".tmp", dir=self.path.parent)
            os.close(descriptor)
            temporary = Path(name)
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
            # Replace first, sidecars after: unlinking the WAL before a failed
            # replace would damage the live library too.
            os.replace(temporary, self.path)
            for sidecar in ("-wal", "-shm"):
                Path(str(self.path) + sidecar).unlink(missing_ok=True)
            # Old handles were closed before any snapshot or replacement.
            return damaged
