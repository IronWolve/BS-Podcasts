#!/usr/bin/env bash
set -euo pipefail
BS_PROJECT="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/../.." && pwd -P)"
BS_PYTHON="$BS_PROJECT/dists/linux/.venv/bin/python"
if [[ "${1:-}" == --windows ]]; then
    shift
    mkdir -p "$BS_PROJECT/tmp"
    BS_WINDOWS_STAGE="$(mktemp -d "$BS_PROJECT/tmp/windows-source.XXXXXX")"
    "$BS_PYTHON" -B "$BS_PROJECT/repo/packaging/source_manifest.py" stage --destination "$BS_WINDOWS_STAGE/source"
    BS_POWERSHELL="$(command -v powershell.exe || true)"
    BS_POWERSHELL="${BS_POWERSHELL:-/mnt/c/Windows/System32/WindowsPowerShell/v1.0/powershell.exe}"
    exec "$BS_POWERSHELL" -NoProfile -ExecutionPolicy Bypass -File \
        "$(wslpath -w "$BS_PROJECT/repo/packaging/build-windows.ps1")" \
        -SourceStage "$(wslpath -w "$BS_WINDOWS_STAGE/source")" "$@"
fi
if [[ ! -x "$BS_PYTHON" ]]; then
    printf 'Missing project Python environment: %s\nDependency installation must be approved separately.\n' "$BS_PYTHON" >&2
    exit 1
fi
mkdir -p "$BS_PROJECT/tmp" "$BS_PROJECT/.cache" "$BS_PROJECT/logs"
unset PYTHONPATH
export TMPDIR="$BS_PROJECT/tmp" XDG_CACHE_HOME="$BS_PROJECT/.cache" PYTHONDONTWRITEBYTECODE=1
exec "$BS_PYTHON" -B "$BS_PROJECT/repo/packaging/deploy.py" "$@"
