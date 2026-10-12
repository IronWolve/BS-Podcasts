"""Small dependency and migration self-check; no test discovery."""

import argparse
import codecs
import importlib.util
import json
import re
import struct
from pathlib import Path
from tempfile import TemporaryDirectory
import os
import sys
import time

from .data import Database
from .data.files import atomic_write
from .privacy import redact


_QUOTED_LOCAL_PATH = re.compile(r'''(['"])(?:[A-Za-z]:|~)?[\\/][^'"\r\n]*\1''')
_LOCAL_PATH = re.compile(r'''(?<![\w:/\\.])(?:[A-Za-z]:|~)?[\\/][^\s'"<>|]*''')


def _file_name(path):
    parts = [part for part in re.split(r'[\\/]', path) if part]
    return parts[-1] if parts else ''


def _shorten_local_paths(text):
    """Keep only the file name of each local path; folders above it can name the account."""
    text = _QUOTED_LOCAL_PATH.sub(lambda m: m.group(1) + _file_name(m.group(0)[1:-1]) + m.group(1), text)
    return _LOCAL_PATH.sub(lambda m: _file_name(m.group(0)), text)


def _exception_text(exc):
    return redact(_shorten_local_paths(f'{type(exc).__name__}: {exc}'))


def _note_dependency_failure(checks, errors, exc):
    """Record the exception under the generic flag; a check already false keeps its own result."""
    checks['dependency loading'] = False
    errors['dependency loading'] = _exception_text(exc)


_QT_APPLICATION = None  # kept for the life of the process; Qt needs it while fonts are registered


def _inter_registers() -> bool:
    """True when Qt registers the bundled Inter files and reports the Inter family.

    The font database needs a GUI application first: registering a font without
    one crashes the process (seen on Linux). No window is shown. On Linux with
    no display the offscreen platform is used, so the check runs headless."""
    global _QT_APPLICATION
    from PySide6.QtGui import QGuiApplication
    from .ui.theme import load_fonts
    if QGuiApplication.instance() is None:
        if sys.platform.startswith('linux') and not (os.environ.get('DISPLAY') or os.environ.get('WAYLAND_DISPLAY')):
            os.environ.setdefault('QT_QPA_PLATFORM', 'offscreen')
        _QT_APPLICATION = QGuiApplication([])
    return 'Inter' in load_fonts()


def _native_root():
    """The native folder next to the deployment record; the fixed package depth is the fallback."""
    here = Path(__file__).resolve()
    for folder in here.parents:
        if (folder / 'deployment.json').is_file():
            return folder / 'native'
    return here.parents[2] / 'native'


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


def _png_matches(path, size=None):
    """True for a PNG whose IHDR size is `size`, or any non-empty size when None. Reads the header only, no Qt."""
    try:
        with Path(path).open('rb') as handle:
            head = handle.read(24)
    except OSError:
        return False
    if len(head) < 24 or head[:8] != b'\x89PNG\r\n\x1a\n' or head[12:16] != b'IHDR':
        return False
    width, height = struct.unpack('>II', head[16:24])
    return (width, height) == size if size is not None else width > 0 and height > 0


def _painter(plain):
    """Colour only on a terminal; --plain, NO_COLOR or TERM=dumb turn it off.
    Windowed builds have no stdout at all, and print() then does nothing."""
    stream = sys.stdout
    colour = (stream is not None and not plain and not os.environ.get('NO_COLOR')
              and os.environ.get('TERM') != 'dumb' and stream.isatty())
    return lambda code, text: f'\033[{code}m{text}\033[0m' if colour else text


def _storage_checks(root, checks, errors):
    database = None
    try:
        database = Database(root / 'library.db')
        with database.connect() as connection:
            versions = [row['version'] for row in connection.execute(
                'SELECT version FROM schema_migrations ORDER BY version').fetchall()]
        checks['library database opens'] = True
        shipped = len(list((Path(__file__).parent / 'data/migrations').glob('[0-9]*.sql')))
        checks['migrations applied'] = bool(versions) and versions == list(range(1, len(versions) + 1)) and len(versions) == shipped
        previous_cache = os.environ.get('BS_PODCASTS_CACHE_DIR')
        try:
            import wave
            from .data.repositories import LibraryRepository
            from .feeds.local import LocalAudioImporter
            os.environ['BS_PODCASTS_CACHE_DIR'] = str(root / 'cache')
            audio = root / 'silent metadata check.wav'
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
    except Exception as exc:
        checks['library database opens'] = False
        errors['library database opens'] = _exception_text(exc)
    finally:
        if database is not None:
            try:
                database.close()
            except Exception as exc:
                checks['library database closes'] = False
                errors['library database closes'] = _exception_text(exc)


def main() -> int:
    started = time.monotonic()
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--self-check', action='store_true', help='accepted for launcher compatibility; has no effect')
    parser.add_argument('--self-check-dir', type=Path)
    parser.add_argument('--plain', action='store_true', help='no colour')
    args = parser.parse_args()
    from .config import cache_dir
    workspace_tmp = args.self_check_dir or Path(os.environ.get("TMPDIR") or cache_dir()/'selfcheck')
    workspace_tmp.mkdir(parents=True, exist_ok=True, mode=0o700)
    checks = {"Python >=3.14.7": sys.version_info[:3] >= (3,14,7)}
    checks['retired metadata dependency absent'] = importlib.util.find_spec('mutagen') is None
    errors = {}
    for encoding in ('gb2312', 'gbk', 'big5', 'shift_jis', 'euc_jp', 'euc_kr'):
        name = 'feed encoding ' + encoding
        try:
            codecs.lookup(encoding)
            checks[name] = True
        except (LookupError, ImportError, OSError) as exc:
            checks[name] = False
            errors[name] = _exception_text(exc)
    try:
        import PySide6
        import requests
        import urllib3
        import mpv
        from .assets import BRANDING_DIR, font_paths, logo_path
        # Exact files: icon_path() and shell_icon_path() fall back to the 512 px
        # icon, so a missing 256 or 64 px file would otherwise still pass here.
        # The About logo and the 512 px icon (rail mark) are read as PNG headers.
        checks.update({
            'PySide6 available': bool(PySide6.__version__),
            'requests available': bool(requests.__version__),
            'urllib3 patched': tuple(int(x) for x in urllib3.__version__.split('.')[:3]) >= (2,8,0),
            'native library loaded': bool(mpv.backend),
            'bundled branding/fonts': (BRANDING_DIR / 'bs-podcasts-icon-256.png').is_file() and len(font_paths()) >= 2,
            'bundled shell/tray icons': all((BRANDING_DIR / f'bs-podcasts-shell-icon-{size}.png').is_file() for size in (64, 256)),
            'bundled logo and 512 px icon': (_png_matches(logo_path())
                                             and _png_matches(BRANDING_DIR / 'bs-podcasts-icon-512.png', (512, 512))),
        })
        from .playback.engine import MpvEngine
        engine = MpvEngine(ao='null', vo='null')
        try:
            checks['native core initialized'] = bool(engine._player.mpv_version)
            if sys.platform.startswith('linux') or getattr(sys,'frozen',False):
                numeric = engine._player.ffmpeg_version.split('-')[0]
                checks['patched bundled FFmpeg'] = tuple(int(x) for x in numeric.split('.')) >= (9,0,2)
            if sys.platform.startswith('linux'):
                native_root = _native_root()
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
    # Counting the font files does not prove Qt accepts them; a rejected Inter
    # file would silently fall back to another family with different metrics.
    try:
        checks['bundled Inter registers with Qt'] = _inter_registers()
    except Exception as exc:
        checks['bundled Inter registers with Qt'] = False
        errors['bundled Inter registers with Qt'] = _exception_text(exc)
    try:
        with TemporaryDirectory(prefix="selfcheck-", dir=workspace_tmp) as temporary:
            _storage_checks(Path(temporary), checks, errors)
    except Exception as exc:
        checks['isolated storage checks'] = False
        errors['isolated storage checks'] = _exception_text(exc)

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
