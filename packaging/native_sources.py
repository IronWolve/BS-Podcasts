"""Hash-pinned native inputs, bounded safe extraction, and build-path remapping."""
import argparse
import hashlib
import json
import platform
from pathlib import Path
import re
import subprocess
import tarfile
import tempfile
import urllib.request


def digest(path):
    with path.open('rb') as stream:
        return hashlib.file_digest(stream,'sha256').hexdigest()


def prepare(source, cache, only=None, macos=False):
    source.mkdir(parents=True,exist_ok=True)
    cache.mkdir(parents=True,exist_ok=True)
    entries=json.loads(Path(__file__).with_name('native-sources.json').read_text())
    if macos:
        entries += json.loads(Path(__file__).with_name('native-sources-macos.json').read_text())
    if only:
        entries=[entry for entry in entries if entry['name']==only]
        if not entries:
            raise ValueError('Unknown native source selection.')
    opener=urllib.request.build_opener(urllib.request.ProxyHandler({}))
    for entry in entries:
        archive=cache/entry['archive']
        if archive.is_symlink():
            raise ValueError('Native archive cache must not contain links.')
        if not archive.exists():
            with tempfile.NamedTemporaryFile(dir=cache,delete=False) as temporary:
                pending=Path(temporary.name)
                try:
                    with opener.open(entry['url'],timeout=30) as response:
                        if not response.url.startswith('https://'):
                            raise ValueError('Native download redirected away from HTTPS.')
                        size=0
                        while chunk:=response.read(65536):
                            size+=len(chunk)
                            if size>80*1024*1024:
                                raise ValueError('Native archive exceeds the size limit.')
                            temporary.write(chunk)
                    temporary.flush()
                    if digest(pending)!=entry['sha256']:
                        raise ValueError('Native archive checksum mismatch: '+entry['name'])
                except BaseException:
                    temporary.close(); pending.unlink(missing_ok=True)
                    raise
            pending.replace(archive)
        if digest(archive)!=entry['sha256']:
            raise ValueError('Native archive checksum mismatch: '+entry['name'])
        with tarfile.open(archive) as bundle:
            members=bundle.getmembers()
            if len(members)>50000 or sum(m.size for m in members)>1024**3:
                raise ValueError('Native archive exceeds extraction limits.')
            bundle.extractall(source,filter='data')
        expected=source/(entry['name']+'-'+entry['version'])
        if not expected.is_dir() or expected.is_symlink():
            raise ValueError('Unexpected native source layout: '+entry['name'])
        if entry.get('destination'):
            target=source/entry['destination']
            if not target.resolve().is_relative_to(source.resolve()) or target.is_symlink():
                raise ValueError('Unsafe native submodule destination.')
            if target.exists():
                target.rmdir()  # Only the empty placeholder from the parent archive.
            target.parent.mkdir(parents=True,exist_ok=True)
            expected.rename(target)
        print('Verified native input:',entry['name'],entry['version'],flush=True)


def sanitize(header, root):
    # Configuration describes compiler inputs but must not publish the builder's
    # home directory. This changes descriptive/generated path strings only.
    text=header.read_text()
    header.write_text(text.replace(str(root),'/bs-native'))


def record(artifact):
    lock=Path(__file__).with_name('native-sources.json')
    receipt={'format':1,'artifact':artifact.name,'sha256':digest(artifact),'inputs_sha256':digest(lock)}
    if artifact.name == 'libmpv-2.dll':
        receipt.update(platform='windows', architecture='x86_64',
                       recipe_sha256=digest(lock.with_name('build-libmpv-lgpl.sh')))
    if artifact.name == 'libmpv.so.2':
        versions = subprocess.check_output(['readelf','--version-info',str(artifact)],text=True,timeout=10)
        glibc = {tuple(map(int,value.split('.'))) for value in re.findall(r'GLIBC_([0-9.]+)',versions)}
        receipt.update(platform='linux', architecture=platform.machine(),
                       minimum_glibc='.'.join(map(str,max(glibc))),
                       recipe_sha256=digest(lock.with_name('build-libmpv-linux.sh')))
    if artifact.name == 'libmpv.2.dylib':
        receipt.update(platform='darwin', architecture=platform.machine(), minimum_macos='14.0',
                       extra_inputs_sha256=digest(lock.with_name('native-sources-macos.json')),
                       patch_sha256=digest(lock.with_name('mpv-coreaudio-only.patch')),
                       recipe_sha256=digest(lock.with_name('build-libmpv-macos.sh')))
    target=artifact.with_name('native-build.json')
    with tempfile.NamedTemporaryFile(dir=target.parent,delete=False) as stream:
        temporary=Path(stream.name)
        stream.write((json.dumps(receipt,indent=2)+'\n').encode())
    temporary.replace(target)
    print(target)


if __name__=='__main__':
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--source',type=Path)
    parser.add_argument('--cache',type=Path)
    parser.add_argument('--sanitize',type=Path)
    parser.add_argument('--root',type=Path)
    parser.add_argument('--only')
    parser.add_argument('--record',type=Path)
    parser.add_argument('--macos',action='store_true')
    args=parser.parse_args()
    if args.record:
        record(args.record)
    elif args.sanitize:
        sanitize(args.sanitize,args.root)
    elif args.source and args.cache:
        prepare(args.source,args.cache,args.only,args.macos)
    else:
        parser.error('Provide --source/--cache or --sanitize/--root.')
