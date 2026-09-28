#!/usr/bin/env bash
# Build a project-local LGPL player with pinned, current FFmpeg; no system installs.
set -euo pipefail
BS_PROJECT="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/../.." && pwd -P)"
BS_PYTHON="$BS_PROJECT/dists/linux/.venv/bin/python"
BS_INPUTS_SHA="$(sha256sum "$BS_PROJECT/repo/packaging/native-sources.json" | cut -d ' ' -f 1)"
if [[ -n "${BS_NATIVE_RESUME:-}" ]]; then
    BS_NATIVE_ROOT="$(cd -- "$BS_NATIVE_RESUME" && pwd -P)"
    [[ "$BS_NATIVE_ROOT" == "$BS_PROJECT"/tmp/linux-native.* ]] || { printf 'Resume only an owned project native build.\n' >&2; exit 1; }
    [[ -f "$BS_NATIVE_ROOT/inputs.sha256" && "$(< "$BS_NATIVE_ROOT/inputs.sha256")" == "$BS_INPUTS_SHA" ]] || { printf 'Native inputs changed; start a fresh build.\n' >&2; exit 1; }
else
    BS_NATIVE_ROOT="$(mktemp -d "$BS_PROJECT/tmp/linux-native.XXXXXX")"
fi
BS_PREFIX="$BS_NATIVE_ROOT/prefix"
BS_SRC="$BS_NATIVE_ROOT/src"
BS_JOBS="${BS_NATIVE_JOBS:-2}"
[[ "$BS_JOBS" =~ ^[1-4]$ ]] || { printf 'BS_NATIVE_JOBS must be 1 to 4.\n' >&2; exit 1; }
pkg-config --exists alsa libpulse || { printf 'ALSA and PulseAudio development headers are required. No packages were installed.\n' >&2; exit 1; }
mkdir -p "$BS_PREFIX" "$BS_SRC" "$BS_NATIVE_ROOT/tmp"
export TMPDIR="$BS_NATIVE_ROOT/tmp" PYTHONDONTWRITEBYTECODE=1
export PKG_CONFIG_PATH="$BS_PREFIX/lib/pkgconfig"
unset PKG_CONFIG_LIBDIR
export CFLAGS="-O2 -fPIC -ffile-prefix-map=$BS_NATIVE_ROOT=/bs-native -fdebug-prefix-map=$BS_NATIVE_ROOT=/bs-native"
export CXXFLAGS="$CFLAGS"
printf 'Native work/log directory: %s\n' "$BS_NATIVE_ROOT"
if [[ -z "${BS_NATIVE_RESUME:-}" ]]; then
    "$BS_PYTHON" -B "$BS_PROJECT/repo/packaging/native_sources.py" --source "$BS_SRC" --cache "$BS_PROJECT/.cache/native-sources"
    printf '%s\n' "$BS_INPUTS_SHA" > "$BS_NATIVE_ROOT/inputs.sha256"
fi
build_static() {
    local source="$1"; shift
    local reconfigure=()
    [[ -f "$BS_NATIVE_ROOT/build-$source/meson-private/coredata.dat" ]] && reconfigure=(--reconfigure)
    meson setup "${reconfigure[@]}" "$BS_NATIVE_ROOT/build-$source" "$BS_SRC/$source" --prefix "$BS_PREFIX" --libdir lib \
        --buildtype release --default-library static --prefer-static --wrap-mode=nodownload -Db_staticpic=true "$@" >> "$BS_NATIVE_ROOT/build.log" 2>&1
    ninja -C "$BS_NATIVE_ROOT/build-$source" -j "$BS_JOBS" install >> "$BS_NATIVE_ROOT/build.log" 2>&1
    printf 'Built %s\n' "$source"
}
build_static freetype-2.14.3 -Dharfbuzz=disabled -Dbrotli=disabled -Dpng=disabled -Dbzip2=disabled -Dzlib=internal -Dtests=disabled
build_static fribidi-1.0.17 -Ddocs=false -Dtests=false
build_static harfbuzz-14.5.0 -Dfreetype=enabled -Dglib=disabled -Dgobject=disabled -Dcairo=disabled -Dicu=disabled -Dtests=disabled -Ddocs=disabled
build_static libass-0.17.5 -Dtest=disabled -Dfontconfig=disabled -Drequire-system-font-provider=false
if [[ ! -f "$BS_PREFIX/.done-ffmpeg" ]]; then
(
    cd "$BS_SRC/ffmpeg-9.0.2"
    ./configure --prefix="$BS_PREFIX" --cpu=generic --enable-pic --disable-shared --enable-static \
        --disable-autodetect --enable-zlib --disable-programs --disable-doc --disable-debug \
        --disable-encoders --disable-muxers --disable-outdevs --disable-indevs \
        --extra-cflags="$CFLAGS"
    "$BS_PYTHON" -B "$BS_PROJECT/repo/packaging/native_sources.py" --sanitize config.h --root "$BS_NATIVE_ROOT"
    make -j "$BS_JOBS"
    make install
) >> "$BS_NATIVE_ROOT/build.log" 2>&1
touch "$BS_PREFIX/.done-ffmpeg"
fi
printf 'Built FFmpeg 9.0.2\n'
build_static libplacebo-7.360.1 -Dvulkan=disabled -Dopengl=disabled -Dd3d11=disabled -Ddemos=false \
    -Dtests=false -Dshaderc=disabled -Dglslang=disabled -Dlcms=disabled
BS_RECONFIGURE=()
[[ -f "$BS_NATIVE_ROOT/build-mpv/meson-private/coredata.dat" ]] && BS_RECONFIGURE=(--reconfigure)
meson setup "${BS_RECONFIGURE[@]}" "$BS_NATIVE_ROOT/build-mpv" "$BS_SRC/mpv-0.41.0" --prefix "$BS_PREFIX" --libdir lib \
    --buildtype release --prefer-static --default-library shared --wrap-mode=nodownload --auto-features=disabled \
    --sysconfdir=/bs-native/etc --datadir=/bs-native/share \
    -Dgpl=false -Dlibmpv=true -Dcplayer=false -Dtests=false -Dgl=disabled -Dalsa=enabled -Dpulse=enabled \
    "-Dc_link_args=['-Wl,--no-undefined','-Wl,--no-as-needed','-lstdc++','-Wl,--as-needed']" >> "$BS_NATIVE_ROOT/build.log" 2>&1
"$BS_PYTHON" -B "$BS_PROJECT/repo/packaging/native_sources.py" --sanitize "$BS_NATIVE_ROOT/build-mpv/config.h" --root "$BS_NATIVE_ROOT"
ninja -C "$BS_NATIVE_ROOT/build-mpv" -j "$BS_JOBS" >> "$BS_NATIVE_ROOT/build.log" 2>&1
mkdir "$BS_NATIVE_ROOT/out"
cp -L "$BS_NATIVE_ROOT/build-mpv/libmpv.so.2" "$BS_NATIVE_ROOT/out/libmpv.so.2"
strip --strip-unneeded "$BS_NATIVE_ROOT/out/libmpv.so.2"
"$BS_PYTHON" -B -c 'import ctypes,sys; ctypes.CDLL(sys.argv[1])' "$BS_NATIVE_ROOT/out/libmpv.so.2"
if readelf -d "$BS_NATIVE_ROOT/out/libmpv.so.2" | grep -Eq 'NEEDED.*lib(avcodec|avformat|avutil|avfilter|swscale|swresample)'; then
    printf 'The native player unexpectedly depends on system FFmpeg.\n' >&2
    exit 1
fi
"$BS_PYTHON" -B "$BS_PROJECT/repo/packaging/native_sources.py" --record "$BS_NATIVE_ROOT/out/libmpv.so.2"
BS_CACHE="$BS_PROJECT/.cache/linux-build/libmpv-lgpl"
mkdir -p "$BS_PROJECT/.cache/linux-build"
if [[ -e "$BS_CACHE" ]]; then
    BS_PREVIOUS="$(mktemp -d "$BS_NATIVE_ROOT/previous-native.XXXXXX")"
    mv -- "$BS_CACHE" "$BS_PREVIOUS/libmpv-lgpl"
fi
mv -- "$BS_NATIVE_ROOT/out" "$BS_CACHE"
printf 'Native Linux library prepared: %s/libmpv.so.2\nNo system package or deployed application was changed.\n' "$BS_CACHE"
