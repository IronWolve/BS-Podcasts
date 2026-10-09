"""Self-contained arm64 QtWidgets bundle with an explicitly reviewed player."""
import os
from pathlib import Path
import tomllib

ROOT = Path(os.environ['BS_PODCASTS_SOURCE_STAGE'])
SOURCE = ROOT / 'src'
VERSION = tomllib.loads((ROOT/'pyproject.toml').read_text())['project']['version']
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
    excludes=['mutagen','PIL','Pillow','setuptools','pkg_resources','_distutils_hack','distutils',
              'PySide6.QtDBus','PySide6.QtQml','PySide6.QtQuick','PySide6.QtPdf'],
    noarchive=False, optimize=1,
)
UNUSED = ('qtqml','qtquick','qtpdf','qtvirtualkeyboard','qtvirtualkeyboardplugin','imageformats/qpdf','imageformats/libqpdf')

def _dropped_payload(name):
    normalized = name.replace('\\','/').lower()
    parts = normalized.split('/')
    if normalized.endswith('.qm') or 'translations' in parts:
        return True
    if any(part == 'pil' or part.startswith('pil.') or part == 'pillow' or part.startswith('pillow.') for part in parts):
        return True
    if any(part in {'setuptools', 'pkg_resources', '_distutils_hack', 'distutils'} for part in parts):
        return True
    return any(marker in normalized for marker in UNUSED)

analysis.binaries = TOC(entry for entry in analysis.binaries if not _dropped_payload(entry[0]))
analysis.datas = TOC(entry for entry in analysis.datas if not _dropped_payload(entry[0]))
pyz = PYZ(analysis.pure)
exe = EXE(pyz, analysis.scripts, [], exclude_binaries=True, name='BS Podcasts',
          debug=False, bootloader_ignore_signals=False, strip=False, upx=False,
          console=False, disable_windowed_traceback=True, argv_emulation=False,
          target_arch='arm64', codesign_identity=IDENTITY,
          entitlements_file=str(ROOT/'packaging/macos-entitlements.plist') if IDENTITY else None)
collect = COLLECT(exe, analysis.binaries, analysis.datas, strip=False, upx=False, name='BS Podcasts')
app = BUNDLE(collect, name='BS Podcasts.app', icon=os.environ['BS_PODCASTS_MAC_ICON'],
             bundle_identifier='com.bspodcasts.app', version=VERSION,
             info_plist={'CFBundleShortVersionString':VERSION, 'CFBundleVersion':VERSION,
                         'LSMinimumSystemVersion':'14.0', 'NSHighResolutionCapable':True})
