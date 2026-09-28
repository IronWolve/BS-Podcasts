"""PyInstaller recipe for the Windows BS Podcasts build.

Builds a onedir application (dist/BS Podcasts/). A onefile exe must extract
its whole payload to %TEMP% on every launch — several seconds plus an
antivirus rescan before anything runs — while onedir loads DLLs in place and
starts near-instantly, as a single process.
"""

import os
import re
from pathlib import Path

ROOT = Path(os.environ["BS_PODCASTS_SOURCE_STAGE"])
SOURCE = ROOT / "src"
LIBMPV = Path(os.environ["BS_PODCASTS_LIBMPV_DLL"])

if not LIBMPV.is_file():
    raise FileNotFoundError(f"Bundled libmpv DLL not found: {LIBMPV}")

VERSION = "0.1.0"
match = re.search(r'^version\s*=\s*"([^"]+)"', (ROOT / "pyproject.toml").read_text(encoding="utf-8"), re.M)
if match:
    VERSION = match.group(1)
NUMERIC = tuple((list(map(int, re.findall(r"\d+", VERSION))) + [0, 0, 0, 0])[:4])

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
    excludes=[
        "mutagen",
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
    "imageformats\\qpdf",
    "opengl32sw",
)
analysis.binaries = TOC(
    entry for entry in analysis.binaries
    if not any(marker in entry[0].lower() for marker in _UNUSED_BINARIES)
)

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
