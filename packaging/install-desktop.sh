#!/bin/sh
# Install the launcher entry and icons for the current user (no root needed).
set -e
HERE="$(cd "$(dirname "$0")" && pwd)"
BRANDING="$HERE/../src/bs_podcasts/assets/branding"
APPS="${XDG_DATA_HOME:-$HOME/.local/share}/applications"
ICONS="${XDG_DATA_HOME:-$HOME/.local/share}/icons/hicolor"
mkdir -p "$APPS"
for size in 32 64 128 256 512; do
  mkdir -p "$ICONS/${size}x${size}/apps"
  cp "$BRANDING/bs-podcasts-icon-$size.png" "$ICONS/${size}x${size}/apps/bs-podcasts.png"
done
# The venv entry point is not on the desktop session's PATH; point Exec at
# the workspace launcher so the menu entry actually starts the app.
LAUNCHER="$(cd "$HERE/../../.." && pwd)/start.sh"
sed "s|^Exec=.*|Exec=$LAUNCHER %U|" "$HERE/bs-podcasts.desktop" > "$APPS/bs-podcasts.desktop"
command -v update-desktop-database >/dev/null 2>&1 && update-desktop-database "$APPS" || true
command -v gtk-update-icon-cache >/dev/null 2>&1 && gtk-update-icon-cache -q "$ICONS" || true
echo "Installed launcher to $APPS/bs-podcasts.desktop"
