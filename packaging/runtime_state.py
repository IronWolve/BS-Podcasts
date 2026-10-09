"""Linux runtime ownership shared by launch, stop and deployment publication."""
from contextlib import contextmanager
import fcntl
import json
import os
from pathlib import Path
import stat
import sys
import tempfile


def process_stamp(pid):
    try:
        return Path(f'/proc/{pid}/stat').read_text().rsplit(')',1)[1].split()[19]
    except FileNotFoundError:
        return None


def read_record(project, path=None):
    """A well-formed record, including one written before the folder was renamed."""
    path = path or project/'tmp/bs-podcasts.pid.json'
    try:
        if path.is_symlink():
            return None
        value = json.loads(path.read_text())
        if (value.get('kind') == 'bs-podcasts-runtime' and isinstance(value.get('project'), str)
                and type(value.get('pid')) is int and value['pid'] > 0
                and isinstance(value.get('started'), str)):
            return value
    except (OSError, ValueError, AttributeError):
        pass
    return None


def _holds_lock(project, pid):
    """True when the process has this project's runtime lock open; None if unreadable."""
    try:
        lock = (project/'.cache/runtime.lock').stat()
    except FileNotFoundError:
        return False
    try:
        with os.scandir(f'/proc/{pid}/fd') as entries:
            for entry in entries:
                try:
                    target = os.stat(entry.path)
                except OSError:
                    continue
                if (target.st_dev, target.st_ino) == (lock.st_dev, lock.st_ino):
                    return True
    except FileNotFoundError:
        return False
    except OSError:
        return None
    return False


def process_state(project, value, runner=None):
    if value is None:
        return 'unknown'
    try:
        stamp = process_stamp(value['pid'])
        if stamp is None or stamp != value['started']:
            return 'stale'
        if value['project'] != str(project):
            # Written under the folder's previous name (or copied with it). The
            # command line still names the old path, so identify the owner by
            # the runtime lock every launched copy holds for its whole life.
            held = _holds_lock(project, value['pid'])
            if held is None:
                return 'unknown'
            return 'running' if held else 'stale'
        arguments = Path(f'/proc/{value["pid"]}/cmdline').read_bytes().split(b'\0')
    except (OSError, IndexError, KeyError):
        return 'unknown'
    expected = runner or project/'dists/linux/runner.py'
    return 'running' if os.fsencode(str(expected)) in arguments else 'unrelated'


@contextmanager
def runtime_lock(project, inherit=False):
    """Exclusive lease; inherited shell FD protects even the bootstrap import."""
    lock_path = project/'.cache/runtime.lock'
    lock_path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    inherited = os.environ.get('BS_PODCASTS_RUNTIME_LOCK_FD') if inherit else None
    own = inherited is None
    descriptor = None
    try:
        if own:
            descriptor = os.open(lock_path, os.O_RDWR | os.O_CREAT | os.O_NONBLOCK | getattr(os,'O_NOFOLLOW',0), 0o600)
        else:
            descriptor = int(inherited)
            actual, expected = os.fstat(descriptor), lock_path.lstat()
            if not stat.S_ISREG(expected.st_mode) or (actual.st_dev,actual.st_ino)!=(expected.st_dev,expected.st_ino):
                raise RuntimeError('Invalid inherited runtime lock.')
        if not stat.S_ISREG(os.fstat(descriptor).st_mode):
            raise RuntimeError('Runtime lock is not a regular file.')
        try:
            fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as exc:
            raise RuntimeError('BS Podcasts is running or its deployment is busy. Retry after it stops.') from exc
        if not own:
            # Keep the lease in this process: a browser or file manager opened
            # from the app must not inherit it and block start or build later.
            os.set_inheritable(descriptor, False)
            os.environ.pop('BS_PODCASTS_RUNTIME_LOCK_FD', None)
        yield
    finally:
        if own and descriptor is not None:
            os.close(descriptor)


def retire_stale(project, path=None):
    """Call while holding runtime_lock. Preserve unknown or live records."""
    path = path or project/'tmp/bs-podcasts.pid.json'
    if not path.exists() and not path.is_symlink():
        return False
    value = read_record(project,path)
    if value is None and not path.is_symlink() and path.is_file():
        # The exclusive runtime lease excludes a launched owner. Preserve a
        # torn record for diagnosis instead of blocking every future launch.
        try:
            json.loads(path.read_text())
        except (ValueError, UnicodeError):
            recovery = project / '.cache/runtime-record-recovery'
            recovery.mkdir(mode=0o700, parents=True, exist_ok=True)
            descriptor, saved = tempfile.mkstemp(prefix='pid-', suffix='.json', dir=recovery)
            os.close(descriptor)
            try:
                os.replace(path, saved)
            except BaseException:
                Path(saved).unlink(missing_ok=True)
                raise
            print('Recovered an incomplete PID record; original retained under .cache/runtime-record-recovery.', file=sys.stderr)
            return True
    state = process_state(project,value)
    if state != 'stale':
        raise RuntimeError(f'PID ownership is {state}; no record was removed. Inspect {path}.')
    if read_record(project,path) != value:
        raise RuntimeError('PID record changed while checking ownership.')
    path.unlink()
    return True


def write_record(project, value, path=None):
    """Publish a complete PID record atomically while holding runtime_lock."""
    path = path or project / 'tmp/bs-podcasts.pid.json'
    if path.exists() or path.is_symlink():
        raise RuntimeError('PID record already exists; nothing was replaced.')
    descriptor, name = tempfile.mkstemp(prefix='.pid-', suffix='.json', dir=path.parent)
    temporary = Path(name)
    try:
        with os.fdopen(descriptor, 'w') as handle:
            json.dump(value, handle)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
        directory = os.open(path.parent, os.O_RDONLY | os.O_DIRECTORY)
        try:
            os.fsync(directory)
        finally:
            os.close(directory)
    finally:
        temporary.unlink(missing_ok=True)
