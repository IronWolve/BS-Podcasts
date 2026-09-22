"""Build the local Linux runtime from an exact manifest, without installing or launching."""
import argparse
import ast
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import shutil
import tempfile
import tomllib
import fcntl
from source_manifest import ROOT, check, inventory, source_digest, file_digest


def deploy():
    workspace = ROOT.parent
    destination = workspace/'dists/linux'
    if not (destination/'.venv/bin/python').is_file():
        raise RuntimeError('Missing dists/linux/.venv. Dependency setup requires explicit approval.')
    pid_record = workspace/'tmp/bs-podcasts.pid.json'
    if pid_record.exists():
        raise RuntimeError('A runtime PID record exists. Check ./start.sh --status and stop this project before rebuilding.')
    check()
    _, runtime = inventory()
    for name in runtime:
        if name.endswith('.py'):
            ast.parse((ROOT/name).read_text(encoding='utf-8'),filename=name)
    destination.mkdir(parents=True,exist_ok=True)
    temporary = Path(tempfile.mkdtemp(prefix='linux-build-',dir=workspace/'tmp'))
    for name, relative in runtime.items():
        target = temporary/relative
        target.parent.mkdir(parents=True,exist_ok=True)
        shutil.copy2(ROOT/name,target)
    version = tomllib.loads((ROOT/'pyproject.toml').read_text())['project']['version']
    record = {'version':version,'built_at':datetime.now(timezone.utc).isoformat(),
              'source_sha256':source_digest(),
              'files':{relative:file_digest(temporary/relative) for relative in runtime.values()}}
    (temporary/'deployment.json').write_text(json.dumps(record,indent=2)+'\n')
    # Preserve the prior runtime; never replace personal settings, data or venv.
    previous = workspace/'.cache/previous-deployments'/temporary.name
    previous.mkdir(parents=True)
    retired, published = [], []
    try:
        for name in ('app','licenses','THIRD-PARTY-NOTICES.txt','runner.py','deployment.json'):
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
    temporary.rmdir()
    print(f'Built BS Podcasts {version}: {destination}')
    print(f'Source SHA-256: {record["source_sha256"]}')
    print('No dependencies installed and no application started. Run ./start.sh when ready.')


if __name__ == '__main__':
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--check',action='store_true')
    args=parser.parse_args()
    if args.check:
        print(f'Approved source files: {check()}')
    else:
        with (ROOT.parent/'.cache/linux-build.lock').open('a+b') as lock:
            fcntl.flock(lock,fcntl.LOCK_EX | fcntl.LOCK_NB)
            deploy()
