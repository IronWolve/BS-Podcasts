"""Rules shared by the Windows and macOS PyInstaller specs.

Excluded modules, the payload filter, the version read and the windowed-traceback choice live here, so
each rule is written once. Both specs import this module from the staged packaging folder at spec time.
Standard library only.
"""
import re
import tomllib
from pathlib import Path

# Modules the bundle never imports. Both specs pass these to Analysis(excludes=...).
EXCLUDES = (
    "mutagen", "PIL", "Pillow", "setuptools", "pkg_resources", "_distutils_hack", "distutils",
    "PySide6.QtDBus", "PySide6.QtQml", "PySide6.QtQuick", "PySide6.QtPdf",
)
# Qt libraries a QtWidgets-only app never loads, matched as lower-case substrings of a payload name. The
# Windows and macOS spellings are both listed; each file name matches only its own platform's spelling.
UNUSED_BINARIES = (
    "qt6qml", "qtqml", "qt6quick", "qtquick", "qt6pdf", "qtpdf",
    "qt6virtualkeyboard", "qtvirtualkeyboard", "qtvirtualkeyboardplugin",
    "imageformats/qpdf", "imageformats/libqpdf", "opengl32sw",
)
# Whether an uncaught traceback in the windowed build is suppressed (True) or shown in a dialog (False).
# The owner has not chosen one value for both platforms, so this is the only place either value is set.
DISABLE_WINDOWED_TRACEBACK = {"windows": False, "macos": True}


def dropped_payload(name):
    """True for a bundle entry the app does not need. Separators are normalised before every test."""
    normalized = name.replace("\\", "/").lower()
    parts = normalized.split("/")
    if normalized.endswith(".qm") or "translations" in parts:
        return True
    if any(part == "pil" or part.startswith("pil.") or part == "pillow" or part.startswith("pillow.") for part in parts):
        return True
    if {"setuptools", "pkg_resources", "_distutils_hack", "distutils"}.intersection(parts):
        return True
    return any(marker in normalized for marker in UNUSED_BINARIES)


def app_modules(source):
    """Every module of the bs_podcasts package under a source folder, as dotted names. The build finds modules
    by reading imports, and the packages load some submodules by name at run time (feeds and playback defer
    theirs to first use), which that analysis cannot see. Listing them here puts every module in the bundle."""
    base = Path(source) / "bs_podcasts"
    names = []
    for path in sorted(base.rglob("*.py")):
        parts = path.relative_to(base.parent).with_suffix("").parts
        names.append(".".join(parts[:-1] if parts[-1] == "__init__" else parts))
    return names


def missing_app_modules(frozen_names, source):
    """The package modules under a source folder that a frozen archive's module table does not hold."""
    present = set(frozen_names)
    return [name for name in app_modules(source) if name not in present]


def read_version(root):
    """The project version from pyproject.toml. A missing or placeholder version stops the build."""
    try:
        version = tomllib.loads((Path(root) / "pyproject.toml").read_text(encoding="utf-8"))["project"]["version"]
    except (KeyError, TypeError, tomllib.TOMLDecodeError):
        version = ""
    if not isinstance(version, str) or not re.search(r"\d", version):
        # Never stamp a placeholder version into the executable.
        raise SystemExit("Could not read a version from pyproject.toml; refusing to build.")
    return version


def numeric_version(version):
    """The four-part version tuple of the release numbers. A pre-release suffix such as rc1 adds no number."""
    release = re.match(r"\d+(?:\.\d+)*", version)
    return tuple((([int(part) for part in release.group(0).split(".")] if release else []) + [0, 0, 0, 0])[:4])
