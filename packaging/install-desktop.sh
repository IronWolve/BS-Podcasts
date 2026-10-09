#!/usr/bin/env bash
# Install the launcher entry and icons for the current user (no root needed).
set -euo pipefail
HERE="$(cd "$(dirname "$0")" && pwd)"
WORKSPACE="$(cd "$HERE/../.." && pwd -P)"
BRANDING="$WORKSPACE/dists/linux/app/bs_podcasts/assets/branding"
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
APPS="${XDG_DATA_HOME:-$HOME/.local/share}/applications"
ICONS="${XDG_DATA_HOME:-$HOME/.local/share}/icons/hicolor"
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
# Point at deployed code; the checkout is not a runtime dependency.
# Render Exec with desktop-entry quoting (including literal percent signs).
temporary="$(mktemp "$APPS/bs-podcasts.desktop.XXXXXX")"
TEMPORARIES+=("$temporary")
"$WORKSPACE/dists/linux/.venv/bin/python" -B "$HERE/render-desktop.py" \
    "$HERE/bs-podcasts.desktop" "$WORKSPACE/dists/linux/start.sh" "$temporary"
chmod 644 "$temporary"
mv -- "$temporary" "$APPS/bs-podcasts.desktop"
command -v update-desktop-database >/dev/null 2>&1 && update-desktop-database "$APPS" || true
command -v gtk-update-icon-cache >/dev/null 2>&1 && gtk-update-icon-cache -q "$ICONS" || true
echo "Installed launcher to $APPS/bs-podcasts.desktop"
