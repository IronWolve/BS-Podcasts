#!/usr/bin/env bash
set -euo pipefail
BS_PROJECT="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/../.." && pwd -P)"
BS_PYTHON="$BS_PROJECT/dists/linux/.venv/bin/python"
BS_CONFIG="$BS_PROJECT/.config/build.json"
BS_STARTED="$(date +%s.%N)"
BS_PLAIN=0
BS_PLAIN_ARGS=()
for BS_ARGUMENT in "$@"; do
    if [[ "$BS_ARGUMENT" == --plain ]]; then
        BS_PLAIN=1
        BS_PLAIN_ARGS=(--plain)
    fi
done
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
cleanup_mac_sync() {
    case "$BS_SNAPSHOT" in
        "$BS_PROJECT"/tmp/mac-sync.*) rm -rf -- "$BS_SNAPSHOT" ;;
    esac
    case "${BS_LIST:-}" in
        "$BS_PROJECT"/tmp/sync-inputs.*) rm -f -- "$BS_LIST" ;;
    esac
}
trap cleanup_mac_sync EXIT
"$BS_PYTHON" -B "$BS_PROJECT/repo/packaging/source_manifest.py" stage --destination "$BS_SNAPSHOT/source" "${BS_PLAIN_ARGS[@]}"
BS_LIST="$(mktemp "$BS_PROJECT/tmp/sync-inputs.XXXXXX")"
"$BS_PYTHON" -B "$BS_SNAPSHOT/source/packaging/source_manifest.py" list > "$BS_LIST"
printf '.build-origin.json\0' >> "$BS_LIST"
# State lines and the stats block use the staged copy of the shared reporter.
bs_report() {
    "$BS_PYTHON" -B -c '
import sys, time
from pathlib import Path
packaging, plain, started, state, text = sys.argv[1:6]
sys.path.insert(0, packaging)
from report import Reporter, size_text
console = Reporter(plain=plain == "1")
console.started -= time.time() - float(started)
console.line(state, text)
if len(sys.argv) > 6:
    from source_manifest import source_digest
    snapshot, listing = Path(sys.argv[6]), Path(sys.argv[7])
    names = [name for name in listing.read_bytes().decode("utf-8").split("\0") if name]
    total = sum((snapshot / name).stat().st_size for name in names)
    console.stats([("files", str(len(names))), ("size", size_text(total)), ("source SHA-256", source_digest(snapshot))])
' "$BS_SNAPSHOT/source/packaging" "$BS_PLAIN" "$BS_STARTED" "$@"
}
bs_report info "Syncing approved source to ${BS_REMOTE[0]}:${BS_REMOTE[1]}/repo"
ssh "${BS_REMOTE[0]}" "mkdir -p -- ${BS_REMOTE[2]}"
# Both GNU tar and macOS bsdtar support this manifest-only archive stream.
tar -C "$BS_SNAPSHOT/source" --null -T "$BS_LIST" -cf - | \
    ssh "${BS_REMOTE[0]}" "tar -C ${BS_REMOTE[2]} -xf -"
# Files an earlier sync left in the Mac copy that the approved manifest no longer lists are reported, never
# removed: the list is named and the owner decides whether to delete them on the Mac.
BS_LEFTOVERS="$(ssh -n "${BS_REMOTE[0]}" "cd -- ${BS_REMOTE[2]} && find . -type f -not -path './.git/*' -print0" | \
    "$BS_PYTHON" -B "$BS_SNAPSHOT/source/packaging/source_manifest.py" leftovers --list "$BS_LIST" | tr '\0' '\n')"
BS_LEFT_COUNT=0
if [[ -n "$BS_LEFTOVERS" ]]; then BS_LEFT_COUNT="$(printf '%s\n' "$BS_LEFTOVERS" | wc -l | tr -d ' ')"; fi
if (( BS_LEFT_COUNT )); then
    BS_LEFT_SHOWN="$(printf '%s\n' "$BS_LEFTOVERS" | head -n 20 | paste -sd ' ' -)"
    if (( BS_LEFT_COUNT > 20 )); then BS_LEFT_SHOWN="$BS_LEFT_SHOWN ..."; fi
    bs_report warn "$BS_LEFT_COUNT file(s) on ${BS_REMOTE[0]} are not in the approved manifest and were not removed: $BS_LEFT_SHOWN"
else
    bs_report ok "No files outside the approved manifest on ${BS_REMOTE[0]}."
fi
bs_report ok "Source synced to ${BS_REMOTE[0]}:${BS_REMOTE[1]}/repo. No remote build or service was started." \
    "$BS_SNAPSHOT/source" "$BS_LIST"
