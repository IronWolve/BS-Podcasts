#!/bin/sh
# Install the launcher entry and icons for the current user (no root needed).
set -e
HERE="$(cd "$(dirname "$0")" && pwd)"
WORKSPACE="$(cd "$HERE/../.." && pwd -P)"
BRANDING="$WORKSPACE/dists/linux/app/bs_podcasts/assets/branding"
test -f "$WORKSPACE/dists/linux/runner.py" || { echo 'Build the Linux deployment first.' >&2; exit 1; }
APPS="${XDG_DATA_HOME:-$HOME/.local/share}/applications"
ICONS="${XDG_DATA_HOME:-$HOME/.local/share}/icons/hicolor"
mkdir -p "$APPS"
for size in 32 64 128 256 512; do
  mkdir -p "$ICONS/${size}x${size}/apps"
  cp "$BRANDING/bs-podcasts-icon-$size.png" "$ICONS/${size}x${size}/apps/bs-podcasts.png"
done
# Point at deployed code; the checkout is not a runtime dependency.
# Render Exec with desktop-entry quoting (including literal percent signs).
"$WORKSPACE/dists/linux/.venv/bin/python" -B "$HERE/render-desktop.py" \
    "$HERE/bs-podcasts.desktop" "$WORKSPACE/dists/linux/start.sh" "$APPS/bs-podcasts.desktop"
command -v update-desktop-database >/dev/null 2>&1 && update-desktop-database "$APPS" || true
command -v gtk-update-icon-cache >/dev/null 2>&1 && gtk-update-icon-cache -q "$ICONS" || true
echo "Installed launcher to $APPS/bs-podcasts.desktop"
