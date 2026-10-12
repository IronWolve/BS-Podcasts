"""Self-contained arm64 QtWidgets bundle with an explicitly reviewed player."""
import os
import sys
from pathlib import Path

ROOT = Path(os.environ['BS_PODCASTS_SOURCE_STAGE'])
SOURCE = ROOT / 'src'
# Excludes, payload filter, version and traceback choice are shared with the Windows spec.
sys.dont_write_bytecode = True
sys.path.insert(0, str(ROOT / 'packaging'))
from pyinstaller_payload import DISABLE_WINDOWED_TRACEBACK, EXCLUDES, dropped_payload, read_version  # noqa: E402

VERSION = read_version(ROOT)
IDENTITY = os.environ.get('BS_PODCASTS_SIGN_IDENTITY') or None
analysis = Analysis(
    [str(ROOT/'packaging/windows_launcher.py')], pathex=[str(SOURCE)],
    binaries=[(os.environ['BS_PODCASTS_LIBMPV_DYLIB'], '.')],
    datas=[(str(SOURCE/'bs_podcasts/assets'), 'bs_podcasts/assets'),
           (str(SOURCE/'bs_podcasts/data/migrations'), 'bs_podcasts/data/migrations'),
           (str(ROOT/'packaging/licenses'), 'licenses'),
           (str(ROOT/'packaging/THIRD-PARTY-NOTICES.txt'), '.')],
    hiddenimports=[], hookspath=[], hooksconfig={},
    runtime_hooks=[str(ROOT/'packaging/runtime_macos.py')],
    excludes=list(EXCLUDES),
    noarchive=False, optimize=1,
)
analysis.binaries = TOC(entry for entry in analysis.binaries if not dropped_payload(entry[0]))
analysis.datas = TOC(entry for entry in analysis.datas if not dropped_payload(entry[0]))
pyz = PYZ(analysis.pure)
exe = EXE(pyz, analysis.scripts, [], exclude_binaries=True, name='BS Podcasts',
          debug=False, bootloader_ignore_signals=False, strip=False, upx=False,
          console=False, disable_windowed_traceback=DISABLE_WINDOWED_TRACEBACK['macos'], argv_emulation=False,
          target_arch='arm64', codesign_identity=IDENTITY,
          entitlements_file=str(ROOT/'packaging/macos-entitlements.plist') if IDENTITY else None)
collect = COLLECT(exe, analysis.binaries, analysis.datas, strip=False, upx=False, name='BS Podcasts')
app = BUNDLE(collect, name='BS Podcasts.app', icon=os.environ['BS_PODCASTS_MAC_ICON'],
             bundle_identifier='com.bspodcasts.app', version=VERSION,
             info_plist={'CFBundleShortVersionString':VERSION, 'CFBundleVersion':VERSION,
                         'LSMinimumSystemVersion':'14.0', 'NSHighResolutionCapable':True})
