"""Private local launcher with source isolation and exact process ownership."""
import argparse
import json
import os
from pathlib import Path
import signal
import sys
import time

DEPLOYMENT = Path(__file__).resolve().parent
PROJECT = DEPLOYMENT.parent.parent
PID_FILE = PROJECT/'tmp/bs-podcasts.pid.json'


def process_stamp(pid):
    try:
        return Path(f'/proc/{pid}/stat').read_text().rsplit(')',1)[1].split()[19]
    except (OSError,IndexError):
        return None


def record():
    try:
        value=json.loads(PID_FILE.read_text())
        return value if value.get('kind')=='bs-podcasts-runtime' and value.get('project')==str(PROJECT) else None
    except (OSError,ValueError,AttributeError):
        return None


def running(value):
    if not value or not isinstance(value.get('pid'),int):
        return False
    try:
        args=Path(f'/proc/{value["pid"]}/cmdline').read_bytes().split(b'\0')
    except OSError:
        return False
    return (process_stamp(value['pid'])==value.get('started') and
            os.fsencode(str(DEPLOYMENT/'runner.py')) in args)


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
    import bs_podcasts
    if not Path(bs_podcasts.__file__).resolve().is_relative_to(DEPLOYMENT/'app'):
        raise RuntimeError('Refusing an application import outside the deployment.')
    return bs_podcasts


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
            print('BS Podcasts is not running from this deployment.')
            return 0
        # A pidfd prevents PID reuse between validation and signalling.
        if not hasattr(os,'pidfd_open') or not hasattr(signal,'pidfd_send_signal'):
            raise RuntimeError('Safe process signalling requires Linux pidfd support; nothing was stopped.')
        descriptor=os.pidfd_open(current['pid'])
        try:
            if not running(current):
                print('The recorded process has exited; nothing was stopped.')
                return 0
            signal.pidfd_send_signal(descriptor,signal.SIGTERM)
        finally:
            os.close(descriptor)
        deadline=time.monotonic()+5
        while running(current) and time.monotonic()<deadline:
            time.sleep(.1)
        print('Stopped.' if not running(current) else 'Still stopping; inspect the project logs. No forced kill was sent.')
        return 0 if not running(current) else 1
    module=environment()
    color='\033[1;36m' if sys.stdout.isatty() and not args.plain and 'NO_COLOR' not in os.environ else ''
    reset='\033[0m' if color else ''
    print(f'{color}BS Podcasts {module.__version__}{reset}',flush=True)
    print(f'Runtime: {DEPLOYMENT}\nPython: {sys.version.split()[0]}\nData: {os.environ["BS_PODCASTS_DATA_DIR"]}\nLog: {PROJECT/"logs/bs-podcasts.log"}',flush=True)
    print(f'State: running (PID {current["pid"]})' if running(current) else 'State: stopped',flush=True)
    if args.check or args.status or running(current):
        return 0
    if PID_FILE.exists():
        if current is None:
            raise RuntimeError('Unrecognized PID file; inspect it before launch.')
        PID_FILE.unlink()
    value={'kind':'bs-podcasts-runtime','project':str(PROJECT),'pid':os.getpid(),'started':process_stamp(os.getpid())}
    with PID_FILE.open('x') as handle:
        json.dump(value,handle)
    requested=[False]
    signal.signal(signal.SIGTERM,lambda *_:requested.__setitem__(0,True))
    signal.signal(signal.SIGINT,lambda *_:requested.__setitem__(0,True))
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
    sys.argv=[sys.argv[0],*extra]
    print(f'Starting PID {os.getpid()} (local desktop; no public endpoint).',flush=True)
    try:
        return app.main()
    finally:
        if record()==value:
            PID_FILE.unlink(missing_ok=True)


if __name__=='__main__':
    raise SystemExit(main())
