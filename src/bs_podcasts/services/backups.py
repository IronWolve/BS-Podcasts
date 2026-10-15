"""Cooperative, verified, rotating compressed library backups; no Qt imports."""

from datetime import datetime, timedelta, timezone
from contextlib import closing
from dataclasses import dataclass
import hashlib
import json
import os
from pathlib import Path
import re
import shutil
import sqlite3
import stat
import sys
import tempfile
import threading
import time
import zipfile

from bs_podcasts.data.files import atomic_write, sync_directory


_PREFIX = "backups."
_ARCHIVE_NAME = re.compile(r"library-auto-\d{8}-\d{6}-\d{6}\.zip\Z")
_CHUNK = 256 * 1024
_YIELD_SECONDS = 0.005
_DEADLINE_SECONDS = 30 * 60
_RESTORE = (
    "BS Podcasts library backup\n"
    "\n"
    "Close BS Podcasts and confirm it has stopped before restoring. Preserve the current library.db and any "
    "matching library.db-wal and library.db-shm together in a separate folder. Extract this archive's library.db "
    "to a separate location and inspect it before use. Replace the empty/current library.db with the extracted "
    "file only after preserving the old files together. Never put the old WAL or SHM beside the restored database; "
    "they belong to the old database. Keep the preserved files until the restored library has been checked.\n"
    "This archive contains library data only; downloaded audio and artwork are stored separately.\n"
)


class BackupCancelled(Exception):
    """Internal cooperative stop signal; cancellation is not a failure."""


@dataclass(frozen=True)
class BackupDeletion:
    path: Path
    bytes_reclaimed: int
    signature: tuple
    marker_signature: tuple
    marker_record: str


class BackupService:
    """Build scheduled backups from a LibraryRepository on a dedicated worker."""

    def __init__(self, repository):
        self.repository = repository
        self.database = repository.database
        self.destination = Path(os.path.abspath(self.database.path.parent / "backups" / "scheduled"))
        self._run_lock = threading.Lock()

    @staticmethod
    def _int_setting(repository, key, default, allowed):
        try:
            value = int(repository.get_setting(_PREFIX + key, str(default)))
        except (TypeError, ValueError, OverflowError):
            return default
        return value if value in allowed else default

    @staticmethod
    def _date(value):
        if not value:
            return None
        try:
            parsed = datetime.fromisoformat(value)
            if parsed.tzinfo is None:
                parsed = parsed.replace(tzinfo=timezone.utc)
            return parsed.astimezone(timezone.utc)
        except (TypeError, ValueError, OverflowError):
            return None

    def status(self):
        enabled_value = self.repository.get_setting(_PREFIX + "enabled", "1")
        enabled = enabled_value.strip().casefold() not in {"0", "false", "no", "off"}
        interval = self._int_setting(self.repository, "interval_days", 7, {1, 7, 30})
        keep = self._int_setting(self.repository, "keep", 2, {1, 2})
        last_success = self.repository.get_setting(_PREFIX + "last_success", "")
        now = datetime.now(timezone.utc)
        last_file = self.repository.get_setting(_PREFIX + "last_file", "") or None
        last_size = self._nonnegative_int(self.repository.get_setting(_PREFIX + "last_size", ""))
        succeeded = self._date(last_success)
        missing_snapshot = bool((last_success or last_file) and not self._last_snapshot_present(last_file))
        clock_skew = bool(succeeded is not None and succeeded > now + timedelta(days=interval * 2))
        if missing_snapshot or clock_skew:
            # A removed backup or a clock rollback must not postpone the next
            # attempt indefinitely based on stale success metadata.
            succeeded = None
        try:
            next_due = succeeded + timedelta(days=interval) if succeeded else None
        except OverflowError:
            succeeded = None
            next_due = None
        if succeeded is None:
            last_success = ""
            last_file = None
            last_size = None
        due = bool(enabled and (next_due is None or now >= next_due))
        state = self.repository.get_setting(_PREFIX + "last_state", "idle")
        if state not in {"idle", "success", "cancelled", "error"}:
            state = "idle"
        return {
            "state": state,
            "enabled": enabled,
            "interval_days": interval,
            "keep": keep,
            "due": due,
            "last_success": last_success or None,
            "last_file": last_file,
            "last_size": last_size,
            "last_snapshot_missing": missing_snapshot,
            "clock_skew": clock_skew,
            "last_error": self.repository.get_setting(_PREFIX + "last_error", "") or None,
            "next_due": next_due.isoformat(timespec="seconds") if next_due else None,
        }

    def _last_snapshot_present(self, filename):
        if not filename or not _ARCHIVE_NAME.fullmatch(filename):
            return False
        try:
            self._check_path_chain(self.destination)
            archive = self.destination / filename
            info = archive.lstat()
            marker = archive.with_name(archive.name + ".auto")
            marker_info = marker.lstat()
            if (not stat.S_ISREG(info.st_mode) or self._is_reparse(info) or info.st_size <= 0
                    or info.st_nlink != 1 or not stat.S_ISREG(marker_info.st_mode)
                    or self._is_reparse(marker_info) or marker_info.st_nlink != 1
                    or marker_info.st_size > 256):
                return False
            with marker.open("r", encoding="ascii") as handle:
                parts = handle.read(257).split()
            return (len(parts) == 4 and parts[:3] == ["1", str(info.st_size), str(info.st_mtime_ns)]
                    and bool(re.fullmatch(r"[0-9a-f]{64}", parts[3])))
        except (OSError, UnicodeError, ValueError):
            return False

    @staticmethod
    def _nonnegative_int(value):
        try:
            number = int(value)
            return number if number >= 0 else None
        except (TypeError, ValueError, OverflowError):
            return None

    @staticmethod
    def _cancelled(cancelled):
        if cancelled is None:
            return False
        if callable(cancelled):
            return bool(cancelled())
        method = getattr(cancelled, "is_set", None)
        return bool(method()) if method is not None else bool(cancelled)

    @staticmethod
    def _lower_current_thread_priority():
        """Lower only a non-main worker thread; never alter GUI/process priority."""
        if threading.current_thread() is threading.main_thread():
            return
        try:
            if sys.platform == "win32":
                import ctypes
                kernel = ctypes.WinDLL("kernel32", use_last_error=True)
                kernel.GetCurrentThread.restype = ctypes.c_void_p
                kernel.SetThreadPriority.argtypes = (ctypes.c_void_p, ctypes.c_int)
                kernel.SetThreadPriority.restype = ctypes.c_int
                kernel.SetThreadPriority(kernel.GetCurrentThread(), -2)  # THREAD_PRIORITY_LOWEST
            elif sys.platform == "darwin":
                import ctypes
                libc = ctypes.CDLL(None)
                setter = getattr(libc, "pthread_set_qos_class_self_np", None)
                if setter is not None:
                    setter(0x11, 0)  # QOS_CLASS_UTILITY
            elif hasattr(os, "setpriority"):
                os.setpriority(os.PRIO_PROCESS, threading.get_native_id(), 10)
        except (AttributeError, OSError, TypeError, ValueError):
            pass

    def _check(self, cancelled, deadline):
        if self._cancelled(cancelled):
            raise BackupCancelled()
        if time.monotonic() >= deadline:
            raise TimeoutError("The backup exceeded its 30 minute time limit.")
        if self.database.maintenance_active:
            raise BackupCancelled()

    def _save_state(self, state, error="", fields=None):
        values = {
            _PREFIX + "last_state": state,
            _PREFIX + "last_error": str(error)[:500],
        }
        if fields:
            values.update({_PREFIX + key: str(value) for key, value in fields.items()})
        self.repository.persist_settings(values)

    @staticmethod
    def _is_reparse(info):
        return bool(getattr(info, "st_file_attributes", 0) & getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0x400))

    @classmethod
    def _check_path_chain(cls, path, *, allow_missing_leaf=False):
        path = Path(os.path.abspath(path))
        chain = list(reversed(path.parents)) + [path]
        for index, item in enumerate(chain):
            try:
                info = item.lstat()
            except FileNotFoundError:
                if allow_missing_leaf and index == len(chain) - 1:
                    continue
                raise OSError(f"Backup path component is missing: {item.name}")
            if stat.S_ISLNK(info.st_mode) or cls._is_reparse(info):
                raise OSError(f"Backup refuses linked or reparse path: {item.name}")
            if index < len(chain) - 1 and not stat.S_ISDIR(info.st_mode):
                raise OSError(f"Backup path component is not a directory: {item.name}")
        return path

    def _prepare_destination(self):
        source = self._check_path_chain(self.database.path)
        if not stat.S_ISREG(source.lstat().st_mode) or source.stat().st_nlink != 1:
            raise OSError("The library database is not a private regular file.")
        parent = source.parent
        if parent != self.destination.parent.parent:
            raise OSError("The scheduled backup directory is outside the library folder.")
        self._check_path_chain(parent)
        for directory in (self.destination.parent, self.destination):
            try:
                info = directory.lstat()
            except FileNotFoundError:
                directory.mkdir(mode=0o700)
                info = directory.lstat()
            if (stat.S_ISLNK(info.st_mode) or self._is_reparse(info)
                    or not stat.S_ISDIR(info.st_mode)):
                raise OSError("The scheduled backup destination is not a private directory.")
        self._check_path_chain(self.destination)
        return source

    @staticmethod
    def _write_zip(snapshot, archive_path, created, cancel_check):
        digest = hashlib.sha256()
        snapshot_size = snapshot.stat().st_size
        metadata = json.dumps({
            "format": "bs-podcasts-library-backup",
            "version": 1,
            "created_utc": created.isoformat(timespec="seconds"),
            "database_file": "library.db",
            "database_size": snapshot_size,
        }, sort_keys=True, separators=(",", ":")).encode("utf-8")
        with zipfile.ZipFile(archive_path, "w", compression=zipfile.ZIP_DEFLATED,
                             compresslevel=6, allowZip64=True) as archive:
            archive.writestr("metadata.json", metadata)
            archive.writestr("RESTORE.txt", _RESTORE.encode("utf-8"))
            with snapshot.open("rb") as source, archive.open("library.db", "w", force_zip64=True) as output:
                while True:
                    cancel_check()
                    block = source.read(_CHUNK)
                    if not block:
                        break
                    output.write(block)
                    digest.update(block)
                    time.sleep(_YIELD_SECONDS)
        with archive_path.open("rb+") as handle:
            handle.flush()
            os.fsync(handle.fileno())
        return digest.hexdigest(), snapshot_size

    @staticmethod
    def _hash_file(path, cancel_check):
        digest = hashlib.sha256()
        with path.open("rb") as handle:
            while True:
                cancel_check()
                block = handle.read(_CHUNK)
                if not block:
                    return digest.hexdigest()
                digest.update(block)
                time.sleep(_YIELD_SECONDS)

    @classmethod
    def _verify(cls, archive_path, snapshot_hash, expected_size, cancel_check):
        with zipfile.ZipFile(archive_path, "r") as archive:
            names = archive.namelist()
            if len(names) != 3 or set(names) != {"metadata.json", "RESTORE.txt", "library.db"}:
                raise OSError("The backup archive has unexpected contents.")
            info = archive.getinfo("library.db")
            if info.file_size != expected_size or info.compress_type != zipfile.ZIP_DEFLATED:
                raise OSError("The backup archive database entry is incomplete.")
            database_hash = hashlib.sha256()
            size = 0
            with archive.open("library.db", "r") as member:
                while True:
                    cancel_check()
                    block = member.read(_CHUNK)
                    if not block:
                        break
                    database_hash.update(block)
                    size += len(block)
                    time.sleep(_YIELD_SECONDS)
            if size != expected_size or database_hash.hexdigest() != snapshot_hash:
                raise OSError("The backup archive database failed its content check.")
            try:
                metadata_info = archive.getinfo("metadata.json")
                if metadata_info.file_size > 4096:
                    raise OSError("The backup archive metadata is too large.")
                with archive.open("metadata.json", "r") as metadata_member:
                    metadata = json.loads(metadata_member.read(4097))
            except (ValueError, UnicodeError, zipfile.BadZipFile) as exc:
                raise OSError("The backup archive metadata is invalid.") from exc
            if (metadata.get("format") != "bs-podcasts-library-backup"
                    or metadata.get("database_file") != "library.db"
                    or metadata.get("database_size") != expected_size):
                raise OSError("The backup archive metadata does not match its database.")

    @staticmethod
    def _snapshot_check(snapshot, cancelled, deadline, database):
        def progress():
            if BackupService._cancelled(cancelled) or database.maintenance_active:
                raise BackupCancelled()
            if time.monotonic() >= deadline:
                raise TimeoutError("The backup exceeded its 30 minute time limit.")

        with closing(sqlite3.connect(snapshot.resolve().as_uri() + "?mode=ro", uri=True, timeout=10.0)) as connection:
            connection.set_progress_handler(lambda: 1 if (BackupService._cancelled(cancelled)
                or database.maintenance_active or time.monotonic() >= deadline) else 0, 1000)
            try:
                result = connection.execute("PRAGMA quick_check").fetchone()
            except sqlite3.OperationalError as exc:
                progress()
                raise
            progress()
            if not result or result[0] != "ok":
                raise OSError("The SQLite snapshot did not pass quick_check.")

    def _owned_archives(self, cancel_check):
        self._check_path_chain(self.destination)
        owned = []
        for item in self.destination.iterdir():
            cancel_check()
            if not _ARCHIVE_NAME.fullmatch(item.name):
                continue
            try:
                info = item.lstat()
                marker = item.with_name(item.name + ".auto")
                marker_info = marker.lstat()
                pin = item.with_name(item.name + ".keep")
                try:
                    pin.lstat()
                    continue
                except FileNotFoundError:
                    pass
                if (not stat.S_ISREG(info.st_mode) or self._is_reparse(info) or info.st_nlink != 1
                        or not stat.S_ISREG(marker_info.st_mode) or self._is_reparse(marker_info)
                        or marker_info.st_nlink != 1 or marker_info.st_size > 256):
                    continue
                with marker.open("r", encoding="ascii") as handle:
                    parts = handle.read(257).split()
                if len(parts) != 4 or parts[:3] != ["1", str(info.st_size), str(info.st_mtime_ns)]:
                    continue
                digest = self._hash_file(item, cancel_check)
                if digest == parts[3] and re.fullmatch(r"[0-9a-f]{64}", digest):
                    after = item.lstat()
                    if (after.st_ino, after.st_dev, after.st_size, after.st_mtime_ns) == (
                            info.st_ino, info.st_dev, info.st_size, info.st_mtime_ns):
                        owned.append((info.st_mtime_ns, item, marker, info))
            except (OSError, UnicodeError, ValueError):
                continue
        return sorted(owned, reverse=True, key=lambda entry: (entry[0], entry[1].name))

    def _deletion_locations(self):
        root = self.destination.parent.parent
        stamp = r'\d{8}-\d{6}(?:-\d{6})?'
        return (
            (self.destination, _ARCHIVE_NAME),
            (self.destination.parent, re.compile(r'library-' + stamp + r'\.db\Z')),
            (root, re.compile(re.escape(self.database.path.name) +
                              r'\.pre-migration(?:-' + stamp + r')?\.bak\Z')),
        )

    @staticmethod
    def _file_signature(info):
        return (info.st_dev, info.st_ino, info.st_mode, info.st_nlink,
                info.st_size, info.st_mtime_ns, info.st_ctime_ns)

    def _deletion_candidate(self, path):
        """Recognize a managed file without reading its potentially large payload."""
        path = Path(path)
        if not any(path.parent == folder and pattern.fullmatch(path.name)
                   for folder, pattern in self._deletion_locations()):
            return None
        self._check_path_chain(path)
        info = path.lstat()
        if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1:
            return None
        try:
            path.with_name(path.name + '.keep').lstat()
            return None
        except FileNotFoundError:
            pass
        marker = path.with_name(path.name + '.auto')
        self._check_path_chain(marker)
        marked = marker.lstat()
        if not stat.S_ISREG(marked.st_mode) or marked.st_nlink != 1 or marked.st_size > 256:
            return None
        with marker.open('r', encoding='ascii') as handle:
            record = handle.read(257)
        parts = record.split()
        if parts[:3] != ['1', str(info.st_size), str(info.st_mtime_ns)]:
            return None
        if path.suffix == '.zip':
            if len(parts) != 4 or not re.fullmatch(r'[0-9a-f]{64}', parts[3]):
                return None
        elif len(parts) != 3:
            return None
        if (self._file_signature(path.lstat()) != self._file_signature(info)
                or self._file_signature(marker.lstat()) != self._file_signature(marked)):
            return None
        return BackupDeletion(path, info.st_size, self._file_signature(info),
                              self._file_signature(marked), record)

    def deletion_preview(self, cancelled=None):
        """Exact unprotected app-created files for an explicit user confirmation."""
        files = []
        self._lower_current_thread_priority()
        for folder, pattern in self._deletion_locations():
            if self._cancelled(cancelled):
                raise BackupCancelled()
            try:
                folder.lstat()
                self._check_path_chain(folder)
            except FileNotFoundError:
                continue
            for path in folder.iterdir():
                if self._cancelled(cancelled):
                    raise BackupCancelled()
                if not pattern.fullmatch(path.name):
                    continue
                try:
                    candidate = self._deletion_candidate(path)
                except (OSError, UnicodeError):
                    continue  # Protected/unmarked or concurrently removed.
                if candidate is not None:
                    files.append(candidate)
        return tuple(sorted(files, key=lambda item: str(item.path)))

    def delete_confirmed(self, previews, cancelled=None):
        """Delete only the previewed files whose identities still match."""
        result = {'deleted': 0, 'bytes': 0, 'skipped': 0, 'errors': [], 'cancelled': False}
        scheduled = self.database._scheduled_backup_lock
        retention = self.database._backup_retention_lock
        if not scheduled.acquire(blocking=False):
            raise OSError('A backup is running. Wait for it to finish, then try again.')
        held_retention = False
        try:
            held_retention = retention.acquire(blocking=False)
            if not held_retention or self.database.maintenance_active:
                raise OSError('Library maintenance is running. Wait for it to finish, then try again.')
            self._lower_current_thread_priority()
            deleted_names = set()
            for preview in dict.fromkeys(previews):
                if self._cancelled(cancelled):
                    result['cancelled'] = True
                    break
                try:
                    current = self._deletion_candidate(preview.path)
                    if current != preview:
                        result['skipped'] += 1
                        continue
                    preview.path.unlink()
                    result['deleted'] += 1
                    result['bytes'] += preview.bytes_reclaimed
                    deleted_names.add(preview.path.name)
                    marker = preview.path.with_name(preview.path.name + '.auto')
                    try:
                        if self._file_signature(marker.lstat()) == preview.marker_signature:
                            marker.unlink()
                    except FileNotFoundError:
                        pass
                    except OSError as exc:
                        result['errors'].append(f'{preview.path.name}: backup deleted, but its marker could not be removed: {exc}')
                except FileNotFoundError:
                    result['skipped'] += 1
                except (OSError, UnicodeError) as exc:
                    result['errors'].append(f'{preview.path.name}: {exc}')
            try:
                if self.repository.get_setting(_PREFIX + 'last_file', '') in deleted_names:
                    self._save_state('idle', fields={'last_success': '', 'last_file': '', 'last_size': ''})
                result['status'] = self.status()
            except Exception as exc:
                result['errors'].append(f'Could not update backup status: {exc}')
            return result
        finally:
            if held_retention:
                retention.release()
            scheduled.release()
            self.database.close_current_thread_connection()

    def _prune(self, keep, cancel_check):
        owned = self._owned_archives(cancel_check)
        victims = owned[keep:]
        removable = []
        for _mtime, path, marker, before in victims:
            cancel_check()
            # All candidates were fully checked before deleting anything.
            self._check_path_chain(path)
            current = path.lstat()
            marker_info = marker.lstat()
            pin = path.with_name(path.name + ".keep")
            if marker_info.st_size > 256:
                continue
            with marker.open("r", encoding="ascii") as handle:
                marker_record = handle.read(257).split()
            if (current.st_ino, current.st_dev, current.st_size, current.st_mtime_ns) != (
                    before.st_ino, before.st_dev, before.st_size, before.st_mtime_ns):
                continue
            if (not stat.S_ISREG(marker_info.st_mode) or self._is_reparse(marker_info)
                    or marker_info.st_nlink != 1
                    or marker_record[:3] != ["1", str(current.st_size), str(current.st_mtime_ns)]
                    or len(marker_record) != 4 or not re.fullmatch(r"[0-9a-f]{64}", marker_record[3])):
                continue
            try:
                pin.lstat()
                continue
            except FileNotFoundError:
                pass
            if path.parent.is_symlink() or self._is_reparse(path.parent.lstat()):
                continue
            removable.append((path, marker, current))
        # Cancellation is honored through the complete preflight. Once removal
        # starts, finish the pass without cancellation checks so a cancelled
        # run cannot leave a partially pruned rotation.
        for path, marker, before in removable:
            try:
                current = path.lstat()
                if (current.st_ino, current.st_dev, current.st_size, current.st_mtime_ns) != (
                        before.st_ino, before.st_dev, before.st_size, before.st_mtime_ns):
                    continue
                marker_info = marker.lstat()
                if (not stat.S_ISREG(marker_info.st_mode) or self._is_reparse(marker_info)
                        or marker_info.st_nlink != 1 or marker_info.st_size > 256):
                    continue
                with marker.open("r", encoding="ascii") as handle:
                    marker_record = handle.read(257).split()
                if (len(marker_record) != 4
                        or marker_record[:3] != ["1", str(current.st_size), str(current.st_mtime_ns)]
                        or not re.fullmatch(r"[0-9a-f]{64}", marker_record[3])):
                    continue
                try:
                    path.with_name(path.name + ".keep").lstat()
                    continue
                except FileNotFoundError:
                    pass
                path.unlink()
                marker.unlink()
            except OSError:
                continue
        if victims:
            sync_directory(self.destination)

    def run(self, force=False, cancelled=None):
        try:
            result = dict(self.status())
        except Exception as exc:
            self.database.close_current_thread_connection()
            return {"state": "error", "created": False, "cancelled": False,
                    "file": None, "size": None, "error": str(exc)[:500],
                    "skipped": None}
        result.update({"created": False, "cancelled": False, "file": None,
                       "size": None, "error": None, "skipped": None})
        if not self._run_lock.acquire(blocking=False):
            self.database.close_current_thread_connection()
            result.update(state="busy", skipped="already_running")
            return result
        database_lock = self.database._scheduled_backup_lock
        if not database_lock.acquire(blocking=False):
            self._run_lock.release()
            self.database.close_current_thread_connection()
            result.update(state="busy", skipped="already_running")
            return result
        snapshot = archive_temp = None
        try:
            self._lower_current_thread_priority()
            settings = self.status()
            result.update(settings)
            if not force and not settings["enabled"]:
                result.update(state="idle", skipped="disabled")
                return result
            if not force and not settings["due"]:
                result.update(state="idle", skipped="not_due")
                return result
            if self._cancelled(cancelled):
                self._save_state("cancelled")
                result.update(state="cancelled", cancelled=True)
                return result
            self._save_state("idle", "")
            deadline = time.monotonic() + _DEADLINE_SECONDS
            self._prepare_destination()
            db_size = self.database.snapshot_size_estimate()
            required = db_size * 2 + max(16 * 1024 * 1024, db_size // 10)
            free = shutil.disk_usage(self.destination).free
            if free < required:
                raise OSError(f"Not enough free space for a safe backup; about {required} bytes are needed.")
            stamp = datetime.now(timezone.utc).strftime("%Y%m%d-%H%M%S-%f")
            final = self.destination / f"library-auto-{stamp}.zip"
            descriptor, name = tempfile.mkstemp(prefix=".library-auto-", suffix=".snapshot.tmp", dir=self.destination)
            os.close(descriptor)
            snapshot = Path(name)
            descriptor, name = tempfile.mkstemp(prefix=".library-auto-", suffix=".zip.tmp", dir=self.destination)
            os.close(descriptor)
            archive_temp = Path(name)

            def cancel_check():
                self._check(cancelled, deadline)

            cancel_check()
            self.database.snapshot_to(snapshot, cancelled=lambda: self._cancelled(cancelled), deadline=deadline)
            cancel_check()
            snapshot_size = snapshot.stat().st_size
            zip_reserve = max(16 * 1024 * 1024, snapshot_size // 10)
            if shutil.disk_usage(self.destination).free < snapshot_size + zip_reserve:
                raise OSError("Not enough free space to finish the verified compressed backup; the previous copies were kept.")
            self._snapshot_check(snapshot, cancelled, deadline, self.database)
            created = datetime.now(timezone.utc)
            snapshot_hash, snapshot_size = self._write_zip(snapshot, archive_temp, created, cancel_check)
            cancel_check()
            self._verify(archive_temp, snapshot_hash, snapshot_size, cancel_check)
            archive_hash = self._hash_file(archive_temp, cancel_check)
            cancel_check()
            archive_info = archive_temp.lstat()
            if not stat.S_ISREG(archive_info.st_mode) or archive_info.st_nlink != 1:
                raise OSError("The new backup archive is not a private regular file.")
            if final.exists() or final.is_symlink():
                raise OSError("A backup with the generated name already exists.")
            os.replace(archive_temp, final)
            archive_temp = None
            sync_directory(self.destination)
            published = final.lstat()
            marker = final.with_name(final.name + ".auto")
            if marker.exists() or marker.is_symlink():
                raise OSError("A backup ownership marker already exists; the new archive is retained.")
            marker_content = f"1 {published.st_size} {published.st_mtime_ns} {archive_hash}".encode("ascii")
            atomic_write(marker, marker_content)
            finished = datetime.now(timezone.utc).isoformat(timespec="seconds")
            try:
                self._save_state("success", "", {
                    "last_success": finished,
                    "last_file": final.name,
                    "last_size": published.st_size,
                })
            except Exception:
                # A published and verified archive is still a success. Without
                # metadata confirmation, keep every older archive untouched.
                result.update(state="success", created=True, file=str(final), size=published.st_size,
                              error="Backup saved, but its status could not be recorded.")
                return result
            try:
                self._prune(settings["keep"], cancel_check)
            except Exception as exc:
                # Retention is optional cleanup after the verified publish;
                # a scan failure must never turn the new archive into failure.
                try:
                    self._save_state("success", f"Backup saved; older-copy cleanup was skipped: {exc}")
                except Exception:
                    pass
            result.update(self.status())
            result.update(state="success", created=True, file=str(final), size=published.st_size)
            return result
        except BackupCancelled:
            try:
                self._save_state("cancelled", "")
            except Exception:
                pass
            result.update(state="cancelled", cancelled=True, error=None)
            return result
        except InterruptedError as exc:
            if self._cancelled(cancelled) or self.database.maintenance_active:
                try:
                    self._save_state("cancelled", "")
                except Exception:
                    pass
                result.update(state="cancelled", cancelled=True, error=None)
                return result
            return self._failed(result, exc)
        except Exception as exc:
            return self._failed(result, exc)
        finally:
            for path in (snapshot, archive_temp):
                if path is not None:
                    try:
                        if path.is_file() and not path.is_symlink():
                            path.unlink()
                    except OSError:
                        pass
            self.database.close_current_thread_connection()
            database_lock.release()
            self._run_lock.release()

    def _failed(self, result, exc):
        message = str(exc).strip() or type(exc).__name__
        try:
            self._save_state("error", message)
        except Exception:
            pass
        result.update(state="error", error=message, cancelled=False)
        return result
