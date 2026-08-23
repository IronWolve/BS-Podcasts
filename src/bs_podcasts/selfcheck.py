"""Small dependency, config, and migration self-check; no test discovery."""

from importlib.metadata import version
from pathlib import Path
from tempfile import TemporaryDirectory
import ctypes.util
import os
import sys

from .config import AppSettings
from .data import Database


def main() -> int:
    workspace_tmp = Path(os.environ.get("TMPDIR", Path.cwd() / "tmp"))
    workspace_tmp.mkdir(parents=True, exist_ok=True)
    checks = {
        "Python 3.14": sys.version_info[:2] == (3, 14),
        "PySide6 installed": bool(version("PySide6")),
        "python-mpv installed": bool(version("python-mpv")),
        "libmpv available": bool(ctypes.util.find_library("mpv")),
    }
    with TemporaryDirectory(prefix="selfcheck-", dir=workspace_tmp) as temporary:
        root = Path(temporary)
        database = Database(root / "library.db")
        with database.connect() as connection:
            versions = [
                row["version"]
                for row in connection.execute(
                    "SELECT version FROM schema_migrations ORDER BY version"
                ).fetchall()
            ]
        checks["migrations 1-4"] = versions == [1, 2, 3, 4]
        corrupt = root / "config.json"
        corrupt.write_text("{not valid json", encoding="utf-8")
        checks["corrupt config fallback"] = AppSettings.load(corrupt).recovered_from_error
        settings = AppSettings()
        settings.save(corrupt)
        checks["atomic config save"] = AppSettings.load(corrupt).values == settings.values

    failed = [name for name, passed in checks.items() if not passed]
    if failed:
        print("Self-check failed: " + ", ".join(failed))
        return 1
    print("BS Podcasts self-check passed.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
