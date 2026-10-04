#!/usr/bin/env bash
set -euo pipefail
BS_PROJECT="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/../.." && pwd -P)"
BS_RUNTIME="$BS_PROJECT/dists/linux"
if [[ ! -x "$BS_RUNTIME/.venv/bin/python" || ! -f "$BS_RUNTIME/runner.py" ]]; then
    printf 'BS Podcasts is not built. Run %s/build.sh first.\n' "$BS_PROJECT" >&2
    exit 1
fi
unset PYTHONPATH
export PYTHONDONTWRITEBYTECODE=1
# Read-only controls and Stop must work while the application holds its lease.
for BS_ARGUMENT in "$@"; do
    case "$BS_ARGUMENT" in
        --check|--status|--stop|--help|-h)
            exec "$BS_RUNTIME/.venv/bin/python" -B "$BS_RUNTIME/runner.py" "$@" ;;
    esac
done
# Open without following or truncating a predictable lock path, then keep its
# descriptor across exec so deployed modules cannot race publication.
exec "$BS_RUNTIME/.venv/bin/python" -I -B -c '
import fcntl, os, stat, sys
project, runner = sys.argv[1:3]
os.makedirs(project+"/.cache", mode=0o700, exist_ok=True)
fd = os.open(project+"/.cache/runtime.lock", os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW | os.O_NONBLOCK, 0o600)
if not stat.S_ISREG(os.fstat(fd).st_mode):
    raise SystemExit("Runtime lock is not a regular file.")
try:
    fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
except BlockingIOError:
    # The running copy listens in the runtime temporary folder, not in the
    # one this shell passed, so resolve the instance name there.
    caller_tmp = os.environ.get("TMPDIR")
    os.environ["TMPDIR"] = project+"/tmp"
    from PySide6.QtCore import QCoreApplication
    from PySide6.QtNetwork import QLocalSocket
    import getpass
    sys.path.insert(0, os.path.dirname(runner)+"/app")
    from bs_podcasts.config import APP_ID
    app = QCoreApplication([])
    socket = QLocalSocket()
    socket.connectToServer(APP_ID+"-"+getpass.getuser())
    if socket.waitForConnected(1000):
        socket.write(b"raise")
        socket.waitForBytesWritten(1000)
        if socket.waitForReadyRead(1500) and socket.readAll().data().startswith(b"ok"):
            raise SystemExit(0)
        socket.abort()
    if caller_tmp is None:
        del os.environ["TMPDIR"]
    else:
        os.environ["TMPDIR"] = caller_tmp
    # Nothing answered. A copy that is quitting stops listening before its
    # final saves, so wait briefly for it to release the lock.
    import time
    deadline = time.monotonic() + 5
    while True:
        time.sleep(0.2)
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            break
        except BlockingIOError:
            if time.monotonic() >= deadline:
                raise SystemExit("BS Podcasts is still running or closing, or the deployment is busy; use --status or retry.")
os.set_inheritable(fd, True)
os.environ["BS_PODCASTS_RUNTIME_LOCK_FD"] = str(fd)
os.execv(sys.executable, [sys.executable, "-B", runner, *sys.argv[3:]])
' "$BS_PROJECT" "$BS_RUNTIME/runner.py" "$@"
