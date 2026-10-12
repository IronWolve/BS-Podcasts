"""PyInstaller recipe for the Windows BS Podcasts build.

Builds a onedir application (dist/BS Podcasts/). A onefile exe must extract
its whole payload to %TEMP% on every launch — several seconds plus an
antivirus rescan before anything runs — while onedir loads DLLs in place and
starts near-instantly, as a single process.
"""

import os
import re
import tomllib
from pathlib import Path

ROOT = Path(os.environ["BS_PODCASTS_SOURCE_STAGE"])
SOURCE = ROOT / "src"
LIBMPV = Path(os.environ["BS_PODCASTS_LIBMPV_DLL"])

if not LIBMPV.is_file():
    raise FileNotFoundError(f"Bundled libmpv DLL not found: {LIBMPV}")

try:
    VERSION = tomllib.loads((ROOT / "pyproject.toml").read_text(encoding="utf-8"))["project"]["version"]
except (KeyError, TypeError, tomllib.TOMLDecodeError):
    VERSION = ""
if not isinstance(VERSION, str) or not re.search(r"\d", VERSION):
    # Never stamp a placeholder version into the executable.
    raise SystemExit("Could not read a version from pyproject.toml; refusing to build.")
# The release numbers only: a pre-release suffix such as rc1 must not add a number to the version resource.
RELEASE = re.match(r"\d+(?:\.\d+)*", VERSION)
NUMERIC = tuple((([int(part) for part in RELEASE.group(0).split(".")] if RELEASE else []) + [0, 0, 0, 0])[:4])

# Identify the process as BS Podcasts (not the Python bootloader) in Task
# Manager and file properties.
VERSION_FILE = Path(os.environ["BS_PODCASTS_BUILD_WORK"]) / "windows-version-info.txt"
VERSION_FILE.parent.mkdir(parents=True, exist_ok=True)
VERSION_FILE.write_text(
    f"""VSVersionInfo(
  ffi=FixedFileInfo(filevers={NUMERIC}, prodvers={NUMERIC}, mask=0x3f, flags=0x0,
                    OS=0x40004, fileType=0x1, subtype=0x0, date=(0, 0)),
  kids=[
    StringFileInfo([StringTable('040904B0', [
      StringStruct('ProductName', 'BS Podcasts'),
      StringStruct('FileDescription', 'BS Podcasts'),
      StringStruct('FileVersion', '{VERSION}'),
      StringStruct('ProductVersion', '{VERSION}'),
      StringStruct('OriginalFilename', 'BS Podcasts.exe'),
      StringStruct('LegalCopyright', ''),
    ])]),
    VarFileInfo([VarStruct('Translation', [1033, 1200])]),
  ],
)
""",
    encoding="utf-8",
)

datas = [
    (str(SOURCE / "bs_podcasts" / "assets"), "bs_podcasts/assets"),
    (str(SOURCE / "bs_podcasts" / "data" / "migrations"), "bs_podcasts/data/migrations"),
    (str(ROOT / "packaging" / "licenses"), "licenses"),
    (str(ROOT / "packaging" / "THIRD-PARTY-NOTICES.txt"), "."),
]

analysis = Analysis(
    [str(ROOT / "packaging" / "windows_launcher.py")],
    pathex=[str(SOURCE)],
    binaries=[(str(LIBMPV), ".")],
    datas=datas,
    hiddenimports=[],
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[str(ROOT / "packaging" / "runtime_windows.py")],
    # The app never imports setuptools. It only arrived through urllib3's optional
    # backports.zstd import (Python 3.14 has compression.zstd) plus its own runtime
    # hook. The packaged self-check below the build still gates publication.
    excludes=[
        "mutagen",
        "PIL",
        "Pillow",
        "setuptools",
        "pkg_resources",
        "_distutils_hack",
        "distutils",
        "PySide6.QtDBus",
        "PySide6.QtQml",
        "PySide6.QtQuick",
        "PySide6.QtPdf",
    ],
    noarchive=False,
    optimize=1,
)

# Qt libraries the PySide6 hook drags in through plugin dependencies but that a
# QtWidgets-only app never loads: the Qml/Quick stack and the virtual keyboard
# that needs it, PDF rendering (and its imageformat plugin), and the ~20 MB
# software-OpenGL rasterizer. Dropping them shrinks what ships and installs.
_UNUSED_BINARIES = (
    "qt6qml",
    "qt6quick",
    "qt6pdf",
    "qt6virtualkeyboard",
    "qtvirtualkeyboardplugin",
    "imageformats/qpdf",
    "opengl32sw",
)

# In-app badge copies the app never loads: only the 512 px badge ships (DQ01).
_UNSHIPPED_BADGES = tuple(f"assets/branding/bs-podcasts-icon-{size}.png" for size in (32, 64, 128, 256))

def _dropped_payload(name):
    # Separators are normalised before every test, so a backslash path and a slash path are judged alike.
    normalized = name.replace("\\", "/").lower()
    parts = normalized.split("/")
    if normalized.endswith(".qm") or "translations" in parts:
        return True
    if normalized.endswith(_UNSHIPPED_BADGES):
        return True
    if any(part == "pil" or part.startswith("pil.") or part == "pillow" or part.startswith("pillow.") for part in parts):
        return True
    if {"setuptools", "pkg_resources", "_distutils_hack", "distutils"}.intersection(parts):
        return True
    return any(marker in normalized for marker in _UNUSED_BINARIES)

analysis.binaries = TOC(entry for entry in analysis.binaries if not _dropped_payload(entry[0]))
analysis.datas = TOC(entry for entry in analysis.datas if not _dropped_payload(entry[0]))

pyz = PYZ(analysis.pure)

exe = EXE(
    pyz,
    analysis.scripts,
    [],
    exclude_binaries=True,
    name="BS Podcasts",
    icon=str(SOURCE.parent / "packaging" / "branding" / "bs-podcasts-icon-master.png"),
    version=str(VERSION_FILE),
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=False,
    console=False,
    # Traceback dialog choice, recorded per platform: False here shows an uncaught traceback in the windowed build;
    # macOS suppresses it (True). The owner has not chosen one value for both.
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
)

collect = COLLECT(
    exe,
    analysis.binaries,
    analysis.datas,
    strip=False,
    upx=False,
    name="BS Podcasts",
)
