#!/usr/bin/env bash
set -euo pipefail
BS_PROJECT="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/../.." && pwd -P)"
BS_PYTHON="$BS_PROJECT/dists/linux/.venv/bin/python"
BS_CONFIG="$BS_PROJECT/.config/build.json"
[[ -f "$BS_CONFIG" ]] || { printf 'Configure .config/build.json before syncing.\n' >&2; exit 1; }
mapfile -t BS_REMOTE < <("$BS_PYTHON" -B -c '
import json, re, shlex, sys
from pathlib import PurePosixPath
c=json.load(open(sys.argv[1])); host=c["mac_host"]; root=c["mac_root"]
assert re.fullmatch(r"[A-Za-z0-9_.@-]+",host) and not host.startswith("-")
assert PurePosixPath(root).is_absolute() and root!="/"
print(host); print(root); print(shlex.quote(root+"/repo"))
' "$BS_CONFIG")
[[ ${#BS_REMOTE[@]} -eq 3 ]] || { printf 'Invalid private sync configuration.\n' >&2; exit 1; }
BS_SNAPSHOT="$(mktemp -d "$BS_PROJECT/tmp/mac-sync.XXXXXX")"
"$BS_PYTHON" -B "$BS_PROJECT/repo/packaging/source_manifest.py" stage --destination "$BS_SNAPSHOT/source"
BS_LIST="$(mktemp "$BS_PROJECT/tmp/sync-inputs.XXXXXX")"
trap 'rm -f -- "$BS_LIST"' EXIT
"$BS_PYTHON" -B "$BS_SNAPSHOT/source/packaging/source_manifest.py" list > "$BS_LIST"
printf '.build-origin.json\0' >> "$BS_LIST"
printf 'Syncing approved source to %s:%s/repo\n' "${BS_REMOTE[0]}" "${BS_REMOTE[1]}"
ssh "${BS_REMOTE[0]}" "mkdir -p -- ${BS_REMOTE[2]}"
# Both GNU tar and macOS bsdtar support this manifest-only archive stream.
tar -C "$BS_SNAPSHOT/source" --null -T "$BS_LIST" -cf - | \
    ssh "${BS_REMOTE[0]}" "tar -C ${BS_REMOTE[2]} -xf -"
printf 'Source synced. No remote build or service was started.\n'
