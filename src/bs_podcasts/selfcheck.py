"""Small dependency and migration self-check; no test discovery."""

import argparse
import importlib.util
import json
from pathlib import Path
from tempfile import TemporaryDirectory
import ctypes.util
import os
import sys

from .data import Database
from .data.files import atomic_write

# Checks recorded inside the dependency try. A later exception must not hide one of these behind the generic flag.
_DEPENDENCY_CHECKS = (
    'PySide6 available',
    'requests available',
    'urllib3 patched',
    'native library loaded',
    'bundled branding/fonts',
    'native core initialized',
    'patched bundled FFmpeg',
    'native library inside deployment',
    'package inside bundle',
    'native library inside bundle',
)


def _exception_text(exc):
    return f'{type(exc).__name__}: {exc}'


def _note_dependency_failure(checks, errors, exc):
    """Keep a check that already recorded false; otherwise record the generic flag."""
    detail = _exception_text(exc)
    specific = [name for name in _DEPENDENCY_CHECKS if checks.get(name) is False]
    if specific:
        errors[specific[-1]] = detail
        return
    checks['dependency loading'] = False
    errors['dependency loading'] = detail


def _distribution_version():
    try:
        import importlib.metadata as metadata
        return metadata.version('bs-podcasts')
    except Exception:
        return None


def _pyproject_version_in_bundle():
    """pyproject version inside a frozen app, or None. A developer checkout is not read."""
    if not getattr(sys, 'frozen', False):
        return None
    roots = []
    meipass = getattr(sys, '_MEIPASS', None)
    if meipass:
        roots.append(Path(meipass).resolve())
    executable = Path(sys.executable).resolve().parent
    roots.append(executable)
    if sys.platform == 'darwin':
        roots.append(executable.parent)
        roots.append(executable.parent.parent)
    seen = set()
    for root in roots:
        if root in seen:
            continue
        seen.add(root)
        path = root / 'pyproject.toml'
        try:
            if not path.is_file():
                continue
            resolved = path.resolve()
            if not resolved.is_relative_to(root):
                continue
            import tomllib
            with resolved.open('rb') as handle:
                project = tomllib.load(handle).get('project') or {}
            version = project.get('version') if isinstance(project, dict) else None
        except (OSError, ValueError, TypeError, AttributeError):
            continue
        if isinstance(version, str) and version:
            return version
    return None


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--self-check', action='store_true')
    parser.add_argument('--self-check-dir', type=Path)
    args = parser.parse_args()
    from .config import cache_dir
    workspace_tmp = args.self_check_dir or Path(os.environ.get("TMPDIR") or cache_dir()/'selfcheck')
    workspace_tmp.mkdir(parents=True, exist_ok=True, mode=0o700)
    checks = {"Python >=3.14.7": sys.version_info[:3] >= (3,14,7)}
    checks['retired metadata dependency absent'] = importlib.util.find_spec('mutagen') is None
    errors = {}
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
            if sys.platform.startswith('linux') or getattr(sys,'frozen',False):
                numeric = engine._player.ffmpeg_version.split('-')[0]
                checks['patched bundled FFmpeg'] = tuple(int(x) for x in numeric.split('.')) >= (9,0,2)
            if sys.platform.startswith('linux'):
                native_root = Path(__file__).resolve().parents[2]/'native'
                checks['native library inside deployment'] = Path(mpv.backend._name).resolve().is_relative_to(native_root)
        finally:
            engine.shutdown()
        if getattr(sys,'frozen',False):
            bundle = Path(sys._MEIPASS).resolve()
            if sys.platform == 'darwin':
                bundle = Path(sys.executable).resolve().parent.parent
            checks['package inside bundle'] = Path(__file__).resolve().is_relative_to(bundle)
            checks['native library inside bundle'] = Path(mpv.backend._name).resolve().is_relative_to(bundle)
    except Exception as exc:
        _note_dependency_failure(checks, errors, exc)
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
        previous_cache = os.environ.get('BS_PODCASTS_CACHE_DIR')
        try:
            import wave
            from .data.repositories import LibraryRepository
            from .feeds.local import LocalAudioImporter
            os.environ['BS_PODCASTS_CACHE_DIR'] = str(root/'cache')
            audio = root/'silent metadata check.wav'
            with wave.open(str(audio), 'wb') as stream:
                stream.setparams((1, 2, 8000, 0, 'NONE', 'not compressed'))
                stream.writeframes(bytes(16000))
            repository = LibraryRepository(database)
            show = LocalAudioImporter(repository).import_file(audio)
            episode = repository.list_episodes(show.id)[0]
            checks['isolated local audio metadata'] = show.source == 'local' and episode.duration_seconds == 1
        except Exception:
            checks['isolated local audio metadata'] = False
        finally:
            if previous_cache is None:
                os.environ.pop('BS_PODCASTS_CACHE_DIR', None)
            else:
                os.environ['BS_PODCASTS_CACHE_DIR'] = previous_cache
            database.close()

    from . import __version__
    from .config import app_version
    package_version = _distribution_version()
    compared_version = _pyproject_version_in_bundle()
    if compared_version is None:
        compared_version = app_version()
    visible = package_version if package_version is not None else __version__
    checks['version agreement'] = visible == compared_version == __version__
    if not checks['version agreement']:
        errors['version agreement'] = f'package {visible}, compared {compared_version}, module {__version__}'
    failed = [name for name, passed in checks.items() if not passed]
    if args.self_check_dir:
        report = {
            'version': __version__,
            'package_version': visible,
            'compared_version': compared_version,
            'python': sys.version.split()[0],
            'checks': checks,
            'passed': not failed,
        }
        if errors:
            report['errors'] = errors
        atomic_write(workspace_tmp/'self-check.json', json.dumps(report, indent=2).encode())
    if failed:
        parts = [errors[name] for name in failed if name in errors]
        detail = " (" + "; ".join(parts) + ")" if parts else ""
        print("Self-check failed: " + ", ".join(failed) + detail)
        return 1
    print("BS Podcasts self-check passed.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
