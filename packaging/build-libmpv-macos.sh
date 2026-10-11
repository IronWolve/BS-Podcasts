#!/usr/bin/env bash
# Pinned LGPL-only arm64 player; no Homebrew libraries or global installs.
set -euo pipefail
unset PYTHONPATH
BS_PROJECT="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/../.." && pwd -P)"
BS_VENV="${BS_PODCASTS_MAC_VENV:-$BS_PROJECT/.cache/macos-build/.venv}"
BS_PYTHON="$BS_VENV/bin/python"
[[ "$(uname -s)" == Darwin && "$(uname -m)" == arm64 ]] || { printf 'Build on an arm64 Mac.\n' >&2; exit 1; }
BS_JOBS="${BS_NATIVE_JOBS:-2}"
[[ "$BS_JOBS" =~ ^[1-4]$ ]] || exit 2
BS_WORK="$(mktemp -d "$BS_PROJECT/tmp/macos-native.XXXXXX")"
# Each run's build log is kept in logs/, and every run ends with a coloured status line and a stats block.
BS_STARTED="$(date +%s)"
# Colour is decided here, once: a status line is printed inside $(...), where stdout is no longer the terminal.
BS_COLOUR=0
if [[ -t 1 && -z "${NO_COLOR:-}" && "${TERM:-}" != dumb ]]; then BS_COLOUR=1; fi
bs_paint() {
    if (( BS_COLOUR )); then printf '\033[%sm%s\033[0m' "$1" "$2"; else printf '%s' "$2"; fi
}
bs_stats() {
    local artifact="$1" size digest
    read -r size digest < <("$BS_PYTHON" -B -I -c '
import hashlib, os, sys
path = sys.argv[1]
print(os.path.getsize(path), hashlib.sha256(open(path, "rb").read()).hexdigest())
' "$artifact")
    printf 'Stats\n  elapsed   %s s\n  artifact  %s\n  size      %s bytes\n  sha256    %s\n' \
        "$(( $(date +%s) - BS_STARTED ))" "$artifact" "$size" "$digest"
}
bs_finish() {
    local status=$?
    if [[ -f "$BS_WORK/build.log" ]]; then
        mkdir -p "$BS_PROJECT/logs"
        cp -- "$BS_WORK/build.log" "$BS_PROJECT/logs/native-macos-$(date -u +%Y%m%dT%H%M%SZ)-status$status.log"
    fi
    if (( status == 0 )); then
        printf "%s Native macOS player ready: $BS_CACHE\n" "$(bs_paint 32 OK)"
        bs_stats "$BS_CACHE/libmpv.2.dylib"
    else
        printf "%s Native macOS build stopped with status $status; work folder kept: %s\n" "$(bs_paint 31 FAIL)" "$BS_WORK"
    fi
    exit "$status"
}
trap bs_finish EXIT
BS_PREFIX="$BS_WORK/prefix"
BS_SRC="$BS_WORK/src"
mkdir -p "$BS_PREFIX" "$BS_WORK/tmp"
export TMPDIR="$BS_WORK/tmp" PYTHONDONTWRITEBYTECODE=1
export PATH="$BS_VENV/bin:$BS_PROJECT/.cache/native-tools/bin:/usr/bin:/bin:/usr/sbin:/sbin"
export CC=clang CXX=clang++ MACOSX_DEPLOYMENT_TARGET=14.0
export PKG_CONFIG_LIBDIR="$BS_PREFIX/lib/pkgconfig" PKG_CONFIG_PATH=""
export CFLAGS="-O2 -fPIC -mmacosx-version-min=14.0 -ffile-prefix-map=$BS_WORK=/bs-native -fdebug-prefix-map=$BS_WORK=/bs-native"
export CXXFLAGS="$CFLAGS"
command -v meson ninja pkg-config >/dev/null
printf 'Native work/log directory: %s\n' "$BS_WORK"
"$BS_PYTHON" -B -I "$BS_PROJECT/repo/packaging/native_sources.py" --source "$BS_SRC" --cache "$BS_PROJECT/.cache/native-sources" --macos
# The fetched directory for a pinned name, read from native-sources.json, so no
# version is written into this recipe and a pin change cannot leave a stale path.
pinned_dir() {
    "$BS_PYTHON" -B -I -c '
import json, sys
for entry in json.load(open(sys.argv[1])):
    if entry["name"] == sys.argv[2]:
        print(entry["name"] + "-" + entry["version"])
        break
else:
    sys.exit("not pinned: " + sys.argv[2])
' "$BS_PROJECT/repo/packaging/native-sources.json" "$1"
}
for name in freetype fribidi harfbuzz libass ffmpeg libplacebo mpv; do
    fetched="$(pinned_dir "$name")"
    [[ -d "$BS_SRC/$fetched" ]] || { printf 'Fetched source is missing: %s\n' "$fetched" >&2; exit 1; }
done
BS_FFMPEG_DIR="$(pinned_dir ffmpeg)"
BS_MPV_DIR="$(pinned_dir mpv)"
patch -d "$BS_SRC/$BS_MPV_DIR" -p1 -F 0 < "$BS_PROJECT/repo/packaging/mpv-coreaudio-only.patch"
build_static() {
    local name="$1"; shift
    local source
    source="$(pinned_dir "$name")"
    meson setup "$BS_WORK/build-$source" "$BS_SRC/$source" --prefix "$BS_PREFIX" --libdir lib \
        --buildtype release --default-library static --prefer-static --wrap-mode=nodownload -Db_staticpic=true "$@" >> "$BS_WORK/build.log" 2>&1
    ninja -C "$BS_WORK/build-$source" -j "$BS_JOBS" install >> "$BS_WORK/build.log" 2>&1
    printf 'Built %s\n' "$source"
}
build_static freetype -Dharfbuzz=disabled -Dbrotli=disabled -Dpng=disabled -Dbzip2=disabled -Dzlib=internal -Dtests=disabled
build_static fribidi -Ddocs=false -Dtests=false
build_static harfbuzz -Dfreetype=enabled -Dglib=disabled -Dgobject=disabled -Dcairo=disabled -Dicu=disabled -Dtests=disabled -Ddocs=disabled
build_static libass -Dtest=disabled -Dfontconfig=disabled
(
    cd "$BS_SRC/$BS_FFMPEG_DIR"
    ./configure --prefix="$BS_PREFIX" --arch=arm64 --enable-pic --disable-shared --enable-static \
        --disable-autodetect --enable-zlib --disable-programs --disable-doc --disable-debug \
        --disable-encoders --disable-muxers --disable-outdevs --disable-indevs \
        --extra-cflags="$CFLAGS" --extra-ldflags=-mmacosx-version-min=14.0
    "$BS_PYTHON" -B -I "$BS_PROJECT/repo/packaging/native_sources.py" --sanitize config.h --root "$BS_WORK"
    make -j "$BS_JOBS"
    make install
) >> "$BS_WORK/build.log" 2>&1
printf 'Built FFmpeg %s\n' "$BS_FFMPEG_DIR"
# The same licence and configure checks the Windows and Linux recipes make: the
# player is built only from an FFmpeg configured as LGPL, with autodetection off.
BS_FFMPEG_LOG="$BS_SRC/$BS_FFMPEG_DIR/ffbuild/config.log"
BS_LICENSE="$(grep -E "^license=" "$BS_FFMPEG_LOG" | head -n 1 || true)"
BS_CONFIGURE="$(head -n 1 "$BS_FFMPEG_LOG")"
if [[ "$BS_LICENSE" != "license='LGPL version 2.1 or later'" ]]; then
    printf 'FFmpeg config.log does not record the expected LGPL configuration.\n' >&2
    exit 1
fi
case "$BS_CONFIGURE" in
    *--enable-gpl*|*--enable-nonfree*)
        printf 'FFmpeg configure enables a non-LGPL license option.\n' >&2
        exit 1 ;;
    *--disable-autodetect*) ;;
    *)
        printf 'FFmpeg config.log does not record --disable-autodetect.\n' >&2
        exit 1 ;;
esac
build_static libplacebo -Dvulkan=disabled -Dopengl=disabled -Dd3d11=disabled -Ddemos=false \
    -Dtests=false -Dshaderc=disabled -Dglslang=disabled -Dlcms=disabled
meson setup "$BS_WORK/build-mpv" "$BS_SRC/$BS_MPV_DIR" --prefix "$BS_PREFIX" --libdir lib \
    --buildtype release --prefer-static --default-library shared --wrap-mode=nodownload --auto-features=disabled \
    --sysconfdir=/bs-native/etc --datadir=/bs-native/share \
    -Dgpl=false -Dlibmpv=true -Dcplayer=false -Dtests=false -Dgl=disabled -Dcoreaudio=enabled \
    "-Dc_link_args=['-lc++','-mmacosx-version-min=14.0']" >> "$BS_WORK/build.log" 2>&1
"$BS_PYTHON" -B -I "$BS_PROJECT/repo/packaging/native_sources.py" --sanitize "$BS_WORK/build-mpv/config.h" --root "$BS_WORK"
ninja -C "$BS_WORK/build-mpv" -j "$BS_JOBS" >> "$BS_WORK/build.log" 2>&1
mkdir "$BS_WORK/out"
cp -L "$BS_WORK/build-mpv/libmpv.2.dylib" "$BS_WORK/out/libmpv.2.dylib"
install_name_tool -id @rpath/libmpv.2.dylib "$BS_WORK/out/libmpv.2.dylib"
strip -x "$BS_WORK/out/libmpv.2.dylib"
codesign --force --sign - "$BS_WORK/out/libmpv.2.dylib"
"$BS_PYTHON" -B -I -c 'import ctypes,sys; ctypes.CDLL(sys.argv[1])' "$BS_WORK/out/libmpv.2.dylib"
# Allow list: system libraries and frameworks, plus the library's own install
# name. Any other path (Homebrew, a user folder, a local prefix) fails the build.
BS_LINKS="$(otool -L "$BS_WORK/out/libmpv.2.dylib")"
BS_UNEXPECTED_LINKS="$(tail -n +2 <<< "$BS_LINKS" | grep -Ev '^[[:space:]]+(/usr/lib/|/System/Library/|@rpath/libmpv[.]2[.]dylib )' || true)"
if [[ -n "$BS_UNEXPECTED_LINKS" ]]; then
    printf 'Unexpected non-system linkage in native player.\n' >&2; exit 1
fi
"$BS_PYTHON" -B -I "$BS_PROJECT/repo/packaging/native_sources.py" --record "$BS_WORK/out/libmpv.2.dylib"
BS_CACHE="$BS_PROJECT/.cache/macos-build/libmpv-lgpl"
if [[ -e "$BS_CACHE" ]]; then
    mkdir -p "$BS_PROJECT/.cache/macos-build"
    BS_PREVIOUS="$(mktemp -d "$BS_PROJECT/.cache/macos-build/previous-libmpv-lgpl-$(date -u +%Y%m%d)-XXXXXX")"
    mv -- "$BS_CACHE" "$BS_PREVIOUS/libmpv-lgpl"
fi
mv -- "$BS_WORK/out" "$BS_CACHE"
