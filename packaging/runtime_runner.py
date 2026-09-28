"""Private local launcher with source isolation and exact process ownership."""
import argparse
import json
import os
from pathlib import Path
import signal
import sys
import time
from runtime_state import process_stamp, read_record, process_state, runtime_lock, retire_stale

DEPLOYMENT = Path(__file__).resolve().parent
PROJECT = DEPLOYMENT.parent.parent
PID_FILE = PROJECT/'tmp/bs-podcasts.pid.json'


def record():
    return read_record(PROJECT, PID_FILE)


def running(value):
    return process_state(PROJECT, value, DEPLOYMENT/'runner.py') == 'running'


def environment():
    for directory in ('tmp','logs','.cache','data'):
        (PROJECT/directory).mkdir(exist_ok=True)
    os.environ['TMPDIR']=str(PROJECT/'tmp')
    os.environ['XDG_CACHE_HOME']=str(PROJECT/'.cache')
    os.environ['BS_PODCASTS_CACHE_DIR']=str(PROJECT/'.cache/app')
    os.environ['BS_PODCASTS_LOG_DIR']=str(PROJECT/'logs')
    os.environ.setdefault('BS_PODCASTS_DATA_DIR',str(PROJECT/'data'))
    os.environ['PYTHONDONTWRITEBYTECODE']='1'
    private=PROJECT/'.config/runtime.json'
    if private.is_file():
        values=json.loads(private.read_text()).get('environment',{})
        allowed={'BS_PODCASTS_GITHUB_URL','BS_PODCASTS_RELEASES_URL','BS_PODCASTS_RELEASES_API_URL','BS_PODCASTS_DATA_DIR'}
        for key,value in values.items():
            if key in allowed and isinstance(value,str):
                os.environ[key]=value
    source=(PROJECT/'repo').resolve()
    sys.path[:]=[p for p in sys.path if p and not Path(p).resolve().is_relative_to(source)]
    sys.path.insert(0,str(DEPLOYMENT/'app'))
    from runtime_linux import bind_native
    bind_native(DEPLOYMENT/'native/libmpv.so.2')
    import bs_podcasts
    if not Path(bs_podcasts.__file__).resolve().is_relative_to(DEPLOYMENT/'app'):
        raise RuntimeError('Refusing an application import outside the deployment.')
    return bs_podcasts


def _run_gui(extra, requested):
    from bs_podcasts import app
    original=app.create_application
    def create(argv=None):
        qt=original(argv)
        from PySide6.QtCore import QTimer
        def poll():
            if requested[0]:
                for window in qt.topLevelWidgets():
                    quit_window=getattr(window,'_request_quit',None)
                    if quit_window is not None:
                        quit_window()
                        return
        timer=QTimer(qt); timer.timeout.connect(poll); timer.start(100)
        qt._project_stop_timer=timer
        return qt
    app.create_application=create
    original_argv=sys.argv
    sys.argv=[sys.argv[0],*extra]
    try:
        return app.main()
    finally:
        app.create_application=original
        sys.argv=original_argv


def _describe(args):
    module=environment()
    current=record()
    color='\033[1;36m' if sys.stdout.isatty() and not args.plain and 'NO_COLOR' not in os.environ else ''
    reset='\033[0m' if color else ''
    print(f'{color}BS Podcasts {module.__version__}{reset}',flush=True)
    print(f'Runtime: {DEPLOYMENT}\nPython: {sys.version.split()[0]}\nData: {os.environ["BS_PODCASTS_DATA_DIR"]}\nLog: {PROJECT/"logs/bs-podcasts.log"}',flush=True)
    print(f'State: running (PID {current["pid"]})' if running(current) else 'State: stopped',flush=True)


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--plain',action='store_true')
    parser.add_argument('--status',action='store_true')
    parser.add_argument('--check',action='store_true')
    parser.add_argument('--stop',action='store_true')
    args,extra=parser.parse_known_args()
    current=record()
    if args.stop:
        if not running(current):
            if process_state(PROJECT,current)=='stale':
                with runtime_lock(PROJECT):
                    retire_stale(PROJECT,PID_FILE)
            print('BS Podcasts is not running from this deployment.')
            return 0
        if not hasattr(os,'pidfd_open') or not hasattr(signal,'pidfd_send_signal'):
            raise RuntimeError('Safe process signalling requires Linux pidfd support; nothing was stopped.')
        descriptor=os.pidfd_open(current['pid'])
        try:
            if not running(current):
                return 0
            signal.pidfd_send_signal(descriptor,signal.SIGTERM)
        finally:
            os.close(descriptor)
        deadline=time.monotonic()+5
        while running(current) and time.monotonic()<deadline:
            time.sleep(.1)
        print('Stopped.' if not running(current) else 'Still stopping; inspect the project logs. No forced kill was sent.')
        return 0 if not running(current) else 1
    if args.status or args.check:
        _describe(args)
        if args.check:
            from tempfile import TemporaryDirectory
            from bs_podcasts.selfcheck import main as check
            with TemporaryDirectory(prefix='runtime-check-', dir=PROJECT/'tmp') as folder:
                previous = sys.argv
                try:
                    sys.argv = ['selfcheck', '--self-check-dir', folder]
                    return check()
                finally:
                    sys.argv = previous
        return 0
    with runtime_lock(PROJECT,inherit=True):
        _describe(args)
        retire_stale(PROJECT,PID_FILE)
        value={'kind':'bs-podcasts-runtime','project':str(PROJECT),'pid':os.getpid(),'started':process_stamp(os.getpid())}
        signals={}
        try:
            with PID_FILE.open('x') as handle:
                json.dump(value,handle)
            requested=[False]
            for kind in (signal.SIGTERM,signal.SIGINT):
                signals[kind]=signal.signal(kind,lambda *_:requested.__setitem__(0,True))
            print(f'Starting PID {os.getpid()} (local desktop; no public endpoint).',flush=True)
            return _run_gui(extra,requested)
        finally:
            for kind,handler in signals.items():
                signal.signal(kind,handler)
            if record()==value:
                PID_FILE.unlink(missing_ok=True)


if __name__=='__main__':
    raise SystemExit(main())
