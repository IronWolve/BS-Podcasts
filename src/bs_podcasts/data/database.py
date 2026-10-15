"""Connection factory and numbered transactional migrations."""

from contextlib import closing, contextmanager, nullcontext
from pathlib import Path
from datetime import datetime
import logging
import os
import re
import shutil
import sqlite3
import threading
import weakref
import tempfile
import time
import stat
from .files import atomic_write, private_file, sync_directory

_log = logging.getLogger("bs_podcasts")

# Optimize waits this long for running queries to finish, then gives up. A
# waiting exclusive request holds back every new query, so the wait must end.
_MAINTENANCE_WAIT_SECONDS = 10.0

# backup(), _backup_before_migration() and repair() write a scratch copy with
# one of these prefixes and rename it on success. A process killed in the
# middle leaves it behind; opening the library removes the ones older than
# this process (the library lease keeps a second instance from running one).
_SCRATCH_PREFIXES = (".library-backup-", ".library-repair-")
_PROCESS_START = time.time()


class _Connection(sqlite3.Connection):
    """Weak-referenceable handle, still used by only one query thread."""


class DatabaseIntegrityError(RuntimeError):
    pass


class EmptyLibraryError(DatabaseIntegrityError):
    """A zero-byte library file. Repair rebuilds rows and an empty file has
    none, so only a backup can replace it."""


def is_corruption_error(error) -> bool:
    """Only SQLite corruption codes or an explicit app format validator."""
    if isinstance(error, EmptyLibraryError):
        return False
    if isinstance(error, DatabaseIntegrityError):
        return True
    if not isinstance(error, sqlite3.DatabaseError):
        return False
    code = getattr(error, "sqlite_errorcode", None)
    return isinstance(code, int) and (code & 0xff) in {sqlite3.SQLITE_CORRUPT, sqlite3.SQLITE_NOTADB}


def is_lock_error(error) -> bool:
    """A busy or locked file says nothing about the data inside it."""
    code = getattr(error, "sqlite_errorcode", None)
    return isinstance(code, int) and (code & 0xff) in {sqlite3.SQLITE_BUSY, sqlite3.SQLITE_LOCKED}


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
    def exclusive(self, timeout=None):
        if getattr(self._held, "depth", 0):
            # Waiting on exclusive while holding shared would deadlock this
            # thread AND (via writer preference) freeze every other one.
            raise RuntimeError("exclusive() requested while holding a shared database lock")
        with self._cond:
            deadline = None if timeout is None else time.monotonic() + max(0.0, float(timeout))
            self._exclusive_waiting += 1
            try:
                while self._exclusive or self._shared:
                    remaining = None if deadline is None else deadline - time.monotonic()
                    if remaining is not None and remaining <= 0:
                        raise TimeoutError("Database queries are still active.")
                    self._cond.wait(remaining)
                self._exclusive = True
            finally:
                self._exclusive_waiting -= 1
                self._cond.notify_all()
        try:
            yield
        finally:
            with self._cond:
                self._exclusive = False
                self._cond.notify_all()


class Database:
    def __init__(self, path: str | Path):
        if sqlite3.sqlite_version_info < (3, 43, 0):
            raise sqlite3.DatabaseError("BS Podcasts requires SQLite 3.43 or newer. Update this application's Python runtime.")
        self._init_state(path)
        if self.path.is_file() and not self._preexisting:
            # An existing but empty file is damage (full disk, failed copy,
            # quarantine placeholder), not a first launch. Opening it as a
            # fresh library silently hid every subscription.
            # Only when no backup exists at all is "start empty" the safe call.
            backup = self._recovery_backup()
            if backup is not None:
                raise EmptyLibraryError(
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

    def _init_state(self, path):
        """Every attribute a Database needs; recover() builds its object with it too."""
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
        self._backup_retention_lock = threading.Lock()
        self.last_repair_lost = {}
        self._preexisting = self.path.is_file() and self.path.stat().st_size > 0

    def _sweep_interrupted_copies(self):
        """Remove scratch copies an earlier process left when it was killed
        in the middle of a backup or repair. A copy made since this process
        started belongs to a live operation and stays, and so does every
        finished backup (those never carry a scratch name)."""
        for directory in (self.path.parent, self.path.parent / "backups"):
            try:
                found = [p for p in directory.iterdir() if p.name.startswith(_SCRATCH_PREFIXES) and ".tmp" in p.name]
            except OSError:
                continue
            for path in found:
                try:
                    info = path.lstat()
                    if stat.S_ISREG(info.st_mode) and info.st_mtime < _PROCESS_START:
                        path.unlink()
                except OSError:
                    pass

    def _backup_candidates(self) -> list:
        """Every non-empty library copy the app keeps, newest first.

        That is the upgrade snapshots beside the library (the first one
        unstamped, every later one stamped, see `_backup_before_migration`)
        and the copies in backups/. Copies of a damaged library are left out.
        Recovery and the retired-ID floors both read this one list, so they
        cannot disagree about which copy is the newest.
        """
        unstamped = self.path.with_suffix(self.path.suffix + ".pre-migration.bak")
        stamped = unstamped.name[:-len(".bak")] + "-"
        found = []
        try:
            found.extend(p for p in self.path.parent.iterdir()
                         if p.name == unstamped.name or (p.name.startswith(stamped) and p.name.endswith(".bak")))
        except OSError:
            pass
        backups = self.path.parent / "backups"
        if backups.is_dir():
            found.extend(p for p in backups.glob("library-*.db") if not p.name.startswith("library-damaged-"))
        usable = []
        for path in found:
            try:
                info = path.lstat()
            except OSError:
                continue
            if stat.S_ISREG(info.st_mode) and info.st_size > 0:
                usable.append((info.st_mtime, str(path), path))
        return [path for _mtime, _name, path in sorted(usable, reverse=True)]

    def _recovery_backup(self) -> Path | None:
        """Newest non-empty backup next to the library, if any."""
        candidates = self._backup_candidates()
        return candidates[0] if candidates else None

    def _new_connection(self) -> sqlite3.Connection:
        connection = sqlite3.connect(self.path, timeout=10.0, check_same_thread=False, factory=_Connection)
        try:
            connection.row_factory = sqlite3.Row
            connection.execute("PRAGMA foreign_keys=ON")
            # sqlite3.connect(timeout=10.0) already installs this per-handle
            # busy timeout; setting the same value again is redundant.
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
        """Run SQLite's quick_check; raise with the report when the file is damaged.

        Damage that SQLite refuses while preparing the check (a malformed
        table definition, a page it cannot parse) never reaches a report. It
        is still damage, so it raises the same error; anything else, such as
        a busy library, keeps its own error.
        """
        try:
            with self.connect() as connection:
                result = connection.execute("PRAGMA quick_check").fetchone()[0]
        except sqlite3.DatabaseError as exc:
            if is_corruption_error(exc):
                raise DatabaseIntegrityError(str(exc)) from exc
            raise
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

    def try_close(self, grace_seconds=0.1) -> bool:
        """Close only after active queries leave; bounded lock acquisition.

        Never close a handle in use by a surviving worker. This does not add
        a potentially blocking explicit checkpoint to forced-exit cleanup.
        """
        try:
            with self._lock.exclusive(timeout=grace_seconds):
                self._retire_connections()
            return True
        except TimeoutError:
            return False

    @contextmanager
    def connect(self):
        with self._lock.shared():
            connection = self._thread_connection()
            try:
                yield connection
                connection.commit()
            except BaseException:
                try:
                    connection.rollback()
                except sqlite3.Error:
                    # The handle itself is unusable; drop it so the next call
                    # on this thread opens a fresh one rather than reusing a
                    # broken connection forever.
                    self._drop_thread_connection()
                raise

    def _initialize(self):
        self._sweep_interrupted_copies()
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
        self._refuse_missing_search_module()
        pending = [migration for migration in migrations if int(migration.name.split("_", 1)[0]) not in applied_versions]
        for migration in pending:
            version = int(migration.name.split("_", 1)[0])
            try:
                with self.connect() as connection:
                    script = migration.read_text(encoding="utf-8")
                    connection.executescript(
                        "BEGIN IMMEDIATE;\n" + script
                        + f"\nINSERT INTO schema_migrations(version) VALUES ({version});\nCOMMIT;"
                    )
            except sqlite3.OperationalError as exc:
                optional_search = migration.name == "025_episode_search.sql"
                unavailable = any(text in str(exc).lower() for text in
                                  ("no such module: fts5", "no such tokenizer: trigram"))
                if not optional_search or not unavailable:
                    raise
                # Builds without optional FTS support keep the literal search
                # path. Do not fail startup or repeat a full upgrade backup.
                with self.connect() as connection:
                    connection.execute("INSERT INTO schema_migrations(version) VALUES (?)", (version,))
                _log.info("Indexed episode search is unavailable in this SQLite runtime; using literal search")
        if pending and self._preexisting:
            self._prune_backups()

    def _refuse_missing_search_module(self):
        # Migration 025 skips the search index on a runtime without FTS5, but
        # a library built where it exists keeps the table and its triggers.
        # Opened on a runtime without the module, every episode write and
        # every search would fail, so refuse before any write.
        with self.connect() as connection:
            if not connection.execute("SELECT 1 FROM sqlite_master WHERE name='episode_search'").fetchone():
                return
            try:
                connection.execute("CREATE VIRTUAL TABLE temp.fts5_probe USING fts5(x, tokenize='trigram')")
                connection.execute("DROP TABLE temp.fts5_probe")
            except sqlite3.OperationalError as exc:
                if any(text in str(exc).lower() for text in ("no such module", "no such tokenizer")):
                    raise sqlite3.DatabaseError(
                        "This library has an episode search index that needs the FTS5 module of SQLite, "
                        "and this application's Python runtime does not include it. "
                        "It was left unchanged. Open it with a runtime that includes FTS5."
                    ) from exc

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
        library, or when that cannot be told — the caller can show
        something before that blocks."""
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
        try:
            return bool(versions - probe._applied_versions_readonly())
        except DatabaseIntegrityError:
            # This is only the optional startup-status hint. A real open
            # reports unknown/unreadable schema without taking a blind backup.
            return False
        except sqlite3.Error as exc:
            # A probe that failed for any other reason does not show that no
            # upgrade is due: the real open may still back up and migrate, so
            # it must take the worker path with the notice. Damage the open
            # refuses before any backup stays False.
            return not is_corruption_error(exc)

    def _applied_versions_readonly(self) -> set:
        """Applied migration versions, read without writing to the file."""
        connection = sqlite3.connect(self.path.resolve().as_uri() + "?mode=ro", uri=True)
        try:
            applied = {
                row[0]
                for row in connection.execute("SELECT version FROM schema_migrations").fetchall()
            }
            if any(not isinstance(value, int) or value < 1 for value in applied):
                raise DatabaseIntegrityError("The library's format record contains invalid migration versions.")
            if not applied and connection.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name IN ('shows','episodes')").fetchone():
                raise DatabaseIntegrityError("The library's format record has no migration versions. Restore a library backup.")
            return applied
        except sqlite3.OperationalError as exc:
            if "no such table: schema_migrations" in str(exc):
                if connection.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name IN ('shows','episodes')").fetchone():
                    raise DatabaseIntegrityError("The library's format record is missing. Restore a library backup.") from exc
                return set()
            if "no such column: version" in str(exc):
                raise DatabaseIntegrityError("The library's format record has an invalid schema.") from exc
            raise
        finally:
            connection.close()

    def _copy_to(self, target: Path, lock=None):
        """Copy the library to `target` through SQLite's backup, written to a sidecar in the same folder and
        put in place only when complete. A copy that failed halfway used to leave a truncated file with a
        valid backup name, exactly the file a user would later restore from. `lock` is a context manager
        held around the read (the backup holds the shared lock; a pre-migration copy owns the exclusive one)."""
        descriptor, name = tempfile.mkstemp(prefix=".library-backup-", suffix=".tmp", dir=target.parent)
        os.close(descriptor)
        temporary = Path(name)
        try:
            with lock if lock is not None else nullcontext():
                with closing(sqlite3.connect(self.path)) as source, closing(sqlite3.connect(temporary)) as destination:
                    source.backup(destination)
            os.replace(temporary, target)
            sync_directory(target.parent)
        finally:
            temporary.unlink(missing_ok=True)
        return target

    def _backup_before_migration(self):
        """Never replace the original recovery snapshot on an upgrade retry."""
        target = self.path.with_suffix(self.path.suffix + ".pre-migration.bak")
        if target.exists():
            target = self.path.with_suffix(self.path.suffix + ".pre-migration-" + datetime.now().strftime("%Y%m%d-%H%M%S-%f") + ".bak")
        self._copy_to(target)
        self._mark_automatic_backup(target)

    def backup(self) -> Path:
        directory = self.path.parent / "backups"
        directory.mkdir(parents=True, exist_ok=True, mode=0o700)
        with self._backup_retention_lock:
            stamp = datetime.now().strftime("%Y%m%d-%H%M%S-%f")
            target = self._copy_to(directory / f"library-{stamp}.db", lock=self._lock.shared())
            self._mark_automatic_backup(target)
            self._prune_backups()
            return target

    @staticmethod
    def _mark_automatic_backup(path):
        try:
            info = path.stat()
            atomic_write(path.with_name(path.name + ".auto"),
                         f"1 {info.st_size} {info.st_mtime_ns}".encode("ascii"))
        except OSError:
            _log.warning("Backup saved but automatic-retention marker unavailable; copy retained", exc_info=True)

    def _prune_backups(self):
        """Keep three recent generated copies, plus originals and .keep-pinned copies.

        Only runs after a successful backup or completed upgrade. Failed upgrades,
        damaged/manual copies, links and unmarked legacy copies never authorize deletion.
        """
        stamp = r"\d{8}-\d{6}(?:-\d{6})?"
        patterns = (
            (self.path.parent, re.compile(re.escape(self.path.name) + r"\.pre-migration-" + stamp + r"\.bak")),
            (self.path.parent / "backups", re.compile(r"library-" + stamp + r"\.db")),
        )
        candidates = []
        def pinned(path):
            try:
                path.with_name(path.name + ".keep").lstat()
                return True
            except FileNotFoundError:
                return False
        for directory, pattern in patterns:
            try:
                if directory.is_symlink():
                    continue
                for path in directory.iterdir():
                    if not pattern.fullmatch(path.name):
                        continue
                    info = path.lstat()
                    if stat.S_ISREG(info.st_mode) and info.st_size > 0 and not pinned(path):
                        marker = path.with_name(path.name + ".auto")
                        if marker.is_symlink():
                            continue
                        try:
                            expected = f"1 {info.st_size} {info.st_mtime_ns}"
                            with marker.open(encoding="ascii") as record:
                                if record.read(128) == expected:
                                    candidates.append((info.st_mtime_ns, path.name, path, info))
                        except (OSError, UnicodeError):
                            continue
            except FileNotFoundError:
                continue
            except OSError:
                _log.warning("Could not enumerate library backups; retention skipped", exc_info=True)
                return
        for _mtime, _name, path, before in sorted(candidates, reverse=True)[3:]:
            try:
                after = path.lstat()
                if ((before.st_dev, before.st_ino, before.st_mtime_ns, before.st_size)
                        == (after.st_dev, after.st_ino, after.st_mtime_ns, after.st_size)
                        and stat.S_ISREG(after.st_mode)
                        and not path.parent.is_symlink() and not pinned(path)):
                    path.unlink()
                    path.with_name(path.name + ".auto").unlink(missing_ok=True)
            except OSError:
                _log.warning("Could not prune an older library backup; copy retained", exc_info=True)

    def reindex(self):
        with self._lock.exclusive():
            connection = self._new_connection()
            try:
                connection.execute("REINDEX")
                connection.commit()
            finally:
                connection.close()

    def optimize(self):
        """Refresh the planner statistics, then rewrite the file compactly.

        The exclusive lock stays held through VACUUM, on purpose. VACUUM
        rewrites the whole file in one write transaction. It cannot run
        inside a transaction. In WAL mode it does not need other connections
        closed and readers on them are not blocked by the database engine,
        but every other writer waits until it commits and fails with
        "database is locked" once its busy timeout is spent. connect() cannot
        tell a reader from a writer, so writers are kept in line here, and
        while the rewrite runs every connect() waits: the stall lasts as long
        as the rewrite and grows with the file. Writing the copy to a sibling
        file (VACUUM INTO) would not shorten it safely, because the copy then
        has to replace the library and any write made meanwhile would be lost
        unless writers were kept out for the whole copy anyway.

        Waiting for running queries to finish is bounded, since a pending
        exclusive request holds back every new connect(): Optimize raises
        TimeoutError rather than hold the application still behind one long
        query, and can simply be run again.
        """
        with self._lock.exclusive(timeout=_MAINTENANCE_WAIT_SECONDS):
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
                    lost = self._salvage(source, destination, self._backup_sequence_floors())
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
                # Source handles are closed. Isolate any remaining old WAL
                # before replacement; never let it attach to the rebuilt file.
                moved_sidecars = []
                try:
                    for suffix in ("-wal", "-shm"):
                        sidecar = Path(str(self.path) + suffix)
                        if sidecar.exists():
                            saved = Path(str(damaged) + ".retired" + suffix)
                            os.replace(sidecar, saved)
                            moved_sidecars.append((sidecar, saved))
                    os.replace(temporary, self.path)
                    sync_directory(self.path.parent)
                    sync_directory(directory)
                except BaseException:
                    for sidecar, saved in reversed(moved_sidecars):
                        os.replace(saved, sidecar)
                    raise
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
            # Old handles were closed before any snapshot or replacement.
            return damaged

    @classmethod
    def recover(cls, path: str | Path) -> Path:
        """Owner-confirmed startup repair, without requiring a successful open.

        The caller must hold this library's lease and display the exact path.
        The damaged file and any old sidecars are retained beside backups.
        """
        database = cls.__new__(cls)
        database._init_state(path)
        info = database.path.lstat()
        if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1:
            raise OSError("Library repair requires a regular, unshared file.")
        if info.st_size == 0:
            raise DatabaseIntegrityError("An empty library cannot be repaired from itself. Restore a non-empty library backup.")
        try:
            return database.repair()
        finally:
            database.close()

    _SEED_ROWS = (
        ("playback_state", "INSERT OR IGNORE INTO playback_state(singleton_id, state, updated_at) VALUES (1, 'idle', 0)"),
        ("playback_metrics", "INSERT OR IGNORE INTO playback_metrics(singleton_id, silence_saved) VALUES (1, 0)"),
    )

    @classmethod
    def _salvage(cls, source, destination, sequence_floors=None) -> dict:
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
        if not any(name == "schema_migrations" for name, _sql in tables):
            raise DatabaseIntegrityError("The library's format record is missing; a backup is needed to recover safely.")
        search_schema = next((sql for name, sql in tables if name == "episode_search"), None)
        search_shadows = ({row[1] for row in source.execute("PRAGMA main.table_list")
                           if row[2] == "shadow" and row[1].startswith("episode_search_")}
                          if search_schema else set())
        for name, sql in tables:
            if name.startswith("sqlite_"):
                continue  # statistics only; no table uses AUTOINCREMENT sequences
            if search_schema and (name == "episode_search" or name in search_shadows):
                continue  # derived virtual index + shadows are rebuilt, not salvaged
            destination.execute(sql)
            table = '"' + name.replace('"', '""') + '"'
            width = len(destination.execute(f"PRAGMA table_info({table})").fetchall())
            insert = f"INSERT INTO {table} VALUES ({','.join('?' * width)})"
            destination.execute("SAVEPOINT salvage")
            try:
                cls._copy_rows(source, destination, f"SELECT * FROM {table}", (), insert)
            except sqlite3.DatabaseError as exc:
                if is_lock_error(exc):
                    raise  # a lock is not damage: never rebuild the library without these rows
                destination.execute("ROLLBACK TO salvage")
                lost[name] = cls._copy_readable_ranges(source, destination, table, insert)
                if name == "schema_migrations" and lost[name] != 0:
                    # Without its record every migration would run again over
                    # the built schema and the rebuilt library would not open.
                    raise DatabaseIntegrityError(
                        "the record of the library's format (schema_migrations) could not be read, "
                        "so a rebuilt copy could not be opened")
            destination.execute("RELEASE salvage")
        applied = [row[0] for row in destination.execute("SELECT version FROM schema_migrations")]
        if not applied or any(not isinstance(value, int) or value < 1 for value in applied):
            raise DatabaseIntegrityError("The library's format record could not be recovered; a backup is needed.")
        for table, count in cls._drop_orphans(destination).items():
            lost[table] = (lost.get(table) or 0) + count
        if search_schema:
            destination.execute(search_schema)
            destination.execute("INSERT INTO episode_search(rowid,title,description,show_title) "
                                "SELECT e.id,e.title,e.description,s.title FROM episodes e JOIN shows s ON s.id=e.show_id")
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
                highest = max(highest, (sequence_floors or {}).get(kind, 0))
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
                "SELECT COALESCE(NULLIF(published_at,''),'0') || printf('!%012d',id), title, published_at "
                "FROM (SELECT id,title,published_at FROM episodes WHERE show_id=shows.id "
                "ORDER BY published_at DESC,id DESC LIMIT 1) "
                "UNION ALL SELECT '', '', '' LIMIT 1)"
            )
        return lost

    @staticmethod
    def _drop_orphans(destination) -> dict:
        """Remove copied rows whose parent row was not copied.

        Tables are copied one at a time with foreign keys off, so a child can
        outlive an unreadable parent. Left in place it is skipped by every
        query that joins the parent, still counts in the library totals, and
        would attach to an unrelated row if the lost parent's id were issued
        again. A reference declared ON DELETE SET NULL is cleared instead.
        Returns the rows removed per table.
        """
        removed, actions = {}, {}
        while violations := destination.execute("PRAGMA foreign_key_check").fetchall():
            changed = 0
            for table, rowid, _parent, key in violations:
                quoted = '"' + table.replace('"', '""') + '"'
                if table not in actions:
                    actions[table] = {row[0]: ('"' + row[3].replace('"', '""') + '"', row[6])
                                      for row in destination.execute(f"PRAGMA foreign_key_list({quoted})")}
                column, on_delete = actions[table][key]
                if on_delete == "SET NULL":
                    changed += destination.execute(f"UPDATE {quoted} SET {column}=NULL WHERE rowid=?", (rowid,)).rowcount
                else:
                    deleted = destination.execute(f"DELETE FROM {quoted} WHERE rowid=?", (rowid,)).rowcount
                    changed += deleted
                    removed[table] = removed.get(table, 0) + deleted
            if not changed:
                break  # a row that cannot be addressed by rowid would repeat forever
        return removed

    def _backup_sequence_floors(self) -> dict:
        """Recover retired-ID high-water marks from a few recent safe snapshots."""
        floors = {}
        for path in self._backup_candidates()[:3]:
            try:
                with closing(sqlite3.connect(path.resolve().as_uri() + "?mode=ro", uri=True)) as connection:
                    for kind, value in connection.execute("SELECT kind,value FROM entity_sequences"):
                        if kind in {"show", "episode"}:
                            floors[kind] = max(floors.get(kind, 0), int(value))
            except (sqlite3.Error, ValueError, TypeError):
                continue
        return floors

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
        except sqlite3.DatabaseError as exc:
            if is_lock_error(exc):
                raise
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
            except sqlite3.DatabaseError as exc:
                if is_lock_error(exc):
                    raise
                destination.execute("ROLLBACK TO salvage_range")
                if start == end:
                    lost += 1
                else:
                    middle = (start + end) // 2
                    ranges.extend(((middle + 1, end), (start, middle)))
            destination.execute("RELEASE salvage_range")
        return lost
