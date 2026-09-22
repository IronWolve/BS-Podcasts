"""Snapshot a data set into platform locations. Close destination app first.

    .venv/bin/python tools/migrate_data_dir.py /old/data-dir [--dry-run] [--force]

Originals are read-only. Media must be assembled below source/downloads;
artwork may be below source/artwork or source/cache/artwork. Local-import
file references remain local references; this does not copy arbitrary files
outside the supplied source. Existing different media files are never replaced.
"""
from pathlib import Path, PureWindowsPath
from tempfile import TemporaryDirectory
import hashlib
import json
import os
import shutil
import sqlite3
import sys
import time

from bs_podcasts.config import cache_dir, data_dir, default_downloads_dir
from bs_podcasts.data import Database
from bs_podcasts.data.lease import LibraryLease


def _copy_database(source: Path, destination: Path):
    origin = sqlite3.connect(source.resolve().as_uri() + "?mode=ro", uri=True)
    target = sqlite3.connect(destination)
    try:
        origin.backup(target)
    finally:
        target.close()
        origin.close()


def _fingerprint(path):
    result = []
    for candidate in (path, Path(str(path) + "-wal")):
        try:
            stat = candidate.stat()
            result.append(None if candidate != path and stat.st_size == 0 else (stat.st_size, stat.st_mtime_ns))
        except FileNotFoundError:
            result.append(None)
    return tuple(result)


def _digest(path):
    with path.open("rb") as handle:
        return hashlib.file_digest(handle, "sha256").hexdigest()


def _normal(value):
    return str(value).replace("\\", "/").rstrip("/")


def _copy_assets(source, destination, folders):
    mapping = {}
    for folder in folders:
        if not folder.is_dir():
            continue
        for item in folder.rglob("*"):
            if not item.is_file():
                continue
            if not item.resolve().is_relative_to(source):
                raise ValueError(f"Asset links outside the supplied source: {item}")
            relative = item.relative_to(folder)
            target = destination / relative
            target.parent.mkdir(parents=True, exist_ok=True)
            if target.exists() and _digest(target) != _digest(item):
                target = target.with_name(target.stem + "-" + _digest(item)[:12] + target.suffix)
            if target.exists():
                if _digest(target) != _digest(item):
                    raise ValueError(f"Asset collision: {target}")
            else:
                # Exclusive creation avoids replacing a file created during the copy.
                with item.open("rb") as reader, target.open("xb") as writer:
                    shutil.copyfileobj(reader, writer, 1024 * 1024)
            # Keep the asset root in the key; artwork/x and cache/artwork/x
            # are distinct source files even when their basenames agree.
            mapping[_normal(item.relative_to(source))] = str(target)
    return mapping


def _mapped(value, mapping):
    normalized = _normal(value)
    for relative in sorted(mapping, key=len, reverse=True):
        if normalized == relative or normalized.endswith("/" + relative):
            return mapping[relative]
    # Older/custom download folders may not carry a standard root name.
    # A unique suffix is usable; an ambiguous one must never silently guess.
    matches, longest = set(), 0
    for relative, target in mapping.items():
        parts = relative.split("/")
        for start in range(1, len(parts)):
            tail = "/".join(parts[start:])
            if normalized == tail or normalized.endswith("/" + tail):
                if len(tail) > longest:
                    matches, longest = set(), len(tail)
                if len(tail) == longest:
                    matches.add(target)
    if len(matches) > 1:
        raise ValueError(f"Ambiguous migrated asset reference: {value}")
    if matches:
        return matches.pop()
    return None


def migrate(source: Path, dry_run=False, force=False) -> dict:
    source = source.expanduser().resolve()
    targets = {"library": data_dir() / "library.db", "artwork": cache_dir() / "artwork",
               "downloads": default_downloads_dir()}
    target = targets["library"].resolve()
    plan = {"source": str(source), **{key: str(path) for key, path in targets.items()}}
    if dry_run:
        return plan
    source_db = source / "library.db"
    if not source_db.is_file():
        raise ValueError(f"No library.db in {source}")
    if source == target.parent or target.parent.is_relative_to(source) or source.is_relative_to(target.parent):
        raise ValueError("Source and destination data directories must not overlap.")
    with LibraryLease(target):
        original = _fingerprint(target)
        if target.exists():
            if not force:
                raise ValueError(f"{target} exists. Use --force only after reviewing --dry-run.")
            destination_time = max((item[1] for item in original if item), default=0)
            source_time = max((item[1] for item in _fingerprint(source_db) if item), default=0)
            if destination_time > source_time:
                raise ValueError("Destination library/WAL is newer than the source; refusing replacement.")
            backup = target.parent / "backups" / f"library-before-migration-{time.time_ns()}.db"
            backup.parent.mkdir(parents=True, exist_ok=True)
            _copy_database(target, backup)
            plan["backup"] = str(backup)
        with TemporaryDirectory(prefix=".bs-migration-", dir=target.parent) as temporary:
            staged = Path(temporary) / "library.db"
            _copy_database(source_db, staged)
            database = Database(staged)
            database.close()
            maps = {
                "artwork": _copy_assets(source, targets["artwork"], [source / "artwork", source / "cache/artwork"]),
                "downloads": _copy_assets(source, targets["downloads"], [source / "downloads"]),
            }
            connection = sqlite3.connect(staged)
            missing = []
            rewritten = 0
            try:
                with connection:
                    for table, column, kind in (
                        ("shows", "artwork_path", "artwork"), ("episodes", "episode_artwork_path", "artwork"),
                        ("episodes", "downloaded_path", "downloads"), ("downloads", "target_path", "downloads"),
                        ("downloads", "partial_path", "downloads"),
                    ):
                        rows = connection.execute(f"SELECT rowid,{column} FROM {table} WHERE {column}!=''").fetchall()
                        for rowid, value in rows:
                            mapped = _mapped(value, maps[kind])
                            if mapped is None and column == "downloaded_path":
                                missing.append(value)
                                continue
                            if mapped is None:
                                mapped = "" if kind == "artwork" else str(targets[kind] / PureWindowsPath(value).name)
                            connection.execute(f"UPDATE {table} SET {column}=? WHERE rowid=?", (mapped, rowid))
                            rewritten += 1
                    connection.execute("DELETE FROM settings WHERE key='downloads.directory'")
                    # Cleanup authority is not portable. Preserve the old list
                    # as inert evidence, never as executable cleanup intent.
                    connection.execute("INSERT OR REPLACE INTO settings(key,value) "
                                       "SELECT 'storage.orphans.retired',value FROM settings "
                                       "WHERE key='storage.orphans'")
                    connection.execute("DELETE FROM settings WHERE key IN "
                                       "('storage.orphans','storage.orphans.owner')")
                if missing:
                    raise ValueError("Downloaded media missing from source/downloads; library not replaced: " + "; ".join(missing[:8]))
                if connection.execute("PRAGMA quick_check").fetchone()[0] != "ok":
                    raise ValueError("Staged library failed integrity checking.")
            finally:
                connection.close()
            if _fingerprint(target) != original:
                raise ValueError("Destination changed during migration; library not replaced.")
            os.replace(staged, target)
            for suffix in ("-wal", "-shm"):
                Path(str(target) + suffix).unlink(missing_ok=True)
            plan.update(paths_rewritten=rewritten, copied={kind: len(values) for kind, values in maps.items()})
            manifest = target.parent / f"migration-{time.time_ns()}.json"
            manifest.write_text(json.dumps({"plan": plan, "copied_files": maps}, indent=2), encoding="utf-8")
            plan["manifest"] = str(manifest)
    return plan


if __name__ == "__main__":
    args = [arg for arg in sys.argv[1:] if not arg.startswith("--")]
    if not args:
        raise SystemExit(__doc__)
    try:
        result = migrate(Path(args[0]), "--dry-run" in sys.argv, "--force" in sys.argv)
    except (OSError, ValueError, sqlite3.Error) as exc:
        raise SystemExit(str(exc))
    for key, value in result.items():
        print(f"{key:16} {value}")
