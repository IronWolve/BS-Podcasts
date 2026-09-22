"""Small source/deployment separation checks. No GUI, network or process stopping."""
import importlib.util
import json
import os
from pathlib import Path
import subprocess
import sys
from tempfile import TemporaryDirectory
from unittest.mock import patch

ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT/'packaging'))
import source_manifest as sources


def main():
    files,runtime=sources.inventory(ROOT)
    assert sources.check(ROOT)==len(files)
    assert set(subprocess.check_output(['git','-C',str(ROOT),'ls-files','-z']).decode().split('\0'))-{''} == set(files)
    for name in ('src/bs_podcasts/unexpected.py','packaging/extra.sh','.env','.config/key.json','notes.md'):
        assert subprocess.run(['git','-C',str(ROOT),'check-ignore','-q','--',name]).returncode==0,name
    for path in ROOT.rglob('*'):
        if '.git' in path.relative_to(ROOT).parts:
            continue
        if path.is_file() or path.is_symlink():
            assert path.relative_to(ROOT).as_posix() in files,'Unexpected physical file in source checkout'
            assert not path.is_symlink(),'Source checkout contains a file symlink'
        assert path.name!='__pycache__' and path.suffix not in {'.pyc','.db','.log'},path
        assert not path.name.startswith('.venv'),path
    with TemporaryDirectory(dir=ROOT.parent/'tmp',prefix='source-layout-check-') as folder:
        fixture=Path(folder)/'fixture'; fixture.mkdir()
        (fixture/'main.py').write_text('VALUE = 1\n')
        (fixture/'unexpected.py').write_text('not approved\n')
        (fixture/'.env').write_text('private fixture\n')
        (fixture/'source-manifest.json').write_text(json.dumps({'files':['main.py','source-manifest.json'],'runtime':{'main.py':'app/main.py'}}))
        staged=sources.stage(Path(folder)/'staged',fixture)
        assert (staged/'main.py').is_file()
        assert not (staged/'.env').exists() and not (staged/'unexpected.py').exists()
        runner=ROOT.parent/'dists/linux/runner.py'
        sys.path.insert(0,str(runner.parent))
        spec=importlib.util.spec_from_file_location('runtime_probe',runner)
        module=importlib.util.module_from_spec(spec); spec.loader.exec_module(module)
        module.PID_FILE=Path(folder)/'pid.json'
        module.PID_FILE.write_text(json.dumps({'kind':'bs-podcasts-runtime','project':str(module.PROJECT),
            'pid':os.getpid(),'started':module.process_stamp(os.getpid())}))
        assert not module.running(module.record()),'Unrelated process matched runtime command'
        with patch.object(sys,'argv',['runner.py','--stop']), patch.object(module.signal,'pidfd_send_signal') as signal_call:
            assert module.main()==0
            assert not signal_call.called
    manifest=json.loads((ROOT.parent/'dists/linux/deployment.json').read_text())
    assert manifest['source_sha256']==sources.source_digest(ROOT),'Deployment is stale; rebuild first'
    for name,digest in manifest['files'].items():
        assert sources.file_digest(ROOT.parent/'dists/linux'/name)==digest,name
    print(f'Source-only Git: {len(files)} exact files; runtime: {len(runtime)} files; unknown/private inputs excluded; unrelated PID rejected.')


if __name__=='__main__': main()
