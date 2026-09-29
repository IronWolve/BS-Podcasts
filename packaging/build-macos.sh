#!/usr/bin/env bash
set -euo pipefail
BS_REPO="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd -P)"
BS_ROOT="${BS_PODCASTS_BUILD_ROOT:-$(cd -- "$BS_REPO/.." && pwd -P)}"
BS_VENV="${BS_PODCASTS_MAC_VENV:-$BS_ROOT/.cache/macos-build/.venv}"
[[ -x "$BS_VENV/bin/python" ]] || { printf 'Prepare the approved project-local Mac environment first: %s\n' "$BS_VENV" >&2; exit 1; }
mkdir -p "$BS_ROOT/tmp" "$BS_ROOT/.cache" "$BS_ROOT/logs"
export TMPDIR="$BS_ROOT/tmp" PYTHONDONTWRITEBYTECODE=1
unset PYTHONPATH
exec "$BS_VENV/bin/python" -B "$BS_REPO/packaging/build_macos.py" "$@"
