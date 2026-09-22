"""C05/C06/C07: fake projects, real locks and snapshots; no GUI or signals."""
from contextlib import ExitStack
import importlib.util
import json
import os
from pathlib import Path
import shlex
import shutil
import subprocess
import sys
from tempfile import TemporaryDirectory
from types import SimpleNamespace as NS
from unittest.mock import patch

SOURCE=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(SOURCE/'packaging'))
import deploy
import source_manifest as sources
import runtime_state as state
import runtime_runner as runner


def fixture(root):
    repo=root/'repo'; repo.mkdir()
    (root/'tmp').mkdir(); (root/'.cache').mkdir()
    marker=root/'dists/linux/.venv/bin/python'; marker.parent.mkdir(parents=True)
    marker.write_text('fixture marker, not an environment')
    (repo/'pyproject.toml').write_text('[project]\nversion="0.0.1"\n')
    (repo/'src').mkdir(); (repo/'src/demo.py').write_text('VALUE = 1\n')
    (repo/'source-manifest.json').write_text(json.dumps({'files':['source-manifest.json','pyproject.toml','src/demo.py'],
        'runtime':{'src/demo.py':'app/demo.py'}}))
    old=root/'dists/linux/app'; old.mkdir(); (old/'demo.py').write_text('VALUE = 0\n')
    return repo


def ownership(root,repo):
    pid=root/'tmp/bs-podcasts.pid.json'
    with ExitStack() as stack:
        stack.enter_context(patch.object(runner,'PROJECT',root))
        stack.enter_context(patch.object(runner,'DEPLOYMENT',root/'dists/linux'))
        stack.enter_context(patch.object(runner,'PID_FILE',pid))
        stack.enter_context(patch.object(runner,'environment',lambda:NS(__version__='fixture')))
        stack.enter_context(patch.dict(os.environ,{'BS_PODCASTS_DATA_DIR':str(root/'data')}))
        stack.enter_context(patch.object(sys,'argv',['runner.py','--plain']))
        with patch.object(runner,'_run_gui',side_effect=ImportError('injected initialization failure')):
            try: runner.main()
            except ImportError: pass
            else: raise AssertionError('failure not raised')
        assert not pid.exists()
        pid.write_text(json.dumps({'kind':'bs-podcasts-runtime','project':str(root),'pid':2**30,'started':'0'}))
        with patch.object(sys,'argv',['runner.py','--stop']),patch.object(runner.signal,'pidfd_send_signal') as send:
            assert runner.main()==0 and not send.called
        assert not pid.exists()
        pid.write_text('unrecognized')
        with state.runtime_lock(root):
            try: state.retire_stale(root)
            except RuntimeError: pass
            else: raise AssertionError('unknown ownership was discarded')
        assert pid.read_text()=='unrecognized'
        pid.unlink()  # owned fixture only


def publication(root,repo,change):
    original=shutil.copy2; lease=[]
    def copy(source,target,*args,**kwargs):
        result=original(source,target,*args,**kwargs)
        if Path(target).parts[-2:]==('app','demo.py'):
            if change=='start':
                guard=state.runtime_lock(root); guard.__enter__(); lease.append(guard)
            else:
                (repo/'src/demo.py').write_text('VALUE = 2\n')
        return result
    expected=sources.source_digest(repo)
    try:
        with patch.object(deploy,'ROOT',repo),patch.object(deploy.shutil,'copy2',side_effect=copy):
            if change=='start':
                try: deploy.deploy()
                except RuntimeError as exc: assert 'running' in str(exc)
                else: raise AssertionError('published through runtime lease')
                assert (root/'dists/linux/app/demo.py').read_text()=='VALUE = 0\n'
            else:
                deploy.deploy()
                data=json.loads((root/'dists/linux/deployment.json').read_text())
                assert data['source_sha256']==expected
                assert data['source_sha256']!=sources.source_digest(repo)
                assert (root/'dists/linux/app/demo.py').read_text()=='VALUE = 1\n'
    finally:
        for guard in lease: guard.__exit__(None,None,None)


def bootstrap(root):
    packaging=root/'repo/packaging'; packaging.mkdir(parents=True)
    shutil.copy2(SOURCE/'packaging/project-start.sh',packaging/'project-start.sh')
    target=root/'dists/linux'; (target/'.venv/bin').mkdir(parents=True)
    # A test forwarding script, not a new virtual environment.
    python=target/'.venv/bin/python'
    python.write_text('#!/bin/sh\nexec '+shlex.quote(sys.executable)+' "$@"\n'); python.chmod(0o700)
    shutil.copy2(SOURCE/'packaging/runtime_state.py',target/'runtime_state.py')
    (target/'runner.py').write_text('from pathlib import Path\nfrom runtime_state import runtime_lock\np=Path(__file__).resolve().parents[2]\nwith runtime_lock(p,inherit=True):\n    print("inherited lease verified")\n')
    result=subprocess.run(['bash',str(packaging/'project-start.sh')],capture_output=True,text=True,timeout=3)
    assert result.returncode==0,(result.stdout,result.stderr)
    assert 'inherited lease verified' in result.stdout


def main():
    with TemporaryDirectory(dir=SOURCE.parent/'tmp',prefix='runtime-cseries-') as folder:
        for kind in ('pid','start','digest'):
            root=Path(folder)/kind; root.mkdir(); repo=fixture(root)
            if kind=='pid': ownership(root,repo)
            else: publication(root,repo,kind)
        bootstrap(Path(folder)/'bootstrap')
    print('C05/C06/C07: PASS startup failure cleanup, safe stale retirement, unknown-record preservation, runtime exclusion, immutable source digest and shell lock inheritance')


if __name__=='__main__': main()
