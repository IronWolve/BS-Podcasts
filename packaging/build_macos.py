"""Build, check and transactionally publish a project-local macOS bundle."""
import fcntl
import json
import os
from pathlib import Path
import platform
import shutil
import subprocess
import sys
import tempfile
import tomllib

from source_manifest import ROOT, stage, source_digest, file_digest
from check_dependencies import check


def run(*args, **kwargs):
    return subprocess.run([str(a) for a in args], check=True, **kwargs)


def diagnostic(app, work):
    profile = Path(tempfile.mkdtemp(prefix='self-check-', dir=work))
    other = work/'Different Working Directory'
    other.mkdir(exist_ok=True)
    env = {k:v for k,v in os.environ.items() if k not in ('PYTHONPATH','PYTHONHOME') and not k.startswith('BS_PODCASTS_')}
    env.update(PATH='/usr/bin:/bin:/usr/sbin:/sbin', TMPDIR=str(profile),
               BS_PODCASTS_DATA_DIR=str(profile), BS_PODCASTS_CACHE_DIR=str(profile/'cache'),
               BS_PODCASTS_SILENT='1', QT_QPA_PLATFORM='offscreen')
    run(app/'Contents/MacOS/BS Podcasts', '--self-check', '--self-check-dir', profile,
        cwd=other, env=env, timeout=40)
    result=json.loads((profile/'self-check.json').read_text())
    if not result['passed']:
        raise RuntimeError('Frozen diagnostic failed; previous application unchanged.')
    return result


def build(workspace):
    if sys.platform != 'darwin' or platform.machine() != 'arm64':
        raise RuntimeError('This recipe requires an arm64 Mac; universal2 is not implied.')
    if len(sys.argv)>1:
        raise RuntimeError('Setup is a separate approved operation; this command only builds.')
    check(ROOT/'packaging/requirements-macos.lock')
    work=Path(tempfile.mkdtemp(prefix='macos-build-',dir=workspace/'tmp'))
    snapshot=stage(work/'source')
    version=tomllib.loads((snapshot/'pyproject.toml').read_text())['project']['version']
    native=workspace/'.cache/macos-build/libmpv-lgpl/libmpv.2.dylib'
    receipt=json.loads(native.with_name('native-build.json').read_text())
    if (receipt['sha256']!=file_digest(native)
            or receipt['inputs_sha256']!=file_digest(snapshot/'packaging/native-sources.json')
            or receipt.get('extra_inputs_sha256')!=file_digest(snapshot/'packaging/native-sources-macos.json')
            or receipt['recipe_sha256']!=file_digest(snapshot/'packaging/build-libmpv-macos.sh')):
        raise RuntimeError('Native receipt mismatch; rebuild the pinned LGPL player first.')
    iconset=work/'BS-Podcasts.iconset'; iconset.mkdir()
    for size in (16,32,128,256,512):
        for scale in (1,2):
            filename=f'icon_{size}x{size}'+('@2x' if scale==2 else '')+'.png'
            run('sips','-z',size*scale,size*scale,snapshot/'packaging/branding/bs-podcasts-icon-master.png',
                '--out',iconset/filename,stdout=subprocess.DEVNULL)
    icon=work/'BS-Podcasts.icns'
    run('iconutil','-c','icns',iconset,'-o',icon)
    os.environ.update(BS_PODCASTS_SOURCE_STAGE=str(snapshot), BS_PODCASTS_LIBMPV_DYLIB=str(native),
                      BS_PODCASTS_NATIVE_RECEIPT=str(native.with_name('native-build.json')),
                      BS_PODCASTS_MAC_ICON=str(icon), TMPDIR=str(work),
                      PYINSTALLER_CONFIG_DIR=str(workspace/'.cache/pyinstaller-macos'))
    run(sys.executable,'-B','-m','PyInstaller','--noconfirm','--clean','--distpath',work/'bundle',
        '--workpath',work/'work',snapshot/'packaging/bs-podcasts-macos.spec',cwd=work)
    app=work/'bundle/BS Podcasts.app'
    run('codesign','--verify','--deep','--strict',app)
    run(sys.executable,'-B',snapshot/'packaging/check_frozen.py',app,'--private-root',workspace)
    for path in app.rglob('*'):
        if not path.is_file() or path.is_symlink(): continue
        with path.open('rb') as stream: magic=stream.read(4)
        if magic not in (b'\xcf\xfa\xed\xfe',b'\xfe\xed\xfa\xcf',b'\xca\xfe\xba\xbe'): continue
        links=subprocess.check_output(['otool','-L',str(path)],text=True).splitlines()[1:]
        for line in links:
            dependency=line.strip().split(' (',1)[0]
            if not dependency.startswith(('@rpath/','@loader_path/','@executable_path/','/usr/lib/','/System/Library/')):
                raise RuntimeError('External bundle dependency: '+dependency)
    result=diagnostic(app,work)
    if result['version']!=version: raise RuntimeError('Frozen version mismatch.')
    relocated=work/'Relocated App With Spaces.app'
    shutil.copytree(app,relocated,symlinks=True)
    diagnostic(relocated,work)
    archive=work/f'BS-Podcasts-{version}-macOS-arm64.zip'
    run('ditto','-c','-k','--sequesterRsrc','--keepParent',app,archive)
    run('unzip','-tq',archive,stdout=subprocess.DEVNULL)
    manifest=work/'BS-Podcasts-macOS-arm64.manifest.json'
    run(sys.executable,'-B',snapshot/'packaging/build_manifest.py','--artifact',app,
        '--archive',archive,'--output',manifest)
    if source_digest(ROOT)!=source_digest(snapshot): raise RuntimeError('Source changed during build.')
    target=workspace/'dists/macos'; target.mkdir(parents=True,exist_ok=True)
    previous=workspace/'.cache/previous-deployments'/work.name; previous.mkdir(parents=True)
    paths=[(app,target/app.name),(archive,target/archive.name),(manifest,target/manifest.name)]
    retired=[]; published=[]
    try:
        for source,destination in paths:
            if destination.is_symlink(): raise RuntimeError('Refusing to replace artifact link.')
            if destination.exists():
                destination.rename(previous/destination.name); retired.append(destination)
            source.rename(destination); published.append((source,destination))
    except BaseException:
        for source,destination in reversed(published): destination.rename(source)
        for destination in reversed(retired): (previous/destination.name).rename(destination)
        raise
    print(f'Built and headless/relocation-verified BS Podcasts {version}: {target}',flush=True)
    print('Signing: '+('Developer ID; notarization still separate' if os.environ.get('BS_PODCASTS_SIGN_IDENTITY') else 'ad-hoc only; not notarized'))
    print(f'Previous artifacts retained: {previous}')


if __name__=='__main__':
    workspace=Path(os.environ.get('BS_PODCASTS_BUILD_ROOT',ROOT.parent)).resolve()
    with (workspace/'.cache/macos-build.lock').open('a+b') as lease:
        fcntl.flock(lease,fcntl.LOCK_EX|fcntl.LOCK_NB)
        build(workspace)
