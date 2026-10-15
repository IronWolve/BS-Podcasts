#!/usr/bin/env bash
set -euo pipefail
BS_PROJECT="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd -P)"
BS_RUNTIME="$BS_PROJECT/dists/linux"
if [[ ! -f "$BS_RUNTIME/native/native-build.json" ]]; then
    printf 'dists/linux/native/native-build.json was not found next to this script.\nRun setup.sh from the top of the Linux release folder; it does not run from the source tree.\n' >&2
    exit 1
fi
BS_PYTHON="${BS_PODCASTS_PYTHON:-python3.14}"
if ! "$BS_PYTHON" -c 'import sys; raise SystemExit(sys.version_info[:3] < (3, 14, 7))'; then
    printf 'Python 3.14.7+ is required. Set BS_PODCASTS_PYTHON to its executable.\n' >&2
    exit 1
fi
"$BS_PYTHON" - "$BS_RUNTIME/native/native-build.json" <<'PY'
import json, platform, sys
from pathlib import Path
record = json.loads(Path(sys.argv[1]).read_text())
libc, version = platform.libc_ver()
if platform.system() != 'Linux' or platform.machine() != record['architecture']:
    raise SystemExit('This native package targets Linux ' + record['architecture'] + '.')
required = tuple(map(int, record['minimum_glibc'].split('.')))
if libc != 'glibc' or tuple(map(int, version.split('.'))) < required:
    raise SystemExit('This native package requires glibc ' + record['minimum_glibc'] + '+; rebuild from source on an older distribution.')
PY
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
if ! "$BS_RUNTIME/.venv/bin/python" -B -c 'import sys; raise SystemExit(sys.version_info[:3] < (3, 14, 7))'; then
    printf 'The existing venv uses an older Python. Preserve/rename dists/linux/.venv and rerun setup with Python 3.14.7+.\n' >&2
    exit 1
fi
BS_PIP_ARGS=(--disable-pip-version-check --only-binary=:all: --require-hashes)
"$BS_RUNTIME/.venv/bin/python" -m pip install "${BS_PIP_ARGS[@]}" \
    -r "$BS_RUNTIME/requirements-linux.lock"
"$BS_RUNTIME/.venv/bin/python" -m pip check
# State lines and the stats block come from report.py, which the release folder carries beside this script.
bs_report() {
    "$BS_PYTHON" -B -I -c 'import sys; sys.path.insert(0, sys.argv[1]); from report import Reporter; Reporter().line(sys.argv[2], sys.argv[3])' "$BS_RUNTIME" "$1" "$2"
}
BS_PACKAGES="$("$BS_PYTHON" -B -I -c 'import sys; print(sum("==" in line for line in open(sys.argv[1], encoding="utf-8")))' "$BS_RUNTIME/requirements-linux.lock")"
BS_VENV_PYTHON="$("$BS_RUNTIME/.venv/bin/python" -B -c 'import sys; print(sys.version.split()[0])')"
bs_report ok "Local environment prepared. The native player is bundled; compatible ALSA/PulseAudio libraries are required."
bs_report info "Check: bash \"$BS_PROJECT/start.sh\" --check --plain"
bs_report info "Start: bash \"$BS_PROJECT/start.sh\""
bs_report info "After moving this application, preserve/rename the old .venv and rerun setup. No app was started."
"$BS_PYTHON" -B -I -c 'import sys; sys.path.insert(0, sys.argv[1]); from report import Reporter; Reporter().stats([("packages installed", sys.argv[2]), ("python", sys.argv[3])])' "$BS_RUNTIME" "$BS_PACKAGES" "$BS_VENV_PYTHON"
