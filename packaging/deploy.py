"""Build the local Linux runtime from an exact manifest, without installing or launching."""
import argparse
import ast
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import re
import shutil
import tempfile
import tomllib
import fcntl
from source_manifest import ROOT, check, inventory, source_digest, file_digest, stage
from runtime_state import runtime_lock, retire_stale


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


def package_version(path):
    """The package's literal __version__, read as text so the checkout is never imported."""
    tree = ast.parse(path.read_text(encoding='utf-8'), filename=str(path))
    values = [node.value.value for node in tree.body
              if isinstance(node, ast.Assign) and len(node.targets) == 1
              and isinstance(node.targets[0], ast.Name) and node.targets[0].id == '__version__'
              and isinstance(node.value, ast.Constant) and isinstance(node.value.value, str)]
    return values[0] if len(values) == 1 else None


def checked_version(snapshot):
    """pyproject's version, only when the package constant agrees with it."""
    version = tomllib.loads((snapshot/'pyproject.toml').read_text(encoding='utf-8'))['project']['version']
    module = package_version(snapshot/'src/bs_podcasts/__init__.py')
    if not isinstance(version, str) or not version or module != version:
        raise RuntimeError(f'Version mismatch: pyproject.toml {version!r}, bs_podcasts.__version__ {module!r}.')
    return version


def install_entrypoints(workspace, destination, previous):
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
            print(f'Customized {command}.sh preserved; use dists/linux/{command}.sh directly.')
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
        finally:
            temporary.unlink(missing_ok=True)


def deploy(prune=False):
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
        _deploy_staged(workspace, destination, temporary, prune)
    finally:
        if (temporary.is_dir() and not temporary.is_symlink()
                and temporary.name.startswith(ROLLBACK_PREFIX) and temporary.is_relative_to(workspace/'tmp')):
            shutil.rmtree(temporary)


def _deploy_staged(workspace, destination, temporary, prune=False):
    snapshot = stage(temporary/'source',root=ROOT)
    version = checked_version(snapshot)
    _, runtime = inventory(snapshot)
    for name in runtime:
        if name.endswith('.py'):
            ast.parse((snapshot/name).read_text(encoding='utf-8'),filename=name)
    for name, relative in runtime.items():
        target = temporary/relative
        target.parent.mkdir(parents=True,exist_ok=True)
        shutil.copy2(snapshot/name,target)
    native = workspace/'.cache/linux-build/libmpv-lgpl'
    receipt = json.loads((native/'native-build.json').read_text())
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
              'files':{relative:file_digest(temporary/relative) for relative in deployed},
              'native':receipt}
    (temporary/'deployment.json').write_text(json.dumps(record,indent=2)+'\n')
    # Preserve the prior runtime; never replace personal settings, data or venv.
    previous = workspace/'.cache/previous-deployments'/temporary.name
    previous.mkdir(parents=True)
    with runtime_lock(workspace):
        retire_stale(workspace)
        retired, published = [], []
        try:
            for name in ('app','native','licenses','THIRD-PARTY-NOTICES.txt','runner.py','runtime_state.py','runtime_linux.py','start.sh','stop.sh','requirements-linux.lock','deployment.json'):
                current = destination/name
                built = temporary/name
                if current.exists():
                    current.rename(previous/name)
                    retired.append(name)
                if built.exists():
                    built.rename(current)
                    published.append(name)
        except BaseException:
            for name in reversed(published):
                (destination/name).rename(temporary/name)
            for name in reversed(retired):
                (previous/name).rename(destination/name)
            raise
        snapshot.rename(previous/'source-inputs')
        install_entrypoints(workspace, destination, previous)
    # Rollbacks are user recovery data: pruning is an explicit opt-in, as on Windows.
    pruned = prune_previous_deployments(workspace/'.cache/previous-deployments', retain=previous) if prune else None
    print(f'Built BS Podcasts {version}: {destination}')
    print(f'Rollback retained: {previous}; ' + (f'older Linux rollbacks pruned: {len(pruned)}' if pruned is not None
          else 'older rollbacks kept (--prune-old-deployments keeps only the newest 3)'))
    print(f'Source SHA-256: {record["source_sha256"]}')
    print('No dependencies installed and no application started. Run ./start.sh when ready.')


if __name__ == '__main__':
    from check_dependencies import check as check_dependencies
    check_dependencies(ROOT/'packaging/requirements-linux.lock')
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--check',action='store_true')
    parser.add_argument('--prune-old-deployments',action='store_true',
                        help='delete all but the newest 3 Linux rollbacks after a successful build')
    args=parser.parse_args()
    if args.check:
        print(f'Approved source files: {check()}')
    else:
        with (ROOT.parent/'.cache/linux-build.lock').open('a+b') as lock:
            fcntl.flock(lock,fcntl.LOCK_EX | fcntl.LOCK_NB)
            deploy(prune=args.prune_old_deployments)
