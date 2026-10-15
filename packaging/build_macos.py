"""Build, check and transactionally publish a project-local macOS bundle."""
import argparse
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

from source_manifest import ROOT, stage, source_digest, file_digest, check as check_source
from check_dependencies import check
from report import Reporter, size_text, tree_size
from build_manifest import require_keys, write_build_info
from prune_helper import confirm_and_prune


# Mac rollbacks are named by mkdtemp with this prefix. The shared folder also
# holds other platforms' rollbacks and staging, which this build never prunes.
ROLLBACK_PREFIX = 'macos-build-'


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


def build(workspace, prune=False, report=None):
    if sys.platform != 'darwin' or platform.machine() != 'arm64':
        raise RuntimeError('This recipe requires an arm64 Mac; universal2 is not implied.')
    check(ROOT/'packaging/requirements-macos.lock')
    work=Path(tempfile.mkdtemp(prefix=ROLLBACK_PREFIX,dir=workspace/'tmp'))
    replaced=False
    try:
        replaced=_finish_macos(workspace, work, prune, report or Reporter())
    finally:
        source=work/'source'
        if source.exists():
            shutil.rmtree(source)
        if (replaced and work.exists() and not work.is_symlink()
                and work.name.startswith(ROLLBACK_PREFIX) and work.is_relative_to(workspace/'tmp')):
            shutil.rmtree(work)


def _finish_macos(workspace, work, prune, report):
    snapshot=stage(work/'source')
    version=tomllib.loads((snapshot/'pyproject.toml').read_text())['project']['version']
    native=workspace/'.cache/macos-build/libmpv-lgpl/libmpv.2.dylib'
    try:
        receipt=json.loads(native.with_name('native-build.json').read_text())
    except OSError as exc:
        raise ValueError(f'macOS native receipt cannot be read ({exc.strerror or exc}); rebuild the pinned LGPL player.') from None
    require_keys(receipt, ('sha256', 'inputs_sha256', 'extra_inputs_sha256', 'patch_sha256', 'recipe_sha256',
                           'minimum_macos'), 'macOS native receipt')
    if (receipt['sha256']!=file_digest(native)
            or receipt['inputs_sha256']!=file_digest(snapshot/'packaging/native-sources.json')
            or receipt.get('extra_inputs_sha256')!=file_digest(snapshot/'packaging/native-sources-macos.json')
            or receipt.get('patch_sha256')!=file_digest(snapshot/'packaging/mpv-coreaudio-only.patch')
            or receipt['recipe_sha256']!=file_digest(snapshot/'packaging/build-libmpv-macos.sh')):
        raise RuntimeError('Native receipt mismatch; rebuild the pinned LGPL player first.')
    write_build_info(snapshot, receipt['sha256'])
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
    for path in app.rglob('*'):
        if path.is_symlink() and (not path.exists() or not path.resolve().is_relative_to(app.resolve())):
            raise RuntimeError('Broken or escaping bundle link: '+str(path.relative_to(app)))
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
    executable=target/app.name/'Contents/MacOS/BS Podcasts'
    if executable.exists():
        opened=subprocess.run(['/usr/sbin/lsof','-t',str(executable)],capture_output=True,text=True)
        if opened.returncode not in (0,1) or opened.stdout.strip():
            raise RuntimeError('Close the target Mac application before replacing it. The completed build remains at '+str(app))
    previous=workspace/'.cache/previous-deployments'/work.name; previous.mkdir(parents=True)
    paths=[(app,target/app.name),(archive,target/archive.name),(manifest,target/manifest.name)]
    retired=[]; published=[]
    try:
        for source,destination in paths:
            if destination.is_symlink(): raise RuntimeError('Refusing to replace artifact link.')
            if destination.exists():
                destination.rename(previous/destination.name); retired.append(destination)
            source.rename(destination); published.append((source,destination))
        replaced=True
    except BaseException:
        for source,destination in reversed(published): destination.rename(source)
        for destination in reversed(retired): (previous/destination.name).rename(destination)
        if not any(previous.iterdir()): previous.rmdir()  # a rolled-back publish leaves nothing to recover
        raise
    # Rollbacks are user recovery data: pruning is an explicit opt-in, as on Windows. An empty folder holds
    # nothing to recover (a first build retires nothing), so it is neither kept, counted nor reported.
    retained=previous if any(previous.iterdir()) else None
    if retained is None: previous.rmdir()
    # Pruning lists the exact rollbacks and sizes, then deletes only after a typed YES in a terminal.
    pruned=(confirm_and_prune(workspace/'.cache/previous-deployments',ROLLBACK_PREFIX,report,retain=retained)
            if prune else None)
    signed=bool(os.environ.get('BS_PODCASTS_SIGN_IDENTITY'))
    app_files,app_bytes=tree_size(target/app.name)
    report.ok(f'Built and headless/relocation-verified BS Podcasts {version}: {target}')
    (report.ok if signed else report.warn)('Signing: '+('Developer ID; notarization still separate' if signed else 'ad-hoc only; not notarized'))
    if retained is not None: report.ok(f'Previous artifacts retained: {retained}')
    if pruned is None:
        report.info('Older rollbacks kept (--prune-old-deployments keeps only the newest 3).')
    report.stats([('version',version),('app files',str(app_files)),('app size',size_text(app_bytes)),
                  ('archive',f'{archive.name}, {size_text((target/archive.name).stat().st_size)}'),
                  ('rollbacks pruned','none (opt-in)' if pruned is None else str(len(pruned)))])
    return replaced


if __name__=='__main__':
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--check',action='store_true')
    parser.add_argument('--prune-old-deployments',action='store_true',
                        help='after a successful build, list the Mac rollbacks beyond the newest 3 and delete them only after a typed YES')
    parser.add_argument('--plain',action='store_true',help='no colour')
    args=parser.parse_args()
    report=Reporter(plain=args.plain)
    if args.check:
        ran=[]
        count=check_source(ran=ran)
        report.ok(f'Approved source files: {count}')
        skipped=[line for line in ran if 'SKIPPED' in line]
        for line in ran:
            report.line('warn' if line in skipped else 'ok',line)
        report.stats([('source files',str(count)),('gates',f'{len(ran)-len(skipped)} checked, {len(skipped)} skipped')])
        raise SystemExit(0)
    workspace=Path(os.environ.get('BS_PODCASTS_BUILD_ROOT',ROOT.parent)).resolve()
    with (workspace/'.cache/macos-build.lock').open('a+b') as lease:
        try:
            fcntl.flock(lease,fcntl.LOCK_EX|fcntl.LOCK_NB)
        except BlockingIOError:
            raise SystemExit('macOS build is busy (.cache/macos-build.lock); retry after it finishes.') from None
        try:
            build(workspace, prune=args.prune_old_deployments, report=report)
        except ValueError as exc:
            report.fail(str(exc))
            raise SystemExit(1) from None
