#!/usr/bin/env bash
set -Eeuo pipefail

# Derived from this script's own location so a clean checkout can build
# without editing anything. BS_PODCASTS_BUILD_ROOT overrides it for a build
# that keeps its artefacts outside the repository.
HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO="$(cd "${HERE}/.." && pwd)"
ROOT="${BS_PODCASTS_BUILD_ROOT:-$(cd "${REPO}/.." && pwd)}"
VENV="${ROOT}/.venv"
DIST="${ROOT}/dist"
TMP="${ROOT}/tmp"

if [[ -x /opt/homebrew/bin/brew ]]; then
    PATH="/opt/homebrew/bin:/opt/homebrew/sbin:${PATH}"
    export PATH
fi

PYTHON="${PYTHON:-$(command -v python3.14 || true)}"

SETUP=false
# universal2 needs universal wheels for PySide6 AND a universal libmpv; the
# Homebrew dylib is single-arch, so arm64 is the default and the choice is
# explicit rather than assumed.
ARCH="${BS_PODCASTS_ARCH:-arm64}"
case "${1:-}" in
    "") ;;
    --setup) SETUP=true ;;
    --universal2) ARCH=universal2 ;;
    *)
        printf 'Usage: %s [--setup]\n' "$0" >&2
        exit 2
        ;;
esac

if [[ ! -f "${REPO}/pyproject.toml" ]]; then
    printf 'Synced repository not found at %s\n' "${REPO}" >&2
    printf 'Run /path/to/work/podcast-codex/sync.sh in WSL first.\n' >&2
    exit 1
fi

if [[ -z "${PYTHON}" ]]; then
    printf 'Python 3.14 is required but was not found.\n' >&2
    printf 'Install it, then rerun this build script.\n' >&2
    exit 1
fi

if ! "${PYTHON}" -c 'import sys; raise SystemExit(sys.version_info[:2] != (3, 14))'; then
    printf 'Expected Python 3.14, got: ' >&2
    "${PYTHON}" --version >&2
    exit 1
fi

if ! command -v brew >/dev/null 2>&1 || ! brew --prefix mpv >/dev/null 2>&1; then
    printf 'Homebrew mpv/libmpv is required for internal playback.\n' >&2
    printf 'Install it, then rerun this build script.\n' >&2
    exit 1
fi

mkdir -p "${DIST}" "${TMP}"

if [[ ! -x "${VENV}/bin/python" ]]; then
    if [[ "${SETUP}" != true ]]; then
        printf 'The local build environment does not exist: %s\n' "${VENV}" >&2
        printf 'Run %s --setup after approving dependency installation.\n' "$0" >&2
        exit 1
    fi
    "${PYTHON}" -m venv "${VENV}"
fi

if [[ "${SETUP}" == true ]]; then
    "${VENV}/bin/python" -m pip install --disable-pip-version-check --upgrade \
        pip setuptools wheel
    "${VENV}/bin/python" -m pip install --disable-pip-version-check --upgrade \
        -e "${REPO}" 'pyinstaller>=6,<7'
else
    "${VENV}/bin/python" -m pip install --disable-pip-version-check \
        --no-build-isolation --no-deps -e "${REPO}"
    "${VENV}/bin/python" -c \
        'import importlib.util; required=("PyInstaller", "PySide6", "mpv", "requests", "mutagen"); missing=[name for name in required if importlib.util.find_spec(name) is None]; raise SystemExit("Missing build packages; rerun build.sh --setup: " + ", ".join(missing) if missing else 0)'
fi

ICON_SOURCE="${REPO}/src/bs_podcasts/assets/branding/bs-podcasts-icon-master.png"
ICON_ICNS="${TMP}/BS-Podcasts.icns"
ICON_TMP="$(mktemp -d "${TMP}/BS-Podcasts-icon.XXXXXX")"
ICONSET="${ICON_TMP}/BS-Podcasts.iconset"
mkdir -p "${ICONSET}"
trap 'rm -rf -- "${ICON_TMP}"' EXIT

while read -r filename size; do
    sips -z "${size}" "${size}" "${ICON_SOURCE}" \
        --out "${ICONSET}/${filename}" >/dev/null
done <<'SIZES'
icon_16x16.png 16
icon_16x16@2x.png 32
icon_32x32.png 32
icon_32x32@2x.png 64
icon_128x128.png 128
icon_128x128@2x.png 256
icon_256x256.png 256
icon_256x256@2x.png 512
icon_512x512.png 512
icon_512x512@2x.png 1024
SIZES

iconutil -c icns "${ICONSET}" -o "${ICON_ICNS}"

"${VENV}/bin/python" -m PyInstaller \
    --noconfirm \
    --clean \
    --windowed \
    --onedir \
    --name 'BS Podcasts' \
    --osx-bundle-identifier 'com.bspodcasts.app' \
    --target-architecture "${ARCH}" \
    --icon "${ICON_ICNS}" \
    --paths "${REPO}/src" \
    --collect-data bs_podcasts \
    --copy-metadata bs-podcasts \
    --runtime-hook "${REPO}/packaging/runtime_macos.py" \
    --exclude-module PySide6.QtQml \
    --exclude-module PySide6.QtQuick \
    --exclude-module PySide6.QtPdf \
    --distpath "${DIST}" \
    --workpath "${TMP}/pyinstaller-work" \
    --specpath "${TMP}" \
    "${REPO}/packaging/windows_launcher.py"

APP="${DIST}/BS Podcasts.app"
if [[ ! -d "${APP}" ]]; then
    printf 'Build finished without producing %s\n' "${APP}" >&2
    exit 1
fi

VERSION="$("${VENV}/bin/python" -c 'import sys, tomllib; print(tomllib.load(open(sys.argv[1], "rb"))["project"]["version"])' "${REPO}/pyproject.toml")"
INFO_PLIST="${APP}/Contents/Info.plist"
/usr/libexec/PlistBuddy -c "Set :CFBundleShortVersionString ${VERSION}" "${INFO_PLIST}"
if ! /usr/libexec/PlistBuddy -c "Set :CFBundleVersion ${VERSION}" "${INFO_PLIST}" 2>/dev/null; then
    /usr/libexec/PlistBuddy -c "Add :CFBundleVersion string ${VERSION}" "${INFO_PLIST}"
fi
RESOURCES="${APP}/Contents/Resources"
mkdir -p "${RESOURCES}"
cp "${REPO}/packaging/THIRD-PARTY-NOTICES.txt" "${RESOURCES}/"
cp -R "${REPO}/packaging/licenses" "${RESOURCES}/"

# Signing. An ad-hoc signature (-) fails Gatekeeper on any machine other than
# the one that built it, so a release build must pass a Developer ID and the
# script refuses to pretend otherwise.
IDENTITY="${BS_PODCASTS_SIGN_IDENTITY:-}"
if [[ -n "${IDENTITY}" ]]; then
    codesign --force --deep --options runtime --timestamp \
        --entitlements "${REPO}/packaging/macos-entitlements.plist" \
        --sign "${IDENTITY}" "${APP}"
    codesign --verify --strict --verbose=2 "${APP}"
else
    codesign --force --deep --sign - "${APP}"
    printf '\n*** AD-HOC SIGNED: development only. ***\n' >&2
    printf 'This build will be refused by Gatekeeper on another Mac.\n' >&2
    printf 'For a release set BS_PODCASTS_SIGN_IDENTITY to a Developer ID\n' >&2
    printf 'Application identity, then notarize:\n' >&2
    printf '  xcrun notarytool submit <zip> --keychain-profile <profile> --wait\n' >&2
    printf '  xcrun stapler staple "%s"\n' "${APP}" >&2
fi

ZIP="${DIST}/BS-Podcasts-macOS-${ARCH}.zip"
ditto -c -k --sequesterRsrc --keepParent "${APP}" "${ZIP}"

printf '\nBuild complete:\n  %s\n  %s\n' "${APP}" "${ZIP}"
shasum -a 256 "${ZIP}"
