"""Private local launcher with source isolation and exact process ownership."""
import argparse
import json
import os
from pathlib import Path
import shutil
import signal
import sys
import time
from runtime_state import LOG_NAME, PID_NAME, RUNTIME_KIND, process_stamp, read_record, process_state, runtime_lock, retire_stale, write_record
from report import Reporter, size_text, tree_size

DEPLOYMENT = Path(__file__).resolve().parent
PROJECT = DEPLOYMENT.parent.parent
PID_FILE = PROJECT/'.cache'/PID_NAME
_LIBRARY_FILES = ('library.db', 'library.db-wal', 'library.db-shm')
_ADOPTED = 'library-adopted.json'
# (state, text) lines for the console, filled while the data folder is chosen.
NOTICES = []


def _is_extract(folder):
    """A release extract carries the files its archive writes at the top and has
    no source checkout. The folder's own name is never consulted."""
    return (not (folder / 'repo').is_dir()
            and (folder / 'INSTALL.txt').is_file() and (folder / 'setup.sh').is_file())


def default_data_dir(project=None):
    """Workspace data for a checkout; a stable sibling folder for a release extract."""
    project = Path(project) if project is not None else PROJECT
    if _is_extract(project):
        sibling = project.parent / 'BS-Podcasts-data'
        extract = project / 'data'
        if (sibling / 'library.db').is_file():
            return sibling
        if (extract / 'library.db').is_file():
            return extract
        if (sibling / _ADOPTED).is_file():
            return sibling
        # Read-only legacy discovery. Do not silently create an empty library
        # when the owner's data still lives under an older release extract.
        previous = []
        for candidate in project.parent.iterdir():
            data = candidate / 'data'
            if (candidate.is_symlink() or data.is_symlink() or not (data / 'library.db').is_file()
                    or not _is_extract(candidate)):
                continue
            previous.append(data)
        if len(previous) > 1:
            raise RuntimeError('Several legacy libraries were found. Set BS_PODCASTS_DATA_DIR to the library you want; no data was moved.')
        if previous:
            return previous[0]
        return sibling
    return project / 'data'


def adopt_library(found, project=None):
    """Copy an older extract's library into the shared data folder, once.

    `found` is the folder default_data_dir() chose. Anything but an older
    extract's folder is returned unchanged. The original is never changed or
    removed; when the copy cannot be made safely it keeps being used in place."""
    project = Path(project) if project is not None else PROJECT
    sibling = project.parent / 'BS-Podcasts-data'
    if not _is_extract(project) or found in (sibling, project / 'data'):
        return found
    names = [name for name in _LIBRARY_FILES if (found / name).is_file() and not (found / name).is_symlink()]
    staged, placed, lock = [], [], None
    try:
        sibling.mkdir(mode=0o700, parents=True, exist_ok=True)
        clash = [name for name in names if (sibling / name).exists() or (sibling / name).is_symlink()]
        if clash:
            raise RuntimeError('the shared folder already holds ' + ', '.join(clash))
        try:
            lock = open(found / 'library.db.use-lock', 'rb')
        except FileNotFoundError:
            pass
        if lock is not None:
            import fcntl
            try:
                fcntl.flock(lock, fcntl.LOCK_SH | fcntl.LOCK_NB)
            except BlockingIOError:
                raise RuntimeError('it is open in a running copy of BS Podcasts; close that copy first') from None
        sizes = {}
        for name in names:
            temporary = sibling / f'.adopting-{os.getpid()}-{name}'
            staged.append(temporary)
            descriptor = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
            with open(found / name, 'rb') as reader, os.fdopen(descriptor, 'wb') as writer:
                shutil.copyfileobj(reader, writer, 1 << 20)
                writer.flush()
                os.fsync(writer.fileno())
            sizes[name] = temporary.stat().st_size
            if sizes[name] != (found / name).stat().st_size:
                raise RuntimeError(f'{name} changed while it was being copied')
        # library.db goes last: its presence is what marks the shared library as complete.
        for name in reversed(names):
            target = sibling / name
            os.replace(sibling / f'.adopting-{os.getpid()}-{name}', target)
            placed.append(target)
        note = sibling / f'.adopting-{os.getpid()}-record'
        staged.append(note)
        descriptor = os.open(note, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
        with os.fdopen(descriptor, 'w', encoding='utf-8') as handle:
            json.dump({'source': str(found), 'files': sizes, 'adopted': time.strftime('%Y-%m-%dT%H:%M:%S%z')}, handle, indent=2)
        os.replace(note, sibling / _ADOPTED)
    except (OSError, RuntimeError) as exc:
        for path in (*staged, *placed):
            try:
                path.unlink(missing_ok=True)
            except OSError:
                pass
        NOTICES.append(('warn', f'The library in {found} could not be copied to {sibling} ({exc}); using it in place.'))
        return found
    finally:
        if lock is not None:
            lock.close()
    NOTICES.append(('ok', f'Library copied from {found} to {sibling} ({len(names)} files, {sum(sizes.values()):,} bytes). The original was left in place.'))
    NOTICES.append(('warn', f'Downloaded episodes and other files in {found} were not copied; do not delete that folder until you have moved what you need.'))
    return sibling


def _legacy_pid_file():
    # Earlier launchers kept the record in tmp/, which may be cleared while the app runs.
    return PROJECT/'tmp'/PID_NAME


def record():
    return read_record(PROJECT, PID_FILE) or read_record(PROJECT, _legacy_pid_file())


def retire_records():
    """Call while holding runtime_lock. Retire a stale record, here or in the old place."""
    retired = retire_stale(PROJECT, PID_FILE)
    if _legacy_pid_file() != PID_FILE:
        retired = retire_stale(PROJECT, _legacy_pid_file()) or retired
    return retired


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
    chosen = os.environ.get('BS_PODCASTS_DATA_DIR')
    data = Path(chosen or default_data_dir()).expanduser()
    if not data.is_absolute():
        data = PROJECT / data
    if not chosen:
        data = adopt_library(data)
        if _is_extract(PROJECT) and not (data / 'library.db').is_file():
            NOTICES.append(('info', f'No existing library was found; a new one is created in {data}. If you expected existing data, set BS_PODCASTS_DATA_DIR to its folder.'))
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
                        # Ask once per stop request; the window retries its own close
                        # while saves finish, and asking again would stack those retries.
                        requested[0]=False
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


def _size(path):
    """File count and human size under path, without following links."""
    files,total=tree_size(path)
    # The launcher's compact stats omit the exact-byte suffix used by builds.
    return f'{files:,} files, '+size_text(total).split(' (',1)[0]


def _describe(args,report):
    module=environment()
    current=record()
    print(report._paint('1;36',f'BS Podcasts {module.__version__}'),flush=True)
    for label,value in (('Runtime',DEPLOYMENT),('Python',sys.version.split()[0]),
                        ('Data',os.environ['BS_PODCASTS_DATA_DIR']),('Log',PROJECT/'logs'/LOG_NAME)):
        report.line('info',f'{label}: {value}')
    for state,text in NOTICES:
        report.line(state,text)
    NOTICES.clear()
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
    report=Reporter(plain=args.plain)
    app_started=[]
    try:
        return _launch(args,extra,report,app_started)
    except RuntimeError as exc:
        # A refusal by the launcher is one plain line. An error from the
        # application or its self-check keeps its traceback.
        if app_started:
            raise
        report.line('fail',str(exc))
        return 1


def _launch(args,extra,report,app_started):
    current=record()
    if args.stop:
        if not running(current):
            retired=False
            if process_state(PROJECT,current)=='stale':
                with runtime_lock(PROJECT):
                    retired=retire_records()
            report.line('info','BS Podcasts is not running from this deployment.')
            report.stats([('stopped','nothing (no running copy)'),('stale record','retired' if retired else 'none')])
            return 0
        if not hasattr(os,'pidfd_open') or not hasattr(signal,'pidfd_send_signal'):
            raise RuntimeError('Safe process signalling requires Linux pidfd support; nothing was stopped.')
        try:
            descriptor=os.pidfd_open(current['pid'])
            try:
                if not running(current):
                    return 0
                signal.pidfd_send_signal(descriptor,signal.SIGTERM)
            finally:
                os.close(descriptor)
        except ProcessLookupError:
            # It exited between the running check and the signal: it is stopped.
            pass
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
            app_started.append(True)
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
        retire_records()
        value={'kind':RUNTIME_KIND,'project':str(PROJECT),'pid':os.getpid(),'started':process_stamp(os.getpid())}
        signals={}
        try:
            write_record(PROJECT, value, PID_FILE)
            requested=[False]
            for kind in (signal.SIGTERM,signal.SIGINT):
                signals[kind]=signal.signal(kind,lambda *_:requested.__setitem__(0,True))
            report.line('ok',f'Starting PID {os.getpid()} (local desktop; no public endpoint).')
            app_started.append(True)
            return _run_gui(extra,requested)
        finally:
            for kind,handler in signals.items():
                signal.signal(kind,handler)
            if record()==value:
                PID_FILE.unlink(missing_ok=True)


if __name__=='__main__':
    raise SystemExit(main())
