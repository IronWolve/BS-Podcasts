"""Private local launcher with source isolation and exact process ownership."""
import argparse
import json
import os
from pathlib import Path
import re
import signal
import sys
import time
from runtime_state import process_stamp, read_record, process_state, runtime_lock, retire_stale

DEPLOYMENT = Path(__file__).resolve().parent
PROJECT = DEPLOYMENT.parent.parent
PID_FILE = PROJECT/'tmp/bs-podcasts.pid.json'
_RELEASE_DIR = re.compile(r'^BS-Podcasts-\d+\.\d+\.\d+-Linux-.+$')


def default_data_dir(project=None):
    """Workspace data for a checkout; a stable sibling folder for a versioned extract."""
    project = Path(project) if project is not None else PROJECT
    if _RELEASE_DIR.fullmatch(project.name) and not (project / 'repo').is_dir():
        sibling = project.parent / 'BS-Podcasts-data'
        extract = project / 'data'
        if (sibling / 'library.db').is_file():
            return sibling
        if (extract / 'library.db').is_file():
            return extract
        # Read-only legacy discovery. Do not silently create an empty library
        # when the owner's data still lives under an older versioned extract.
        previous = []
        for candidate in project.parent.iterdir():
            if candidate.is_symlink() or not _RELEASE_DIR.fullmatch(candidate.name):
                continue
            data = candidate / 'data'
            if not data.is_symlink() and (data / 'library.db').is_file():
                previous.append(data)
        if len(previous) > 1:
            raise RuntimeError('Several legacy libraries were found. Set BS_PODCASTS_DATA_DIR to the library you want; no data was moved.')
        if previous:
            return previous[0]
        return sibling
    return project / 'data'


def record():
    return read_record(PROJECT, PID_FILE)


def running(value):
    return process_state(PROJECT, value, DEPLOYMENT/'runner.py') == 'running'


def remember_caller():
    """Record the caller's temporary and cache folders before they are replaced,
    so programs the app opens can get their own back (external_environment)."""
    if 'BS_PODCASTS_CALLER_ENV' not in os.environ:
        os.environ['BS_PODCASTS_CALLER_ENV']=json.dumps({key:os.environ.get(key) for key in ('TMPDIR','XDG_CACHE_HOME')})


def environment():
    # Private from the start: later 0700 requests do not tighten existing folders.
    for directory in ('tmp','logs','.cache'):
        (PROJECT/directory).mkdir(mode=0o700,exist_ok=True)
    remember_caller()
    os.environ['TMPDIR']=str(PROJECT/'tmp')
    os.environ['XDG_CACHE_HOME']=str(PROJECT/'.cache')
    os.environ['BS_PODCASTS_CACHE_DIR']=str(PROJECT/'.cache/app')
    os.environ['BS_PODCASTS_LOG_DIR']=str(PROJECT/'logs')
    os.environ['PYTHONDONTWRITEBYTECODE']='1'
    private=PROJECT/'.config/runtime.json'
    if private.is_file():
        try:
            values=json.loads(private.read_text(encoding='utf-8')).get('environment',{})
            values=dict(values.items())
        except (OSError,ValueError,AttributeError,TypeError) as exc:
            # Name the problem, never the private values.
            raise SystemExit(f'.config/runtime.json is invalid ({type(exc).__name__}); fix or remove it. Nothing was started.') from None
        allowed={'BS_PODCASTS_GITHUB_URL','BS_PODCASTS_RELEASES_URL','BS_PODCASTS_RELEASES_API_URL','BS_PODCASTS_DATA_DIR'}
        for key,value in values.items():
            if key in allowed and isinstance(value,str):
                os.environ[key]=value
    data = Path(os.environ.get('BS_PODCASTS_DATA_DIR') or default_data_dir()).expanduser()
    if not data.is_absolute():
        data = PROJECT / data
    data.mkdir(mode=0o700, parents=True, exist_ok=True)
    os.environ['BS_PODCASTS_DATA_DIR'] = str(data)
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
                # No main window yet: a startup error dialog runs its own loop.
                modal=qt.activeModalWidget()
                if modal is not None and hasattr(modal,'reject'):
                    modal.reject()
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


class Report:
    """State lines and a stats block in the build commands' format.

    Colour only on a terminal; --plain, NO_COLOR or TERM=dumb turn it off."""
    STATES={'ok':('OK  ','32'),'warn':('WARN','33'),'fail':('FAIL','31'),'info':('    ','')}

    def __init__(self,plain=False):
        self.colour=(not plain and not os.environ.get('NO_COLOR') and os.environ.get('TERM')!='dumb'
                     and sys.stdout.isatty())
        self.started=time.monotonic()

    def paint(self,code,text):
        return f'\033[{code}m{text}\033[0m' if self.colour and code else text

    def line(self,state,text):
        label,code=self.STATES[state]
        print(self.paint(code,label)+' '+text,flush=True)

    def stats(self,rows):
        rows=[*rows,('elapsed',f'{time.monotonic()-self.started:.1f} s')]
        width=max(len(label) for label,_ in rows)
        print(self.paint('1','Stats'))
        for label,value in rows:
            print(f'  {label:<{width}}  {value}')
        sys.stdout.flush()


def _size(path):
    """File count and human size under path, without following links."""
    files=total=0
    for root,_,names in os.walk(path):
        for name in names:
            entry=os.path.join(root,name)
            try:
                if not os.path.islink(entry):
                    total+=os.path.getsize(entry); files+=1
            except OSError:
                pass
    value=float(total)
    for unit in ('bytes','KB','MB','GB'):
        if value<1024 or unit=='GB':
            break
        value/=1024
    return f'{files:,} files, '+(f'{total:,} bytes' if unit=='bytes' else f'{value:.1f} {unit}')


def _describe(args,report):
    module=environment()
    current=record()
    print(report.paint('1;36',f'BS Podcasts {module.__version__}'),flush=True)
    for label,value in (('Runtime',DEPLOYMENT),('Python',sys.version.split()[0]),
                        ('Data',os.environ['BS_PODCASTS_DATA_DIR']),('Log',PROJECT/'logs/bs-podcasts.log')):
        report.line('info',f'{label}: {value}')
    live=running(current)
    report.line('ok' if live else 'info',f'State: running (PID {current["pid"]})' if live else 'State: stopped')
    return current if live else None


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--plain',action='store_true')
    parser.add_argument('--status',action='store_true')
    parser.add_argument('--check',action='store_true')
    parser.add_argument('--stop',action='store_true')
    args,extra=parser.parse_known_args()
    report=Report(plain=args.plain)
    current=record()
    if args.stop:
        if not running(current):
            retired=False
            if process_state(PROJECT,current)=='stale':
                with runtime_lock(PROJECT):
                    retired=retire_stale(PROJECT,PID_FILE)
            report.line('info','BS Podcasts is not running from this deployment.')
            report.stats([('stopped','nothing (no running copy)'),('stale record','retired' if retired else 'none')])
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
        signalled=time.monotonic()
        deadline=signalled+5
        while running(current) and time.monotonic()<deadline:
            time.sleep(.1)
        stopped=not running(current)
        if stopped:
            report.line('ok','Stopped.')
        else:
            report.line('fail','Still stopping; inspect the project logs. No forced kill was sent.')
        report.stats([('process',f'PID {current["pid"]}'),('result','stopped' if stopped else 'still running'),
                      ('wait',f'{time.monotonic()-signalled:.1f} s of 5.0 s')])
        return 0 if stopped else 1
    if args.status or args.check:
        live=_describe(args,report)
        if args.check:
            from tempfile import TemporaryDirectory
            from bs_podcasts.selfcheck import main as check
            with TemporaryDirectory(prefix='runtime-check-', dir=PROJECT/'tmp') as folder:
                previous = sys.argv
                try:
                    sys.argv = ['selfcheck', '--self-check-dir', folder, *(['--plain'] if args.plain else [])]
                    return check()
                finally:
                    sys.argv = previous
        report.stats([('state',f'running, PID {live["pid"]}' if live else 'stopped'),
                      ('data',_size(os.environ['BS_PODCASTS_DATA_DIR'])),('logs',_size(PROJECT/'logs'))])
        return 0
    with runtime_lock(PROJECT,inherit=True):
        _describe(args,report)
        retire_stale(PROJECT,PID_FILE)
        value={'kind':'bs-podcasts-runtime','project':str(PROJECT),'pid':os.getpid(),'started':process_stamp(os.getpid())}
        signals={}
        try:
            with PID_FILE.open('x') as handle:
                json.dump(value,handle)
            requested=[False]
            for kind in (signal.SIGTERM,signal.SIGINT):
                signals[kind]=signal.signal(kind,lambda *_:requested.__setitem__(0,True))
            report.line('ok',f'Starting PID {os.getpid()} (local desktop; no public endpoint).')
            return _run_gui(extra,requested)
        finally:
            for kind,handler in signals.items():
                signal.signal(kind,handler)
            if record()==value:
                PID_FILE.unlink(missing_ok=True)


if __name__=='__main__':
    raise SystemExit(main())
