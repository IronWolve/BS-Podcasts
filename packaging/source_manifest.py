"""Exact source inventory, privacy checks and manifest-only build staging."""
import argparse
import hashlib
import json
from pathlib import Path, PurePosixPath
import re
import shutil
import subprocess
import sys

ROOT = Path(__file__).resolve().parents[1]

PRIVATE_ROOTS = frozenset({'data', 'docs', 'logs', 'tmp', 'run', 'dist', 'dists', 'build', 'node_modules'})
PRIVATE_NAMES = frozenset({'agents.md', 'agent.md', 'claude.md', 'auth.json', 'credentials.json', 'id_rsa', 'id_ed25519'})


def private_path(name):
    parts = PurePosixPath(name).parts
    return (parts[0].lower() in PRIVATE_ROOTS
            or any(part.lower() in PRIVATE_NAMES for part in parts)
            or any(part.startswith('.') for part in parts if part not in {'.gitignore', '.gitattributes'})
            or PurePosixPath(name).suffix.lower() in {'.db', '.sqlite', '.sqlite3', '.log', '.pcap', '.key', '.pem'})


def inventory(root=ROOT):
    root = Path(root).resolve()
    payload = json.loads((root / 'source-manifest.json').read_text(encoding='utf-8'))
    files = payload['files']
    if not isinstance(files, list) or len(files) != len(set(files)):
        raise ValueError('Source manifest must contain unique exact file paths.')
    if 'source-manifest.json' not in files:
        raise ValueError('The source manifest must include itself in its inventory.')
    for name in files:
        path = PurePosixPath(name)
        if path.is_absolute() or '..' in path.parts or str(path) != name or any(c in name for c in '\\:\0\r\n'):
            raise ValueError(f'Invalid manifest path: {name}')
        if private_path(name):
            raise ValueError(f'Private/agent/runtime path is not an approved source input: {name}')
        source = root / name
        if not source.is_file() or source.is_symlink() or not source.resolve().is_relative_to(root):
            raise ValueError(f'Missing or unsafe source file: {name}')
    runtime = payload['runtime']
    if not isinstance(runtime, dict) or not set(runtime).issubset(files):
        raise ValueError('Runtime inputs must be approved source files.')
    if len(set(runtime.values())) != len(runtime):
        raise ValueError('Duplicate deployment targets.')
    for name in runtime.values():
        path = PurePosixPath(name)
        if path.is_absolute() or '..' in path.parts or str(path) != name or any(c in name for c in '\\:\0\r\n'):
            raise ValueError(f'Invalid runtime path: {name}')
        if private_path(name):
            raise ValueError(f'Private/agent/runtime path is not an approved deployment output: {name}')
    return files, runtime


def file_digest(path):
    with path.open('rb') as handle:
        return hashlib.file_digest(handle, 'sha256').hexdigest()


def source_digest(root=ROOT):
    files, _ = inventory(root)
    records = [(name, file_digest(Path(root)/name)) for name in sorted(files)]
    return hashlib.sha256(json.dumps(records,separators=(',',':')).encode()).hexdigest()


def check(root=ROOT):
    root = Path(root).resolve()
    files, _ = inventory(root)
    private_tokens = root.parent / '.config/source-deny.txt'
    denied = [s.strip() for s in private_tokens.read_text().splitlines() if s.strip()] if private_tokens.is_file() else []
    secrets = re.compile(r'(?:sk-[A-Za-z0-9]{24,}|ghp_[A-Za-z0-9]{30,}|github_pat_[A-Za-z0-9_]{30,}|-----BEGIN [A-Z ]*PRIVATE KEY-----)')
    failures = []
    for name in files:
        raw = (root/name).read_bytes()
        if any(value.encode() in raw for value in denied):
            failures.append(name)
            continue
        text = raw.decode('utf-8', 'replace')
        if secrets.search(text):
            failures.append(name)
    if failures:
        raise ValueError('Private references or credential patterns in: '+', '.join(failures))
    try:
        top = subprocess.run(['git','-C',str(root),'rev-parse','--show-toplevel'],capture_output=True,text=True,timeout=5)
        if top.returncode == 0 and Path(top.stdout.strip()).resolve() == root:
            tracked = subprocess.run(['git','-C',str(root),'ls-files','-z'],capture_output=True,text=True,timeout=5,check=True)
            unexpected = set(tracked.stdout.split('\0')) - {''} - set(files)
            if unexpected:
                raise ValueError('Tracked files outside the source allowlist: '+', '.join(sorted(unexpected)))
    except (OSError, subprocess.SubprocessError):
        pass  # Manifest-only snapshots/build hosts need not have Git available.
    return len(files)


def stage(destination, root=ROOT):
    root = Path(root).resolve()
    destination = Path(destination).resolve()
    if destination.is_relative_to(root) or root.is_relative_to(destination):
        raise ValueError('Staging must be outside the source checkout and cannot contain it.')
    check(root)
    files, _ = inventory(root)
    expected = source_digest(root)
    destination.mkdir(parents=True, exist_ok=False)
    for name in files:
        target = destination/name
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(root/name, target)
    if source_digest(destination) != expected or source_digest(root) != expected:
        raise ValueError('Source changed while staging; build cancelled before compilation.')
    origin={'commit':None,'dirty':True,'sha256':expected}
    inherited = root/'.build-origin.json'
    if inherited.is_file():
        previous=json.loads(inherited.read_text())
        if previous.get('sha256') != expected:
            raise ValueError('Imported source origin does not match its exact files.')
        origin.update(commit=previous.get('commit'), dirty=previous.get('dirty',True))
    try:
        top = subprocess.run(['git','-C',str(root),'rev-parse','--show-toplevel'],capture_output=True,text=True,timeout=5)
        if top.returncode==0 and Path(top.stdout.strip()).resolve()==root:
            commit = subprocess.run(['git','-C',str(root),'rev-parse','HEAD'],capture_output=True,text=True,timeout=5)
            status = subprocess.run(['git','-C',str(root),'status','--porcelain'],capture_output=True,text=True,timeout=5)
            origin.update(commit=commit.stdout.strip() if commit.returncode==0 else None,
                          dirty=status.returncode!=0 or bool(status.stdout.strip()))
    except (OSError,subprocess.TimeoutExpired):
        pass  # Build hosts without Git still get content-addressed development artifacts.
    (destination/'.build-origin.json').write_text(json.dumps(origin,indent=2)+'\n')
    return destination


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('operation', choices=['check','stage','hash','list'])
    parser.add_argument('--destination', type=Path)
    args = parser.parse_args()
    if args.operation == 'check':
        print(f'Approved source files: {check()}')
    elif args.operation == 'hash':
        print(source_digest())
    elif args.operation == 'list':
        check()
        sys.stdout.buffer.write(('\0'.join(inventory()[0])+'\0').encode())
    elif args.destination is None:
        parser.error('stage requires --destination')
    else:
        print(stage(args.destination))
