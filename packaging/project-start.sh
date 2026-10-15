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
import hashlib, json
project, runner = sys.argv[1:3]
os.makedirs(project+"/.cache", mode=0o700, exist_ok=True)
fd = os.open(project+"/.cache/runtime.lock", os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW | os.O_NONBLOCK, 0o600)
if not stat.S_ISREG(os.fstat(fd).st_mode):
    raise SystemExit("Runtime lock is not a regular file.")
try:
    fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
except BlockingIOError:
    # The running copy listens in the runtime temporary folder, not in the
    # one this shell passed, so look for its endpoint there.
    caller_tmp = os.environ.get("TMPDIR")
    os.environ["TMPDIR"] = project+"/tmp"
    from PySide6.QtCore import QCoreApplication
    from PySide6.QtNetwork import QLocalSocket
    sys.path.insert(0, os.path.dirname(runner)+"/app")
    from bs_podcasts.config import APP_ID
    from bs_podcasts.config import data_dir as application_data_dir
    incoming = [os.path.abspath(path) for path in sys.argv[3:]]
    profile_path = os.path.normcase(os.path.abspath(str(application_data_dir() / "library.db")))
    profile = hashlib.sha256(os.fsencode(profile_path)).hexdigest()
    payload = (json.dumps({"kind": "file-arguments", "profile": profile, "paths": incoming},
                          ensure_ascii=False, separators=(",", ":")).encode("utf-8") + b"\n"
               if incoming else b"raise " + profile.encode("ascii") + b"\n")
    app = QCoreApplication([])
    def endpoints():
        # The application names its endpoint (user and library), so the name
        # is not rebuilt here: the running copy listens in this folder.
        found = []
        try:
            for name in sorted(os.listdir(project+"/tmp")):
                if name.startswith(APP_ID+"-") and stat.S_ISSOCK(os.lstat(project+"/tmp/"+name).st_mode):
                    found.append(name)
        except OSError:
            pass
        return found
    def left():
        return max(0, int((deadline - time.monotonic()) * 1000))
    def raised():
        # Each wait is capped by the time left, so a silent socket cannot run past the deadline.
        for name in endpoints():
            if left() == 0:
                break
            socket = QLocalSocket()
            socket.connectToServer(name)
            if socket.waitForConnected(min(1000, left())):
                socket.write(payload)
                socket.waitForBytesWritten(min(1000, left()))
                reply = b""
                if socket.waitForReadyRead(min(1500, left())):
                    reply = socket.readAll().data()
                accepted = reply.startswith(b"files-queued") if incoming else reply.startswith(b"ok")
                if accepted:
                    return True
            socket.abort()
        return False
    # A copy that is still starting begins listening a moment later; one that
    # is quitting stops listening before its final saves and then frees the
    # lock. Ask again every round, so either way this ends as soon as it can.
    # The deadline is checked before each round, and no sleep runs past it.
    import time
    deadline = time.monotonic() + 5
    while True:
        if time.monotonic() < deadline and raised():
            raise SystemExit(0)
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            break
        except BlockingIOError:
            if time.monotonic() >= deadline:
                raise SystemExit("BS Podcasts is still running or closing, or the deployment is busy; use --status or retry.")
        time.sleep(min(0.2, max(0.0, deadline - time.monotonic())))
    if caller_tmp is None:
        del os.environ["TMPDIR"]
    else:
        os.environ["TMPDIR"] = caller_tmp
os.set_inheritable(fd, True)
os.environ["BS_PODCASTS_RUNTIME_LOCK_FD"] = str(fd)
os.execv(sys.executable, [sys.executable, "-B", runner, *sys.argv[3:]])
' "$BS_PROJECT" "$BS_RUNTIME/runner.py" "$@"
