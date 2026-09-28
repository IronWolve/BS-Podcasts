"""Reject stale build environments; installation remains an explicit operation."""
import argparse
import importlib.metadata
from pathlib import Path
import sys


def check(lock):
    if sys.version_info < (3,14,7):
        raise RuntimeError('The security-updated application requires Python 3.14.7 or newer.')
    failures=[]
    count=0
    for line in Path(lock).read_text().splitlines():
        line=line.strip()
        if not line or line.startswith('#'):
            continue
        name, version=line.split('==',1)
        count+=1
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
