"""Connection factory and numbered transactional migrations."""

from contextlib import closing, contextmanager
from pathlib import Path
from datetime import datetime
import logging
import os
import shutil
import sqlite3
import threading
import weakref
import tempfile
from .files import private_file

_log = logging.getLogger("bs_podcasts")


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
        self.last_repair_lost = {}
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
        self.path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        private_file(self.path)
        try:
            self._initialize()
        except BaseException:
            self.close()
            raise

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
        try:
            connection.row_factory = sqlite3.Row
            connection.execute("PRAGMA foreign_keys=ON")
            connection.execute("PRAGMA busy_timeout=10000")
            # WAL + NORMAL syncs at checkpoints, not every progress update.
            connection.execute("PRAGMA synchronous=NORMAL")
        except BaseException:
            connection.close()
            raise
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
        shipped = {int(m.name.split("_", 1)[0]) for m in migrations}
        # The pre-migration backup must be the FIRST thing that touches a
        # preexisting file: the WAL pragma and the schema_migrations CREATE
        # below both mutate it, so backing up after them snapshots a file the
        # about-to-run migration has already partly shaped. Peeking read-only
        # decides whether a backup is due without writing anything.
        if self._preexisting:
            applied = self._applied_versions_readonly()
            self._refuse_newer_schema(applied, shipped)
            if shipped - applied:
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
        self._refuse_newer_schema(applied_versions, shipped)
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

    @staticmethod
    def _refuse_newer_schema(applied, shipped):
        # A newer release has migrated this library. Code from this build
        # would write rows its schema no longer expects, and the newer
        # release never re-migrates them. Refuse before any write.
        newest, known = max(applied, default=0), max(shipped, default=0)
        if newest > known:
            raise sqlite3.DatabaseError(
                f"This library was updated by a newer version of the app (library format {newest}; "
                f"this version understands up to {known}). It was left unchanged. "
                "Open it with the newer version."
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
            connection = sqlite3.connect(self.path.resolve().as_uri() + "?mode=ro", uri=True)
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
        """Never replace the original recovery snapshot on an upgrade retry."""
        target = self.path.with_suffix(self.path.suffix + ".pre-migration.bak")
        if target.exists():
            target = self.path.with_suffix(self.path.suffix + ".pre-migration-" + datetime.now().strftime("%Y%m%d-%H%M%S-%f") + ".bak")
        descriptor, name = tempfile.mkstemp(prefix=".library-backup-", suffix=".tmp", dir=target.parent)
        os.close(descriptor)
        temporary = Path(name)
        try:
            with closing(sqlite3.connect(self.path)) as source, closing(sqlite3.connect(temporary)) as destination:
                source.backup(destination)
            os.replace(temporary, target)
        finally:
            temporary.unlink(missing_ok=True)

    def backup(self) -> Path:
        directory = self.path.parent / "backups"
        directory.mkdir(parents=True, exist_ok=True, mode=0o700)
        stamp = datetime.now().strftime("%Y%m%d-%H%M%S-%f")
        target = directory / f"library-{stamp}.db"
        # Write to a sidecar and replace on success: a backup() that failed
        # halfway used to leave a truncated file with a valid backup name —
        # exactly the file a user would later restore from.
        descriptor, name = tempfile.mkstemp(prefix=".library-backup-", suffix=".tmp", dir=directory)
        os.close(descriptor)
        temporary = Path(name)
        try:
            with self._lock.shared():
                with closing(sqlite3.connect(self.path)) as source, closing(sqlite3.connect(temporary)) as destination:
                    source.backup(destination)
            os.replace(temporary, target)
        finally:
            temporary.unlink(missing_ok=True)
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
            directory.mkdir(parents=True, exist_ok=True, mode=0o700)
            stamp = datetime.now().strftime("%Y%m%d-%H%M%S-%f")
            damaged = directory / f"library-damaged-{stamp}.db"
            private_file(damaged)
            # backup() folds the WAL into the snapshot; a raw file copy of a
            # live WAL database can silently miss recently committed pages,
            # making the advertised "recoverable original" unrecoverable.
            try:
                with closing(sqlite3.connect(self.path)) as snapshot_source, closing(sqlite3.connect(damaged)) as snapshot_target:
                    snapshot_source.backup(snapshot_target)
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
            replace_error = None
            try:
                with closing(sqlite3.connect(self.path)) as source, closing(sqlite3.connect(temporary)) as destination:
                    lost = self._salvage(source, destination)
                    destination.commit()
                    self.last_repair_lost = lost
                    if lost:
                        _log.warning("Repair could not read every row: %s", ", ".join(
                            f"{table}: {'all' if count is None else count}" for table, count in sorted(lost.items())))
                    # Shared readers require WAL after replacement too.
                    destination.execute("PRAGMA journal_mode=WAL")
                    report = destination.execute("PRAGMA quick_check").fetchone()[0]
                    if report != "ok":
                        raise DatabaseIntegrityError(report)
                # Replace first, sidecars after: unlinking the WAL before a failed
                # replace would damage the live library too.
                os.replace(temporary, self.path)
            except Exception as exc:
                replace_error = exc
            finally:
                for suffix in ("-wal", "-shm"):
                    try:
                        Path(str(temporary) + suffix).unlink(missing_ok=True)
                    except OSError:
                        pass
                if temporary.exists():
                    try:
                        temporary.unlink(missing_ok=True)
                    except OSError:
                        if replace_error is not None and temporary.exists():
                            raise type(replace_error)(
                                f"{replace_error}; repair file remains at {temporary}"
                            ) from replace_error
            if replace_error is not None:
                raise replace_error
            for sidecar in ("-wal", "-shm"):
                Path(str(self.path) + sidecar).unlink(missing_ok=True)
            # Old handles were closed before any snapshot or replacement.
            return damaged

    _SEED_ROWS = (
        ("playback_state", "INSERT OR IGNORE INTO playback_state(singleton_id, state, updated_at) VALUES (1, 'idle', 0)"),
        ("playback_metrics", "INSERT OR IGNORE INTO playback_metrics(singleton_id, silence_saved) VALUES (1, 0)"),
    )

    @classmethod
    def _salvage(cls, source, destination) -> dict:
        """Copy the schema and rows table by table into a fresh file.

        Rows stream in batches rather than as one dump script held in
        memory. When a table has unreadable pages, the rowid ranges that
        still read are kept. Returns the rows left out per table (None when
        the whole table was unreadable). Indexes and triggers come last, as
        in a dump, so triggers do not fire for copied rows.
        """
        source.row_factory = None
        lost = {}
        tables = source.execute(
            "SELECT name, sql FROM sqlite_master WHERE type='table' AND sql NOT NULL ORDER BY name"
        ).fetchall()
        for name, sql in tables:
            if name.startswith("sqlite_"):
                continue  # statistics only; no table uses AUTOINCREMENT sequences
            destination.execute(sql)
            table = '"' + name.replace('"', '""') + '"'
            width = len(destination.execute(f"PRAGMA table_info({table})").fetchall())
            insert = f"INSERT INTO {table} VALUES ({','.join('?' * width)})"
            destination.execute("SAVEPOINT salvage")
            try:
                cls._copy_rows(source, destination, f"SELECT * FROM {table}", (), insert)
            except sqlite3.DatabaseError:
                destination.execute("ROLLBACK TO salvage")
                lost[name] = cls._copy_readable_ranges(source, destination, table, insert)
                if name == "schema_migrations" and lost[name] != 0:
                    # Without its record every migration would run again over
                    # the built schema and the rebuilt library would not open.
                    raise DatabaseIntegrityError(
                        "the record of the library's format (schema_migrations) could not be read, "
                        "so a rebuilt copy could not be opened")
            destination.execute("RELEASE salvage")
        for (sql,) in source.execute(
            "SELECT sql FROM sqlite_master WHERE type IN ('index', 'trigger', 'view') AND sql NOT NULL"
        ).fetchall():
            destination.execute(sql)
        lost = {table: count for table, count in lost.items() if count != 0}
        # Rows a migration seeds once and the code only ever updates: left
        # empty, saving the playing episode, the listening totals and the
        # never-reused ids all stop working without an error.
        for table, seed in cls._SEED_ROWS:
            if table in lost:
                destination.execute(seed)
        if "entity_sequences" in lost:
            for kind, owner in (("show", "shows"), ("episode", "episodes")):
                highest = destination.execute(f"SELECT COALESCE(MAX(id), 0) FROM {owner}").fetchone()[0]
                destination.execute(
                    "INSERT INTO entity_sequences(kind, value) VALUES (?, ?) "
                    "ON CONFLICT(kind) DO UPDATE SET value=MAX(value, excluded.value)", (kind, highest))
        if "episodes" in lost:
            # Per-show counts are cached (migration 014) and only adjusted by
            # triggers; recount them for the episodes that were kept. The
            # cached latest episode goes with them: a lost newest episode
            # otherwise stays named, and a later insert only replaces it by
            # sorting after it (the expression is migration 023's).
            destination.execute(
                "UPDATE shows SET episode_count=(SELECT COUNT(*) FROM episodes e WHERE e.show_id=shows.id), "
                "new_count=(SELECT COUNT(*) FROM episodes e WHERE e.show_id=shows.id AND e.is_new=1)"
            )
            destination.execute(
                "UPDATE shows SET (latest_key, latest_episode_title, latest_episode_published_at)=("
                "SELECT COALESCE(NULLIF(published_at,''),'0') || printf('~%012d',id), title, published_at "
                "FROM (SELECT id,title,published_at FROM episodes WHERE show_id=shows.id "
                "ORDER BY published_at DESC,id DESC LIMIT 1) "
                "UNION ALL SELECT '', '', '' LIMIT 1)"
            )
        return lost

    @staticmethod
    def _copy_rows(source, destination, query, params, insert):
        cursor = source.execute(query, params)
        while rows := cursor.fetchmany(500):
            destination.executemany(insert, rows)

    @classmethod
    def _copy_readable_ranges(cls, source, destination, table, insert):
        # Halve every rowid range that fails to read until single rowids
        # remain; each of those is counted as a row that could not be read.
        try:
            low, high = source.execute(f"SELECT min(rowid), max(rowid) FROM {table}").fetchone()
        except sqlite3.DatabaseError:
            return None
        if low is None:
            return 0
        lost, ranges = 0, [(low, high)]
        while ranges:
            start, end = ranges.pop()
            destination.execute("SAVEPOINT salvage_range")
            try:
                cls._copy_rows(source, destination, f"SELECT * FROM {table} WHERE rowid BETWEEN ? AND ?",
                               (start, end), insert)
            except sqlite3.DatabaseError:
                destination.execute("ROLLBACK TO salvage_range")
                if start == end:
                    lost += 1
                else:
                    middle = (start + end) // 2
                    ranges.extend(((middle + 1, end), (start, middle)))
            destination.execute("RELEASE salvage_range")
        return lost
