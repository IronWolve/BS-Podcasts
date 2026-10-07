"""Small dependency and migration self-check; no test discovery."""

import argparse
import importlib.util
import json
from pathlib import Path
from tempfile import TemporaryDirectory
import ctypes.util
import os
import sys
import time

from .data import Database
from .data.files import atomic_write
from .privacy import redact

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
    return redact(f'{type(exc).__name__}: {exc}')


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


def _painter(plain):
    """Colour only on a terminal; --plain, NO_COLOR or TERM=dumb turn it off.
    Windowed builds have no stdout at all, and print() then does nothing."""
    stream = sys.stdout
    colour = (stream is not None and not plain and not os.environ.get('NO_COLOR')
              and os.environ.get('TERM') != 'dumb' and stream.isatty())
    return lambda code, text: f'\033[{code}m{text}\033[0m' if colour else text


def main() -> int:
    started = time.monotonic()
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--self-check', action='store_true')
    parser.add_argument('--self-check-dir', type=Path)
    parser.add_argument('--plain', action='store_true', help='no colour')
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
        from .assets import icon_path, shell_icon_path, font_paths
        checks.update({
            'PySide6 available': bool(PySide6.__version__),
            'requests available': bool(requests.__version__),
            'urllib3 patched': tuple(int(x) for x in urllib3.__version__.split('.')[:3]) >= (2,8,0),
            'native library loaded': bool(mpv.backend),
            'bundled branding/fonts': icon_path().is_file() and len(font_paths()) >= 2,
            'bundled shell/tray icons': all(shell_icon_path(size).is_file() for size in (64, 256)),
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
        except Exception as exc:
            checks['isolated local audio metadata'] = False
            errors['isolated local audio metadata'] = _exception_text(exc)
        finally:
            if previous_cache is None:
                os.environ.pop('BS_PODCASTS_CACHE_DIR', None)
            else:
                os.environ['BS_PODCASTS_CACHE_DIR'] = previous_cache
            database.close()

    from . import __version__
    package_version = _distribution_version()
    compared_version = _pyproject_version_in_bundle()
    # Agreement needs an independent source; never compare the module constant with itself.
    # Builds compare the reported version with pyproject.toml where no source ships.
    sources = [value for value in (package_version, compared_version) if value is not None]
    version_agreement = 'not available'
    if sources:
        checks['version agreement'] = all(value == __version__ for value in sources)
        version_agreement = 'checked'
        if not checks['version agreement']:
            errors['version agreement'] = f'package {package_version}, compared {compared_version}, module {__version__}'
    failed = [name for name, passed in checks.items() if not passed]
    if args.self_check_dir:
        report = {
            'version': __version__,
            'version_agreement': version_agreement,
            'package_version': package_version,
            'compared_version': compared_version,
            'python': sys.version.split()[0],
            'checks': checks,
            'passed': not failed,
        }
        if errors:
            report['errors'] = errors
        atomic_write(workspace_tmp/'self-check.json', json.dumps(report, indent=2).encode())
    colour = _painter(args.plain)
    for name, passed in checks.items():
        reason = f": {errors[name]}" if not passed and name in errors else ""
        print((colour('32', 'OK  ') if passed else colour('31', 'FAIL')) + f" {name}{reason}")
    if failed:
        parts = [errors[name] for name in failed if name in errors]
        detail = " (" + "; ".join(parts) + ")" if parts else ""
        print(colour('31', "Self-check failed: ") + ", ".join(failed) + detail)
    else:
        print(colour('32', "BS Podcasts self-check passed.")
              + (" Version agreement: not available in this runtime." if not sources else ""))
    print(colour('1', 'Stats'))
    for label, value in (('checks', f'{len(checks) - len(failed)} passed, {len(failed)} failed'),
                         ('version', f'{__version__} (agreement {version_agreement})'),
                         ('elapsed', f'{time.monotonic() - started:.1f} s')):
        print(f'  {label:<8} {value}')
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
