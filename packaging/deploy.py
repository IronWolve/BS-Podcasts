"""Build the local Linux runtime from an exact manifest, without installing or launching."""
import argparse
import ast
from datetime import datetime, timezone
import json
import os
from pathlib import Path, PurePosixPath
import re
import shutil
import tempfile
import fcntl
# The version gate lives with the source check; deploy keeps its names for callers.
from source_manifest import ROOT, check, checked_version, inventory, package_version, source_digest, file_digest, stage  # noqa: F401
from runtime_state import runtime_lock, retire_stale
from report import Reporter, size_text
from build_manifest import require_keys


# Linux rollbacks are named by mkdtemp with this prefix. The shared folder also
# holds other platforms' rollbacks and staging, which this build never prunes.
ROLLBACK_PREFIX = 'linux-build-'
_ROLLBACK_NAME = re.compile(re.escape(ROLLBACK_PREFIX) + r'[a-z0-9_]+')


def prune_previous_deployments(root, keep=3, retain=None):
    """Keep the newest `keep` Linux rollbacks (always including `retain`); return removed names."""
    if not root.is_dir() or root.is_symlink():
        return []
    entries = [path for path in root.iterdir()
               if _ROLLBACK_NAME.fullmatch(path.name) and path.is_dir() and not path.is_symlink()
               and path != retain]
    entries.sort(key=lambda path: path.stat().st_mtime, reverse=True)
    removed = []
    for stale in entries[max(keep - (retain is not None), 0):]:
        shutil.rmtree(stale)
        removed.append(stale.name)
    return removed


# Built by this deploy beside the manifest's runtime destinations: the native library folder and the record.
BUILT_ENTRIES = ('native', 'deployment.json')


def publish_names(runtime):
    """The names published into the runtime folder: the manifest's top-level runtime destinations and the built entries."""
    return sorted({PurePosixPath(relative).parts[0] for relative in runtime.values()} | set(BUILT_ENTRIES))


def receipt_source(snapshot):
    """The commit and dirty state that stage() recorded for this snapshot, in the shape the currency check reads."""
    origin = json.loads((snapshot/'.build-origin.json').read_text(encoding='utf-8'))
    return {'commit': origin.get('commit'), 'dirty': bool(origin.get('dirty', True))}


def install_entrypoints(workspace, destination, previous, report=None, changed=None):
    changed = [] if changed is None else changed
    template = '''#!/usr/bin/env bash
set -euo pipefail
BS_PROJECT="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd -P)"
exec bash "$BS_PROJECT/dists/linux/COMMAND.sh" "$@"
'''
    for command in ('start', 'stop'):
        if not (destination/(command+'.sh')).is_file():
            continue
        target = workspace/(command+'.sh')
        content = template.replace('COMMAND', command)
        old = content.replace('dists/linux/'+command+'.sh', 'repo/packaging/project-'+command+'.sh')
        if target.is_symlink() or (target.exists() and target.read_text() not in (old, content)):
            (report or Reporter()).warn(f'Customized {command}.sh preserved; use dists/linux/{command}.sh directly.')
            continue
        if target.exists():
            shutil.copy2(target, previous/('root-'+command+'.sh'))
        descriptor, name = tempfile.mkstemp(prefix='.launcher-',dir=workspace/'tmp')
        temporary = Path(name)
        try:
            with os.fdopen(descriptor,'w') as handle:
                handle.write(content)
            temporary.chmod(0o755)
            os.replace(temporary,target)
            changed.append(command)
        finally:
            temporary.unlink(missing_ok=True)


def deploy(prune=False, report=None):
    report = report or Reporter()
    workspace = ROOT.parent
    destination = workspace/'dists/linux'
    if not (destination/'.venv/bin/python').is_file():
        raise RuntimeError('Missing dists/linux/.venv. Dependency setup requires explicit approval.')
    # Fail early for a running app, then check again under the publication lease.
    with runtime_lock(workspace):
        retire_stale(workspace)
    destination.mkdir(parents=True,exist_ok=True)
    temporary = Path(tempfile.mkdtemp(prefix=ROLLBACK_PREFIX,dir=workspace/'tmp'))
    try:
        _deploy_staged(workspace, destination, temporary, prune, report)
    finally:
        if (temporary.is_dir() and not temporary.is_symlink()
                and temporary.name.startswith(ROLLBACK_PREFIX) and temporary.is_relative_to(workspace/'tmp')):
            shutil.rmtree(temporary)


def _deploy_staged(workspace, destination, temporary, prune, report):
    snapshot = stage(temporary/'source',root=ROOT)
    version = checked_version(snapshot)
    source_files, runtime = inventory(snapshot)
    for name in runtime:
        if name.endswith('.py'):
            ast.parse((snapshot/name).read_text(encoding='utf-8'),filename=name)
    for name, relative in runtime.items():
        target = temporary/relative
        target.parent.mkdir(parents=True,exist_ok=True)
        shutil.copy2(snapshot/name,target)
    native = workspace/'.cache/linux-build/libmpv-lgpl'
    try:
        receipt = json.loads((native/'native-build.json').read_text())
    except OSError as exc:
        raise ValueError(f'Linux native receipt cannot be read ({exc.strerror or exc}); rebuild the native library.') from None
    require_keys(receipt, ('sha256', 'inputs_sha256', 'recipe_sha256'), 'Linux native receipt')
    if (receipt['sha256'] != file_digest(native/'libmpv.so.2')
            or receipt['inputs_sha256'] != file_digest(snapshot/'packaging/native-sources.json')
            or receipt['recipe_sha256'] != file_digest(snapshot/'packaging/build-libmpv-linux.sh')):
        raise RuntimeError('Linux native library differs from its approved inputs; rebuild it first.')
    (temporary/'native').mkdir()
    for name in ('libmpv.so.2', 'native-build.json'):
        shutil.copy2(native/name, temporary/'native'/name)
    deployed = [*runtime.values(), 'native/libmpv.so.2', 'native/native-build.json']
    record = {'version':version,'built_at':datetime.now(timezone.utc).isoformat(),
              'source_sha256':source_digest(snapshot),
              'source':receipt_source(snapshot),
              'files':{relative:file_digest(temporary/relative) for relative in deployed},
              'native':receipt}
    (temporary/'deployment.json').write_text(json.dumps(record,indent=2)+'\n')
    # The publish list comes from the manifest's top-level runtime destinations plus the two built entries.
    # Refuse before anything is swapped if the built tree holds a different set.
    publish = publish_names(runtime)
    built_names = sorted(path.name for path in temporary.iterdir() if path.name != 'source')
    if built_names != publish:
        raise RuntimeError('Built runtime entries differ from the manifest destinations; nothing was published: '
                           f'built {built_names}, expected {publish}.')
    # Preserve the prior runtime; never replace personal settings, data or venv.
    previous = workspace/'.cache/previous-deployments'/temporary.name
    previous.mkdir(parents=True)
    with runtime_lock(workspace):
        retire_stale(workspace)
        retired, published, launchers = [], [], []
        try:
            for name in publish:
                current = destination/name
                built = temporary/name
                if current.exists():
                    current.rename(previous/name)
                    retired.append(name)
                if built.exists():
                    built.rename(current)
                    published.append(name)
            snapshot.rename(previous/'source-inputs')
            install_entrypoints(workspace, destination, previous, report, changed=launchers)
        except BaseException:
            for command in reversed(launchers):
                current = workspace / (command + '.sh')
                saved = previous / ('root-' + command + '.sh')
                if saved.exists():
                    os.replace(saved, current)
                else:
                    current.rename(previous / ('unpublished-root-' + command + '.sh'))
            for name in reversed(published):
                (destination/name).rename(temporary/name)
            for name in reversed(retired):
                (previous/name).rename(destination/name)
            raise
    # Rollbacks are user recovery data: pruning is an explicit opt-in, as on Windows.
    pruned = prune_previous_deployments(workspace/'.cache/previous-deployments', retain=previous) if prune else None
    deployed_bytes = sum((destination/relative).stat().st_size for relative in deployed)
    report.ok(f'Built BS Podcasts {version}: {destination}')
    report.ok(f'Rollback retained: {previous}')
    if pruned is None:
        report.info('Older rollbacks kept (--prune-old-deployments keeps only the newest 3).')
    report.info('No dependencies installed and no application started. Run ./start.sh when ready.')
    report.stats([('version', version), ('source files', str(len(source_files))),
                  ('source SHA-256', record['source_sha256']), ('deployed files', str(len(deployed))),
                  ('deployed size', size_text(deployed_bytes)),
                  ('rollbacks pruned', 'none (opt-in)' if pruned is None else str(len(pruned)))])


if __name__ == '__main__':
    from check_dependencies import check as check_dependencies
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--check',action='store_true')
    parser.add_argument('--prune-old-deployments',action='store_true',
                        help='delete all but the newest 3 Linux rollbacks after a successful build')
    parser.add_argument('--plain',action='store_true',help='no colour')
    args=parser.parse_args()
    report = Reporter(plain=args.plain)
    if args.check:
        ran = []
        count = check(ran=ran)
        report.ok(f'Approved source files: {count}')
        skipped = [line for line in ran if 'SKIPPED' in line]
        for line in ran:
            report.line('warn' if line in skipped else 'ok', line)
        report.stats([('source files', str(count)), ('gates', f'{len(ran) - len(skipped)} checked, {len(skipped)} skipped')])
    else:
        # The locked environment guards the build only; the source report and --help need none of it.
        try:
            check_dependencies(ROOT/'packaging/requirements-linux.lock')
        except (OSError, RuntimeError, ValueError) as exc:
            report.fail(f'Dependency check refused the build: {exc}')
            raise SystemExit(1) from None
        with (ROOT.parent/'.cache/linux-build.lock').open('a+b') as lock:
            try:
                fcntl.flock(lock,fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError:
                raise SystemExit('Linux build is busy (.cache/linux-build.lock); retry after it finishes.') from None
            try:
                deploy(prune=args.prune_old_deployments, report=report)
            except ValueError as exc:
                report.fail(str(exc))
                raise SystemExit(1) from None
