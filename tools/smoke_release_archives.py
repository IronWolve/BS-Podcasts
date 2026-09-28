"""Small offline release-inventory, privacy and archive-format checks."""
import json
from pathlib import Path
import sys
import tarfile
from tempfile import TemporaryDirectory
import zipfile
from unittest.mock import patch
from types import SimpleNamespace

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT/'packaging'))
import build_manifest
from release_archives import scan_inputs, tar_files, verify_files, zip_files


def rejects(call):
    try:
        call()
    except ValueError:
        return
    raise AssertionError('Invalid release input accepted')


def main():
    with TemporaryDirectory(prefix='release-archives-', dir=ROOT.parent/'tmp') as name:
        folder = Path(name)
        artifact = folder/'app'; artifact.mkdir()
        (artifact/'Z.txt').write_bytes(b'first')
        (artifact/'a.txt').write_bytes(b'second')
        records = build_manifest.artifact_inventory(artifact)
        assert [record['path'] for record in records] == ['Z.txt', 'a.txt']
        files = verify_files(artifact, records)
        (artifact/'extra.txt').write_bytes(b'not inventoried')
        rejects(lambda: verify_files(artifact, records))
        scan_inputs(files, [b'private-fixture-marker'])
        rejects(lambda: scan_inputs({'safe.txt': b'private-fixture-marker'}, [b'private-fixture-marker']))
        rejects(lambda: scan_inputs({'.codex/auth.json': b'fixture'}, []))
        rejects(lambda: scan_inputs({'../escape': b'fixture'}, []))
        zip_files(folder/'app.zip', files, 'Application')
        tar_files(folder/'app.tar.gz', files, 'Application', ('Z.txt',))
        with zipfile.ZipFile(folder/'app.zip') as archive:
            assert {name: archive.read(name) for name in archive.namelist()} == {'Application/'+name: value for name, value in files.items()}
        with tarfile.open(folder/'app.tar.gz') as archive:
            assert archive.getmember('Application/Z.txt').mode == 0o755
            assert all(item.uid == 0 and item.gid == 0 and not item.uname and not item.gname for item in archive)
        with patch.object(build_manifest.subprocess, 'run', return_value=SimpleNamespace(returncode=0, stdout=str(folder))) as git:
            assert build_manifest.git_value('status', '--porcelain') is None
            assert git.call_count == 1, 'Parent repository status must not be inspected'
    print('Release archives: PASS exact inventories, stable path ordering, private/traversal rejection, ZIP/tar integrity and parent-Git isolation')


if __name__ == '__main__':
    main()
