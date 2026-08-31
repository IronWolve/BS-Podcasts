"""Copy a BS Podcasts data set into the current platform locations and rewrite
the absolute paths stored in the database. Originals are never modified.

    .venv/bin/python tools/migrate_data_dir.py /old/data-dir [--dry-run]
"""

from pathlib import Path
import shutil
import sqlite3
import sys
import time

from bs_podcasts.config import cache_dir, data_dir, default_downloads_dir


def _rewrite(connection, table, column, old_root: Path, new_root: Path) -> int:
    rows = connection.execute(f"SELECT rowid, {column} FROM {table} WHERE {column} != ''").fetchall()
    changed = 0
    for rowid, value in rows:
        try:
            relative = Path(value).relative_to(old_root)
        except ValueError:
            continue
        connection.execute(f"UPDATE {table} SET {column}=? WHERE rowid=?", (str(new_root / relative), rowid))
        changed += 1
    return changed


def _stored_root(connection, sql: str):
    row = connection.execute(sql + " LIMIT 1").fetchone()
    return Path(row[0]).parent if row and row[0] else None


def _copy_database(source: Path, destination: Path):
    """Snapshot via SQLite so an open source cannot yield a torn file."""
    origin = sqlite3.connect(source)
    target = sqlite3.connect(destination)
    try:
        origin.backup(target)
    finally:
        target.close()
        origin.close()


def migrate(source: Path, dry_run: bool = False, force: bool = False) -> dict:
    source = source.expanduser().resolve()
    targets = {
        "library": data_dir() / "library.db",
        "artwork": cache_dir() / "artwork",
        "downloads": default_downloads_dir(),
    }
    plan = {"source": str(source), **{key: str(path) for key, path in targets.items()}}
    if dry_run:
        return plan
    if not (source / "library.db").is_file():
        raise SystemExit(f"No library.db in {source}")
    for path in (data_dir(), targets["artwork"], targets["downloads"]):
        path.mkdir(parents=True, exist_ok=True)
    if targets["library"].exists():
        # Overwriting unconditionally meant a second run replaced an
        # already-migrated destination — one that may have collected new
        # subscriptions and progress since — with a stale copy of the source,
        # and a third run overwrote the .bak too. Require an explicit
        # --force, and refuse outright when the destination is newer.
        destination_mtime = targets["library"].stat().st_mtime
        source_mtime = (source / "library.db").stat().st_mtime
        if not force:
            raise SystemExit(
                f"{targets['library']} already exists (modified "
                f"{time.strftime('%Y-%m-%d %H:%M', time.localtime(destination_mtime))}).\n"
                "Re-run with --force to replace it, or --dry-run to see the plan."
            )
        if destination_mtime > source_mtime:
            raise SystemExit(
                "Refusing to overwrite: the destination library is NEWER than the source.\n"
                f"  destination {time.strftime('%Y-%m-%d %H:%M', time.localtime(destination_mtime))}\n"
                f"  source      {time.strftime('%Y-%m-%d %H:%M', time.localtime(source_mtime))}\n"
                "Copy it aside yourself if you really mean to go backwards."
            )
        backup = targets["library"].with_suffix(f".db.{int(destination_mtime)}.bak")
        shutil.copy2(targets["library"], backup)
        plan["backup"] = str(backup)
    # Connection.backup(), not copy2: the source may be open elsewhere, and a
    # WAL-less file copy can capture a torn snapshot. design.md requires this
    # for repair snapshots; the same reasoning applies to a migration.
    _copy_database(source / "library.db", targets["library"])
    # No sidecar copy: Connection.backup() produces a self-contained file, and
    # carrying a -wal from a different database would corrupt it.
    copied = {"artwork": 0, "downloads": 0}
    for key in ("artwork", "downloads"):
        folder = source / key
        if folder.is_dir():
            for item in folder.iterdir():
                if item.is_file():
                    destination = targets[key] / item.name
                    if not destination.exists():
                        shutil.copy2(item, destination)
                    copied[key] += 1
    from bs_podcasts.data import Database

    Database(targets["library"])  # bring the copy up to the current schema first
    connection = sqlite3.connect(targets["library"])
    with connection:
        # The database stores absolute paths; take the old roots from what it
        # actually contains rather than assuming they match `source`.
        old_art = _stored_root(connection, "SELECT artwork_path FROM shows WHERE artwork_path != ''") or (source / "artwork")
        old_dl = _stored_root(connection, "SELECT downloaded_path FROM episodes WHERE downloaded_path != ''") or (source / "downloads")
        plan["old_artwork_root"] = str(old_art)
        plan["old_downloads_root"] = str(old_dl)
        rewritten = 0
        rewritten += _rewrite(connection, "shows", "artwork_path", old_art, targets["artwork"])
        rewritten += _rewrite(connection, "episodes", "episode_artwork_path", old_art, targets["artwork"])
        rewritten += _rewrite(connection, "episodes", "downloaded_path", old_dl, targets["downloads"])
        rewritten += _rewrite(connection, "downloads", "target_path", old_dl, targets["downloads"])
        rewritten += _rewrite(connection, "downloads", "partial_path", old_dl, targets["downloads"])
        connection.execute("DELETE FROM settings WHERE key='downloads.directory'")
    connection.close()
    plan.update({"copied": copied, "paths_rewritten": rewritten})
    return plan


def main() -> int:
    args = [arg for arg in sys.argv[1:] if not arg.startswith("--")]
    if not args:
        print(__doc__)
        return 2
    result = migrate(Path(args[0]), dry_run="--dry-run" in sys.argv,
                     force="--force" in sys.argv)
    for key, value in result.items():
        print(f"{key:16} {value}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
