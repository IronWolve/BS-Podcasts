"""Reject stale build environments; installation remains an explicit operation."""
import argparse
import importlib.metadata
from pathlib import Path
import sys


def parse_requirement(text):
    if '==' not in text:
        raise ValueError('Dependency lock line must pin name==version.')
    name, rest = text.split('==', 1)
    parts = rest.split()
    if not name.strip() or not parts:
        raise ValueError('Dependency lock line must pin name==version.')
    version = parts[0]
    hashes = []
    for part in parts[1:]:
        if not part.startswith('--hash='):
            raise ValueError('Unsupported dependency lock field.')
        digest = part.split('=', 1)[1]
        algorithm, _, hexdigest = digest.partition(':')
        if algorithm != 'sha256' or len(hexdigest) != 64 or any(c not in '0123456789abcdef' for c in hexdigest):
            raise ValueError('Dependency lock hash must be sha256.')
        hashes.append(digest)
    return name.strip(), version, hashes


def parse_lock(text):
    entries = []
    pending = ''
    for line in text.splitlines():
        stripped = line.strip()
        if not stripped or stripped.startswith('#'):
            if pending:
                raise ValueError('Dependency lock has a dangling continuation.')
            continue
        if stripped.endswith('\\'):
            pending += stripped[:-1].strip() + ' '
            continue
        pending += stripped
        entries.append(parse_requirement(pending.strip()))
        pending = ''
    if pending:
        raise ValueError('Dependency lock has a dangling continuation.')
    return entries


def check(lock):
    if sys.version_info < (3,14,7):
        raise RuntimeError('The security-updated application requires Python 3.14.7 or newer.')
    failures=[]
    try:
        importlib.metadata.version('mutagen')
    except importlib.metadata.PackageNotFoundError:
        pass
    else:
        failures.append('Retired dependency mutagen is still installed; remove it from this project environment before building.')
    count=0
    for name, version, hashes in parse_lock(Path(lock).read_text()):
        count+=1
        if not hashes:
            failures.append(f'{name}: dependency lock is missing publisher hashes')
        try:
            installed=importlib.metadata.version(name)
        except importlib.metadata.PackageNotFoundError:
            installed='missing'
        if installed!=version:
            failures.append(f'{name}: expected {version}, found {installed}')
    if failures:
        raise RuntimeError('Environment differs from the approved dependency lock; update it explicitly.\n'+'\n'.join(failures))
    print(f'Approved dependency versions verified: {count}; Python {sys.version.split()[0]}')


if __name__=='__main__':
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('lock',type=Path)
    check(parser.parse_args().lock)
