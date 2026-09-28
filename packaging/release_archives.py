"""Assemble versioned source/Windows/Linux archives from verified exact inputs."""
import argparse
import io
import json
import os
import platform
from pathlib import Path
import re
import subprocess
import tarfile
import tempfile
import tomllib
import zipfile

from source_manifest import ROOT, check, file_digest, inventory, source_digest
from build_manifest import artifact_inventory


def verify_files(root, records):
    expected = {record['path']: record for record in records}
    if len(expected) != len(records):
        raise ValueError('Duplicate artifact paths.')
    actual = artifact_inventory(root)
    if {record['path']: record for record in actual} != expected:
        raise ValueError('Artifact files differ from the build manifest.')
    if any('symlink' in record for record in actual):
        raise ValueError('This release format expects regular files, not links.')
    return {name: (root / name).read_bytes() for name in sorted(expected)}


def scan_inputs(files, tokens):
    forbidden = {'.git', '.codex', '.claude', '.agents', '.env', 'auth.json', 'credentials.json', 'agents.md'}
    for name, data in files.items():
        path = Path(name)
        if path.is_absolute() or '..' in path.parts or any(part.lower() in forbidden for part in path.parts):
            raise ValueError('Private or unsafe release path: ' + name)
        if any(token in data for token in tokens):
            raise ValueError('Known private reference in release input: ' + name)


def zip_files(path, files, prefix, executable=()):
    with zipfile.ZipFile(path, 'x', compression=zipfile.ZIP_DEFLATED, compresslevel=6) as archive:
        for name, data in sorted(files.items()):
            item = zipfile.ZipInfo(prefix + '/' + name, date_time=(2026, 1, 1, 0, 0, 0))
            item.create_system = 3
            item.external_attr = (0o100755 if name in executable else 0o100644) << 16
            item.compress_type = zipfile.ZIP_DEFLATED
            archive.writestr(item, data)
    with zipfile.ZipFile(path) as archive:
        if archive.testzip() is not None:
            raise ValueError('ZIP integrity check failed.')


def tar_files(path, files, prefix, executable=()):
    with tarfile.open(path, 'x:gz', compresslevel=6) as archive:
        for name, data in sorted(files.items()):
            item = tarfile.TarInfo(prefix + '/' + name)
            item.size = len(data); item.mode = 0o755 if name in executable else 0o644
            item.uid = item.gid = item.mtime = 0
            archive.addfile(item, io.BytesIO(data))
    with tarfile.open(path) as archive:
        for item in archive:
            name = item.name.removeprefix(prefix + '/')
            if archive.extractfile(item).read() != files[name]:
                raise ValueError('Linux archive integrity check failed.')


def build(output):
    check()
    names, runtime = inventory()
    def git(*args):
        return subprocess.check_output(['git', '-C', str(ROOT), *args]).decode().strip()
    if Path(git('rev-parse', '--show-toplevel')).resolve() != ROOT:
        raise ValueError('Release source must be its own Git checkout.')
    if git('status', '--porcelain'):
        raise ValueError('Release archives require a clean source checkout.')
    if set(git('ls-files', '-z').split('\0')) - {''} != set(names):
        raise ValueError('Release source inventory must exactly match Git tracking.')
    commit = git('rev-parse', 'HEAD')
    version = tomllib.loads((ROOT / 'pyproject.toml').read_text())['project']['version']
    if not re.fullmatch(r'\d+\.\d+\.\d+', version):
        raise ValueError('Release version must be numeric major.minor.patch.')
    output = (output or ROOT.parent / 'dists/releases' / version).resolve()
    if output.exists() or output.is_relative_to(ROOT):
        raise ValueError('Use a new release directory outside the source checkout.')
    fingerprint = source_digest()
    private = ROOT.parent / '.config/source-deny.txt'
    tokens = []
    if private.is_file():
        for line in private.read_text().splitlines():
            if line.strip():
                tokens.extend(line.encode(encoding) for encoding in ('utf-8', 'utf-16-le', 'utf-16-be'))
    source_files = {name: (ROOT / name).read_bytes() for name in names}
    source_executable = [name for name in names if os.stat(ROOT/name).st_mode & 0o111]
    scan_inputs(source_files, tokens)

    windows = ROOT.parent / 'dists/windows'
    win_record = json.loads((windows/'BS-Podcasts-Windows.manifest.json').read_text())
    if win_record['version'] != version or win_record['source'] != {'commit': commit, 'dirty': False, 'sha256': fingerprint}:
        raise ValueError('Windows build is stale, dirty or from a different commit.')
    windows_files = verify_files(windows/'BS Podcasts', win_record['artifact']['files'])
    scan_inputs(windows_files, tokens)

    linux = ROOT.parent / 'dists/linux'
    linux_record = json.loads((linux/'deployment.json').read_text())
    deployed = [*runtime.values(), 'native/libmpv.so.2', 'native/native-build.json']
    expected = {relative: file_digest(linux/relative) for relative in deployed}
    if linux_record['version'] != version or linux_record['source_sha256'] != fingerprint or linux_record['files'] != expected:
        raise ValueError('Linux build is stale or differs from its manifest.')
    if (linux_record['native']['sha256'] != expected['native/libmpv.so.2']
            or linux_record['native']['inputs_sha256'] != file_digest(ROOT/'packaging/native-sources.json')
            or linux_record['native']['recipe_sha256'] != file_digest(ROOT/'packaging/build-libmpv-linux.sh')):
        raise ValueError('Linux native library does not match the pinned source inputs.')
    linux_files = {'dists/linux/'+name: (linux/name).read_bytes() for name in deployed}
    linux_files['dists/linux/deployment.json'] = (linux/'deployment.json').read_bytes()
    linux_files['setup.sh'] = (ROOT/'packaging/linux-setup.sh').read_bytes()
    for name in ('start', 'stop'):
        linux_files[name+'.sh'] = ('#!/usr/bin/env bash\nset -euo pipefail\n'
            'BS_PROJECT="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd -P)"\n'
            f'exec bash "$BS_PROJECT/dists/linux/{name}.sh" "$@"\n').encode()
    linux_files['INSTALL.txt'] = (
        'BS Podcasts '+version+' - Linux runtime\n\n'
        'Requires Python 3.14.7+ and compatible native desktop/audio libraries.\n'
        'Requires glibc '+linux_record['native']['minimum_glibc']+' or newer.\n'
        'Bundles libmpv 0.41.0 with FFmpeg 9.0.2; the launcher never falls back to system libmpv.\n'
        'Built and tested on Ubuntu 26.04 '+platform.machine()+'. Other Linux distributions need ABI validation.\n'
        'Run bash setup.sh to create the project-local environment and install locked packages.\n'
        'Then bash start.sh --check --plain; bash start.sh launches the GUI.\n'
        'This is not a self-contained Linux binary. No developer checkout is required.\n'
        'After moving the folder, preserve/rename dists/linux/.venv and rerun setup.sh.\n'
        'Data, configuration, logs, caches and downloads stay beside the app by default.\n'
        'Do not include those private files when redistributing or updating the application.\n').encode()
    scan_inputs(linux_files, tokens)
    output.parent.mkdir(parents=True, exist_ok=True)
    work = Path(tempfile.mkdtemp(prefix='.release-', dir=output.parent))
    zip_files(work/f'BS-Podcasts-{version}-Source.zip', source_files, f'BS-Podcasts-{version}-Source', source_executable)
    zip_files(work/f'BS-Podcasts-{version}-Windows-x64.zip', windows_files, 'BS Podcasts')
    linux_name = f'BS-Podcasts-{version}-Linux-{platform.machine()}'
    tar_files(work/(linux_name+'.tar.gz'), linux_files, linux_name,
              ('start.sh', 'stop.sh', 'setup.sh', 'dists/linux/start.sh', 'dists/linux/stop.sh'))
    (work/'Windows.manifest.json').write_text(json.dumps(win_record, indent=2)+'\n')
    report = {'version': version, 'source': {'commit': commit, 'sha256': fingerprint},
              'windows_signed': False, 'linux_runtime_dependencies': 'Python >=3.14.7 and compatible ALSA/PulseAudio/desktop libraries; run setup.sh',
              'linux_architecture':platform.machine(), 'linux_native':linux_record['native'],
              'source_files': len(source_files), 'windows_files': len(windows_files),
              'linux_files': len(linux_files), 'archives': {}}
    for path in sorted(work.iterdir()):
        report['archives'][path.name] = {'bytes': path.stat().st_size, 'sha256': file_digest(path)}
    (work/'release.json').write_text(json.dumps(report, indent=2)+'\n')
    sums = ''.join(file_digest(path)+'  '+path.name+'\n' for path in sorted(work.iterdir()))
    (work/'SHA256SUMS').write_text(sums)
    if git('status', '--porcelain') or git('rev-parse', 'HEAD') != commit or source_digest() != fingerprint:
        raise ValueError('Source changed while release archives were being assembled; outputs remain staged.')
    work.rename(output)
    print(output)
    print(json.dumps(report, indent=2))


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--output', type=Path)
    build(parser.parse_args().output)
