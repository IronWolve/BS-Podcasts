"""PyInstaller recipe for the Windows BS Podcasts build.

Builds a onedir application (dist/BS Podcasts/). A onefile exe must extract
its whole payload to %TEMP% on every launch — several seconds plus an
antivirus rescan before anything runs — while onedir loads DLLs in place and
starts near-instantly, as a single process.
"""

import os
import sys
from pathlib import Path

ROOT = Path(os.environ["BS_PODCASTS_SOURCE_STAGE"])
SOURCE = ROOT / "src"
LIBMPV = Path(os.environ["BS_PODCASTS_LIBMPV_DLL"])
# Excludes, payload filter, version and traceback choice are shared with the macOS spec.
sys.dont_write_bytecode = True
sys.path.insert(0, str(ROOT / "packaging"))
from pyinstaller_payload import DISABLE_WINDOWED_TRACEBACK, EXCLUDES, dropped_payload, numeric_version, read_version  # noqa: E402

if not LIBMPV.is_file():
    raise FileNotFoundError(f"Bundled libmpv DLL not found: {LIBMPV}")

VERSION = read_version(ROOT)
NUMERIC = numeric_version(VERSION)

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
    excludes=list(EXCLUDES),
    noarchive=False,
    optimize=1,
)

analysis.binaries = TOC(entry for entry in analysis.binaries if not dropped_payload(entry[0]))
analysis.datas = TOC(entry for entry in analysis.datas if not dropped_payload(entry[0]))

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
    disable_windowed_traceback=DISABLE_WINDOWED_TRACEBACK["windows"],
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
