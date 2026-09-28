#!/usr/bin/env bash
set -euo pipefail
BS_PROJECT="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd -P)"
BS_RUNTIME="$BS_PROJECT/dists/linux"
BS_PYTHON="${BS_PODCASTS_PYTHON:-python3.14}"
if ! "$BS_PYTHON" -c 'import sys; raise SystemExit(sys.version_info[:3] < (3, 14, 7))'; then
    printf 'Python 3.14.7+ is required. Set BS_PODCASTS_PYTHON to its executable.\n' >&2
    exit 1
fi
umask 077
mkdir -p "$BS_PROJECT/tmp" "$BS_PROJECT/.cache" "$BS_PROJECT/.config" "$BS_PROJECT/logs" "$BS_PROJECT/data"
export TMPDIR="$BS_PROJECT/tmp" PIP_CACHE_DIR="$BS_PROJECT/.cache/pip" PYTHONDONTWRITEBYTECODE=1
unset PYTHONPATH
if [[ ! -d "$BS_RUNTIME/.venv" ]]; then
    "$BS_PYTHON" -m venv "$BS_RUNTIME/.venv"
fi
if [[ ! -x "$BS_RUNTIME/.venv/bin/python" ]]; then
    printf 'The local environment is incomplete. Preserve/rename dists/linux/.venv and rerun setup.\n' >&2
    exit 1
fi
"$BS_RUNTIME/.venv/bin/python" -m pip install --disable-pip-version-check --only-binary=:all: \
    -r "$BS_RUNTIME/requirements-linux.lock"
"$BS_RUNTIME/.venv/bin/python" -m pip check
printf '\nLocal environment prepared. libmpv and its system dependencies must also be installed.\n'
printf 'Check: bash "%s/start.sh" --check --plain\nStart: bash "%s/start.sh"\n' "$BS_PROJECT" "$BS_PROJECT"
printf 'After moving this application, preserve/rename the old .venv and rerun setup. No app was started.\n'
