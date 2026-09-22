#!/usr/bin/env bash
set -euo pipefail
BS_PROJECT="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/../.." && pwd -P)"
if [[ ! -f "$BS_PROJECT/dists/linux/runner.py" ]]; then
    printf 'No built Linux runtime is available. Nothing was stopped.\n'
    exit 0
fi
unset PYTHONPATH
export PYTHONDONTWRITEBYTECODE=1
exec "$BS_PROJECT/dists/linux/.venv/bin/python" -B "$BS_PROJECT/dists/linux/runner.py" --stop "$@"
