"""Linux runtime ownership shared by launch, stop and deployment publication."""
from contextlib import contextmanager
import fcntl
import json
import os
from pathlib import Path
import stat


def process_stamp(pid):
    try:
        return Path(f'/proc/{pid}/stat').read_text().rsplit(')',1)[1].split()[19]
    except FileNotFoundError:
        return None


def read_record(project, path=None):
    path = path or project/'tmp/bs-podcasts.pid.json'
    try:
        if path.is_symlink():
            return None
        value = json.loads(path.read_text())
        if (value.get('kind') == 'bs-podcasts-runtime' and value.get('project') == str(project)
                and type(value.get('pid')) is int and value['pid'] > 0
                and isinstance(value.get('started'), str)):
            return value
    except (OSError, ValueError, AttributeError):
        pass
    return None


def process_state(project, value, runner=None):
    if value is None:
        return 'unknown'
    try:
        stamp = process_stamp(value['pid'])
        if stamp is None or stamp != value['started']:
            return 'stale'
        arguments = Path(f'/proc/{value["pid"]}/cmdline').read_bytes().split(b'\0')
    except (OSError, IndexError, KeyError):
        return 'unknown'
    expected = runner or project/'dists/linux/runner.py'
    return 'running' if os.fsencode(str(expected)) in arguments else 'unrelated'


@contextmanager
def runtime_lock(project, inherit=False):
    """Exclusive lease; inherited shell FD protects even the bootstrap import."""
    lock_path = project/'.cache/runtime.lock'
    lock_path.parent.mkdir(parents=True, exist_ok=True)
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
    state = process_state(project,value)
    if state != 'stale':
        raise RuntimeError(f'PID ownership is {state}; no record was removed. Inspect {path}.')
    if read_record(project,path) != value:
        raise RuntimeError('PID record changed while checking ownership.')
    path.unlink()
    return True
