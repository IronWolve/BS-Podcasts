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
exec "$BS_RUNTIME/.venv/bin/python" -B "$BS_RUNTIME/runner.py" "$@"
