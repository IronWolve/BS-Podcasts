"""Small offline checks for imported source origins and Mac archive receipts."""
import hashlib
import json
from pathlib import Path
import sys
from tempfile import TemporaryDirectory
from unittest.mock import patch
import zipfile

ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT/'packaging'))
import source_manifest
import release_archives


def rejects(call):
    try: call()
    except ValueError: return
    raise AssertionError('Bad provenance accepted')


def main():
    scratch=ROOT.parent/'tmp'; scratch.mkdir(exist_ok=True)
    with TemporaryDirectory(prefix='release-provenance-',dir=scratch) as temporary:
        root=Path(temporary); imported=root/'imported'; imported.mkdir()
        (imported/'pyproject.toml').write_text('[project]\nversion="0.0.1"\n')
        (imported/'source-manifest.json').write_text(json.dumps({'files':['source-manifest.json','pyproject.toml'],'runtime':{}}))
        origin={'commit':'1'*40,'dirty':False,'sha256':source_manifest.source_digest(imported)}
        (imported/'.build-origin.json').write_text(json.dumps(origin))
        with patch.object(source_manifest.subprocess,'run',side_effect=FileNotFoundError):
            staged=source_manifest.stage(root/'staged',imported)
        assert json.loads((staged/'.build-origin.json').read_text())==origin
        (imported/'pyproject.toml').write_text('[project]\nversion="0.0.2"\n')
        rejects(lambda:source_manifest.stage(root/'stale',imported))
        payload=b'fixture native bytes'
        digest=hashlib.sha256(payload).hexdigest()
        native_file='Contents/Frameworks/libmpv.2.dylib'
        archive=root/'BS-Podcasts-0.0.1-macOS-arm64.zip'
        with zipfile.ZipFile(archive,'w') as bundle:
            bundle.writestr('BS Podcasts.app/'+native_file,payload)
        native={'file':native_file,'sha256':digest,'recipe_sha256':source_manifest.file_digest(ROOT/'packaging/build-libmpv-macos.sh'),
                'extra_inputs_sha256':source_manifest.file_digest(ROOT/'packaging/native-sources-macos.json'),
                'inputs':json.loads((ROOT/'packaging/native-sources.json').read_text()),
                'extra_inputs':json.loads((ROOT/'packaging/native-sources-macos.json').read_text())}
        record={'version':'0.0.1','source':origin,'runtime':{'native':native},
                'archive':{'name':archive.name,'bytes':archive.stat().st_size,'sha256':source_manifest.file_digest(archive)},
                'artifact':{'name':'BS Podcasts.app','files':[{'path':native_file,'bytes':len(payload),'sha256':digest}]}}
        manifest=root/'BS-Podcasts-macOS-arm64.manifest.json'
        manifest.write_text(json.dumps(record))
        release_archives.verify_macos(root,'0.0.1',origin)
        record['source']={**origin,'dirty':True}; manifest.write_text(json.dumps(record))
        rejects(lambda:release_archives.verify_macos(root,'0.0.1',origin))
        record['source']=origin; record['artifact']['files'][0]['sha256']='0'*64; manifest.write_text(json.dumps(record))
        rejects(lambda:release_archives.verify_macos(root,'0.0.1',origin))
    print('Release provenance: PASS imported origin, stale-source refusal, Mac ZIP hashes and dirty-build rejection')


if __name__=='__main__': main()
