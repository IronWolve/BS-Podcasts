#!/usr/bin/env bash
# Pinned LGPL-only arm64 player; no Homebrew libraries or global installs.
set -euo pipefail
BS_PROJECT="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/../.." && pwd -P)"
BS_VENV="${BS_PODCASTS_MAC_VENV:-$BS_PROJECT/.cache/macos-build/.venv}"
BS_PYTHON="$BS_VENV/bin/python"
[[ "$(uname -s)" == Darwin && "$(uname -m)" == arm64 ]] || { printf 'Build on an arm64 Mac.\n' >&2; exit 1; }
BS_JOBS="${BS_NATIVE_JOBS:-2}"
[[ "$BS_JOBS" =~ ^[1-4]$ ]] || exit 2
BS_WORK="$(mktemp -d "$BS_PROJECT/tmp/macos-native.XXXXXX")"
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
"$BS_PYTHON" -B "$BS_PROJECT/repo/packaging/native_sources.py" --source "$BS_SRC" --cache "$BS_PROJECT/.cache/native-sources" --macos
build_static() {
    local source="$1"; shift
    meson setup "$BS_WORK/build-$source" "$BS_SRC/$source" --prefix "$BS_PREFIX" --libdir lib \
        --buildtype release --default-library static --prefer-static --wrap-mode=nodownload -Db_staticpic=true "$@" >> "$BS_WORK/build.log" 2>&1
    ninja -C "$BS_WORK/build-$source" -j "$BS_JOBS" install >> "$BS_WORK/build.log" 2>&1
    printf 'Built %s\n' "$source"
}
build_static freetype-2.14.3 -Dharfbuzz=disabled -Dbrotli=disabled -Dpng=disabled -Dbzip2=disabled -Dzlib=internal -Dtests=disabled
build_static fribidi-1.0.17 -Ddocs=false -Dtests=false
build_static harfbuzz-14.5.0 -Dfreetype=enabled -Dglib=disabled -Dgobject=disabled -Dcairo=disabled -Dicu=disabled -Dtests=disabled -Ddocs=disabled
build_static libass-0.17.5 -Dtest=disabled -Dfontconfig=disabled
(
    cd "$BS_SRC/ffmpeg-9.0.2"
    ./configure --prefix="$BS_PREFIX" --arch=arm64 --enable-pic --disable-shared --enable-static \
        --disable-autodetect --enable-zlib --disable-programs --disable-doc --disable-debug \
        --disable-encoders --disable-muxers --disable-outdevs --disable-indevs \
        --extra-cflags="$CFLAGS" --extra-ldflags=-mmacosx-version-min=14.0
    "$BS_PYTHON" -B "$BS_PROJECT/repo/packaging/native_sources.py" --sanitize config.h --root "$BS_WORK"
    make -j "$BS_JOBS"
    make install
) >> "$BS_WORK/build.log" 2>&1
printf 'Built FFmpeg 9.0.2\n'
build_static libplacebo-7.360.1 -Dvulkan=disabled -Dopengl=disabled -Dd3d11=disabled -Ddemos=false \
    -Dtests=false -Dshaderc=disabled -Dglslang=disabled -Dlcms=disabled
meson setup "$BS_WORK/build-mpv" "$BS_SRC/mpv-0.41.0" --prefix "$BS_PREFIX" --libdir lib \
    --buildtype release --prefer-static --default-library shared --wrap-mode=nodownload --auto-features=disabled \
    --sysconfdir=/bs-native/etc --datadir=/bs-native/share \
    -Dgpl=false -Dlibmpv=true -Dcplayer=false -Dtests=false -Dgl=disabled -Dcoreaudio=enabled -Dcocoa=enabled \
    "-Dc_link_args=['-lc++','-mmacosx-version-min=14.0']" >> "$BS_WORK/build.log" 2>&1
"$BS_PYTHON" -B "$BS_PROJECT/repo/packaging/native_sources.py" --sanitize "$BS_WORK/build-mpv/config.h" --root "$BS_WORK"
ninja -C "$BS_WORK/build-mpv" -j "$BS_JOBS" >> "$BS_WORK/build.log" 2>&1
mkdir "$BS_WORK/out"
cp -L "$BS_WORK/build-mpv/libmpv.2.dylib" "$BS_WORK/out/libmpv.2.dylib"
install_name_tool -id @rpath/libmpv.2.dylib "$BS_WORK/out/libmpv.2.dylib"
strip -x "$BS_WORK/out/libmpv.2.dylib"
codesign --force --sign - "$BS_WORK/out/libmpv.2.dylib"
"$BS_PYTHON" -B -c 'import ctypes,sys; ctypes.CDLL(sys.argv[1])' "$BS_WORK/out/libmpv.2.dylib"
if otool -L "$BS_WORK/out/libmpv.2.dylib" | tail -n +2 | grep -Eq '/(opt/homebrew|usr/local|Users)/'; then
    printf 'Unexpected non-system linkage in native player.\n' >&2; exit 1
fi
"$BS_PYTHON" -B "$BS_PROJECT/repo/packaging/native_sources.py" --record "$BS_WORK/out/libmpv.2.dylib"
BS_CACHE="$BS_PROJECT/.cache/macos-build/libmpv-lgpl"
if [[ -e "$BS_CACHE" ]]; then
    mv -- "$BS_CACHE" "$BS_WORK/previous-native"
fi
mv -- "$BS_WORK/out" "$BS_CACHE"
printf 'Native macOS player ready: %s\n' "$BS_CACHE"
