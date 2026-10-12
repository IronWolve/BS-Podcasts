#!/usr/bin/env bash
# Install the launcher entry and icons for the current user (no root needed).
set -euo pipefail
HERE="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd -P)"
WORKSPACE="$(cd "$HERE/../.." && pwd -P)"
BRANDING="$WORKSPACE/dists/linux/app/bs_podcasts/assets/branding"
DATA="${XDG_DATA_HOME:-$HOME/.local/share}"
APPS="$DATA/applications"
ICONS="$DATA/icons/hicolor"
LAUNCHER_DIR="$DATA/bs-podcasts"
# One state line through the shared Reporter (colour on a terminal, the same labels as the other tools).
# Without the project Python the line is printed plainly, so --status still works on a partial checkout.
report() {
  local state="$1" text="$2" python="$WORKSPACE/dists/linux/.venv/bin/python"
  if [[ -x "$python" ]]; then
    "$python" -B -I -c 'import sys; sys.path.insert(0, sys.argv[1]); from report import Reporter; Reporter().line(sys.argv[2], sys.argv[3])' "$HERE" "$state" "$text"
  else
    case "$state" in ok) printf 'OK   %s\n' "$text" ;; info) printf '     %s\n' "$text" ;; *) printf 'FAIL %s\n' "$text" ;; esac
  fi
}
# Label and value pairs under a Stats heading, through the same Reporter; nothing is printed without the project Python.
report_stats() {
  local python="$WORKSPACE/dists/linux/.venv/bin/python"
  [[ -x "$python" ]] || return 0
  "$python" -B -I -c 'import sys; sys.path.insert(0, sys.argv[1]); from report import Reporter; a = sys.argv[2:]; Reporter().stats(list(zip(a[::2], a[1::2])))' "$HERE" "$@"
}
DRY_RUN=0
[[ "${1:-}" == "--dry-run" ]] && DRY_RUN=1
if [[ "${1:-}" == "--status" ]]; then
  # Read-only: report whether the installed entry still reaches a project.
  failed=0
  entry="$APPS/bs-podcasts.desktop"
  target=""
  if [[ -f "$entry" ]]; then
    while IFS= read -r line; do
      [[ "$line" == Exec=* ]] && { target="${line#Exec=}"; break; }
    done < "$entry"
    target="${target#\"}"; target="${target%\"}"
    report ok "Desktop entry: $entry"
  else
    report fail "Desktop entry: not installed"; failed=1
  fi
  if [[ -n "$target" && -x "$target" ]]; then report ok "Exec target: $target"
  else report fail "Exec target: ${target:-none} does not exist"; failed=1; fi
  recorded=""
  { IFS= read -r recorded < "$LAUNCHER_DIR/project-root"; } 2>/dev/null || true
  if [[ -n "$recorded" && -x "$recorded/dists/linux/start.sh" ]]; then report ok "Project: $recorded"
  else report fail "Project: ${recorded:-none recorded} has no launcher; run packaging/install-desktop.sh from the project folder again"; failed=1; fi
  report_stats checks 3 failed "$failed"
  exit "$failed"
fi
if [[ "${1:-}" == "--uninstall" ]]; then
  # Removes exactly the files this installer writes: the five icon copies, the entry, the launcher and its project
  # record. Nothing else under the data home is touched, and the launcher folder goes only when it is then empty.
  removed=0
  for size in 32 64 128 256 512; do
    target="$ICONS/${size}x${size}/apps/bs-podcasts.png"
    if [[ -e "$target" || -L "$target" ]]; then rm -- "$target"; report ok "Removed $target"; removed=$((removed + 1)); fi
  done
  for target in "$APPS/bs-podcasts.desktop" "$LAUNCHER_DIR/launch.sh" "$LAUNCHER_DIR/project-root"; do
    if [[ -e "$target" || -L "$target" ]]; then rm -- "$target"; report ok "Removed $target"; removed=$((removed + 1)); fi
  done
  rmdir -- "$LAUNCHER_DIR" 2>/dev/null || true
  report_stats "files removed" "$removed" "launcher folder" "$LAUNCHER_DIR"
  exit 0
fi
test -f "$WORKSPACE/dists/linux/runner.py" || { echo 'Build the Linux deployment first.' >&2; exit 1; }
# Validate all inputs before writing to the user's desktop directories. The
# deployment receipt, not current checkout contents, identifies the built icons.
"$WORKSPACE/dists/linux/.venv/bin/python" -B -c '
import hashlib, json, pathlib, sys
root = pathlib.Path(sys.argv[1])
try:
    record = json.loads((root / "deployment.json").read_text())
    for size in (32, 64, 128, 256, 512):
        name = f"app/bs_podcasts/assets/branding/bs-podcasts-shell-icon-{size}.png"
        path = root / name
        if not path.is_file() or hashlib.sha256(path.read_bytes()).hexdigest() != record["files"][name]:
            raise ValueError("missing or changed shell icon")
    if not (root / "start.sh").is_file():
        raise ValueError("missing deployed launcher")
except (OSError, ValueError, KeyError):
    raise SystemExit("Linux desktop inputs are incomplete; rebuild the Linux deployment first. No desktop files changed.")
' "$WORKSPACE/dists/linux"
# The recorded project folder is kept one per line.
[[ "$WORKSPACE" != *$'\n'* ]] || { echo 'The project path contains a line break. No desktop files changed.' >&2; exit 1; }
if (( DRY_RUN )); then
  # Everything above only read the inputs; this stops before the first write.
  report info "Dry run: nothing written. The install would write:"
  for size in 32 64 128 256 512; do report info "  $ICONS/${size}x${size}/apps/bs-podcasts.png"; done
  report info "  $LAUNCHER_DIR/launch.sh and $LAUNCHER_DIR/project-root"
  report info "  $APPS/bs-podcasts.desktop"
  exit 0
fi
mkdir -p "$APPS"
TEMPORARIES=()
cleanup() { for file in "${TEMPORARIES[@]}"; do [[ ! -e "$file" ]] || rm -- "$file"; done; }
trap cleanup EXIT
for size in 32 64 128 256 512; do
  mkdir -p "$ICONS/${size}x${size}/apps"
  target="$ICONS/${size}x${size}/apps/bs-podcasts.png"
  temporary="$(mktemp "$target.XXXXXX")"
  TEMPORARIES+=("$temporary")
  cp "$BRANDING/bs-podcasts-shell-icon-$size.png" "$temporary"
  chmod 644 "$temporary"
  mv -- "$temporary" "$target"
done
# The entry points at a small per-user launcher, not at this project folder, so
# the entry never holds the folder's path. The launcher reads the recorded
# folder and names it when it is gone, instead of failing without a message.
mkdir -p "$LAUNCHER_DIR"
temporary="$(mktemp "$LAUNCHER_DIR/launch.sh.XXXXXX")"
TEMPORARIES+=("$temporary")
cat > "$temporary" <<'LAUNCHER'
#!/usr/bin/env bash
# Opens BS Podcasts from the project folder recorded by install-desktop.sh.
HERE="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd -P)"
root=""
{ IFS= read -r root < "$HERE/project-root"; } 2>/dev/null || true
if [[ -z "$root" || ! -x "$root/dists/linux/start.sh" ]]; then
    message="BS Podcasts was not found at '${root:-nowhere}'. If the project folder was moved or renamed, run packaging/install-desktop.sh from it again."
    printf '%s\n' "$message" >&2
    command -v notify-send >/dev/null 2>&1 && notify-send 'BS Podcasts' "$message" >/dev/null 2>&1 || true
    exit 1
fi
exec "$root/dists/linux/start.sh" "$@"
LAUNCHER
chmod 755 "$temporary"
mv -- "$temporary" "$LAUNCHER_DIR/launch.sh"
temporary="$(mktemp "$LAUNCHER_DIR/project-root.XXXXXX")"
TEMPORARIES+=("$temporary")
printf '%s\n' "$WORKSPACE" > "$temporary"
chmod 644 "$temporary"
mv -- "$temporary" "$LAUNCHER_DIR/project-root"
# Render Exec with desktop-entry quoting (including literal percent signs).
temporary="$(mktemp "$APPS/bs-podcasts.desktop.XXXXXX")"
TEMPORARIES+=("$temporary")
"$WORKSPACE/dists/linux/.venv/bin/python" -B "$HERE/render-desktop.py" \
    "$HERE/bs-podcasts.desktop" "$LAUNCHER_DIR/launch.sh" "$temporary"
chmod 644 "$temporary"
mv -- "$temporary" "$APPS/bs-podcasts.desktop"
command -v update-desktop-database >/dev/null 2>&1 && update-desktop-database "$APPS" || true
command -v gtk-update-icon-cache >/dev/null 2>&1 && gtk-update-icon-cache -q "$ICONS" || true
report ok "Installed launcher to $APPS/bs-podcasts.desktop (opens $WORKSPACE; run this again if that folder moves or is renamed)"
report_stats icons "5 sizes, 32 to 512 px" "desktop entry" "$APPS/bs-podcasts.desktop" launcher "$LAUNCHER_DIR/launch.sh"
