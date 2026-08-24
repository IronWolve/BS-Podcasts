"""PyInstaller recipe for a single-file Windows BS Podcasts executable."""

import os
from pathlib import Path

from PyInstaller.utils.hooks import copy_metadata


ROOT = Path(SPECPATH).parent
SOURCE = ROOT / "src"
LIBMPV = Path(os.environ["BS_PODCASTS_LIBMPV_DLL"])

if not LIBMPV.is_file():
    raise FileNotFoundError(f"Bundled libmpv DLL not found: {LIBMPV}")

datas = [
    (str(SOURCE / "bs_podcasts" / "assets"), "bs_podcasts/assets"),
    (str(SOURCE / "bs_podcasts" / "data" / "migrations"), "bs_podcasts/data/migrations"),
    *copy_metadata("bs-podcasts"),
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
    excludes=["PySide6.QtDBus"],
    noarchive=False,
    optimize=1,
)

pyz = PYZ(analysis.pure)

exe = EXE(
    pyz,
    analysis.scripts,
    analysis.binaries,
    analysis.datas,
    [],
    name="BS Podcasts",
    icon=str(SOURCE / "bs_podcasts" / "assets" / "branding" / "bs-podcasts-icon-master.png"),
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
