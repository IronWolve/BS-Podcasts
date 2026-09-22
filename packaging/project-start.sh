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
os.makedirs(project+"/.cache", exist_ok=True)
fd = os.open(project+"/.cache/runtime.lock", os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW | os.O_NONBLOCK, 0o600)
if not stat.S_ISREG(os.fstat(fd).st_mode):
    raise SystemExit("Runtime lock is not a regular file.")
try:
    fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
except BlockingIOError:
    raise SystemExit("BS Podcasts is running or deployment is busy; use --status or retry.")
os.set_inheritable(fd, True)
os.environ["BS_PODCASTS_RUNTIME_LOCK_FD"] = str(fd)
os.execv(sys.executable, [sys.executable, "-B", runner, *sys.argv[3:]])
' "$BS_PROJECT" "$BS_RUNTIME/runner.py" "$@"
