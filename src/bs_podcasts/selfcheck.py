"""Small dependency and migration self-check; no test discovery."""

import argparse
import json
from pathlib import Path
from tempfile import TemporaryDirectory
import ctypes.util
import os
import sys

from .data import Database
from .data.files import atomic_write


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--self-check', action='store_true')
    parser.add_argument('--self-check-dir', type=Path)
    args = parser.parse_args()
    from .config import cache_dir
    workspace_tmp = args.self_check_dir or Path(os.environ.get("TMPDIR") or cache_dir()/'selfcheck')
    workspace_tmp.mkdir(parents=True, exist_ok=True, mode=0o700)
    checks = {"Python >=3.14.7": sys.version_info[:3] >= (3,14,7)}
    try:
        import PySide6
        import requests
        import urllib3
        import mpv
        from .assets import icon_path, font_paths
        checks.update({
            'PySide6 available': bool(PySide6.__version__),
            'requests available': bool(requests.__version__),
            'urllib3 patched': tuple(int(x) for x in urllib3.__version__.split('.')[:3]) >= (2,8,0),
            'native library loaded': bool(mpv.backend),
            'bundled branding/fonts': icon_path().is_file() and len(font_paths()) >= 2,
        })
        from .playback.engine import MpvEngine
        engine = MpvEngine(ao='null', vo='null')
        try:
            checks['native core initialized'] = bool(engine._player.mpv_version)
            if sys.platform == 'win32' and getattr(sys,'frozen',False):
                numeric = engine._player.ffmpeg_version.split('-')[0]
                checks['patched bundled FFmpeg'] = tuple(int(x) for x in numeric.split('.')) >= (9,0,2)
        finally:
            engine.shutdown()
        if getattr(sys,'frozen',False):
            bundle = Path(sys._MEIPASS).resolve()
            checks['package inside bundle'] = Path(__file__).resolve().is_relative_to(bundle)
            checks['native library inside bundle'] = Path(mpv.backend._name).resolve().is_relative_to(bundle)
    except Exception:
        checks['dependency loading'] = False
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
        # Gapless from 1 and matching what ships, never a pinned count: the
        # hardcoded "1-11" broke the moment migration 012 landed, failing
        # every healthy install — the same bug class the audit smoke had.
        shipped = len(list((Path(__file__).parent / "data" / "migrations").glob("[0-9]*.sql")))
        checks["migrations applied"] = bool(versions) and versions == list(
            range(1, len(versions) + 1)
        ) and len(versions) == shipped
        database.close()

    failed = [name for name, passed in checks.items() if not passed]
    if args.self_check_dir:
        from . import __version__
        atomic_write(workspace_tmp/'self-check.json', json.dumps({
            'version':__version__,'python':sys.version.split()[0],
            'checks':checks,'passed':not failed},indent=2).encode())
    if failed:
        print("Self-check failed: " + ", ".join(failed))
        return 1
    print("BS Podcasts self-check passed.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
