#!/usr/bin/env bash
# Cross-compile an LGPL libmpv-2.dll (mpv -Dgpl=false + LGPL FFmpeg) for x86_64 Windows.
set -euo pipefail
BS_BUILD_WORKSPACE="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/../.." && pwd -P)"
ROOT="$(mktemp -d "$BS_BUILD_WORKSPACE/tmp/lgpl-mpv.XXXXXX")"
BS_PYTHON="$BS_BUILD_WORKSPACE/dists/linux/.venv/bin/python"
PREFIX="$ROOT/prefix"
SRC="$ROOT/src"
JOBS="${BS_NATIVE_JOBS:-2}"
[[ "$JOBS" =~ ^[1-4]$ ]] || { printf 'BS_NATIVE_JOBS must be 1 to 4.\n' >&2; exit 1; }
TRIPLE=x86_64-w64-mingw32
mkdir -p "$PREFIX" "$SRC" "$ROOT/tmp"
export TMPDIR="$ROOT/tmp" PYTHONDONTWRITEBYTECODE=1
export PKG_CONFIG_LIBDIR="$PREFIX/lib/pkgconfig"
export PKG_CONFIG_PATH=""

cross_file="$ROOT/mingw-cross.txt"
cat > "$cross_file" <<CROSS
[binaries]
c = '${TRIPLE}-gcc'
cpp = '${TRIPLE}-g++'
ar = '${TRIPLE}-ar'
strip = '${TRIPLE}-strip'
windres = '${TRIPLE}-windres'
pkg-config = 'pkg-config'

[built-in options]
c_args = ['-ffile-prefix-map=$ROOT=/bs-native', '-fdebug-prefix-map=$ROOT=/bs-native']
cpp_args = ['-ffile-prefix-map=$ROOT=/bs-native', '-fdebug-prefix-map=$ROOT=/bs-native']
c_link_args = ['-static-libgcc', '-static', '-lstdc++']
cpp_link_args = ['-static-libgcc', '-static-libstdc++', '-static']

[host_machine]
system = 'windows'
cpu_family = 'x86_64'
cpu = 'x86_64'
endian = 'little'
CROSS

echo "=== fetch sources"
"$BS_PYTHON" -B "$BS_BUILD_WORKSPACE/repo/packaging/native_sources.py" \
  --source "$SRC" --cache "$BS_BUILD_WORKSPACE/.cache/native-sources"

meson_build() { # dir extra-args...
  local dir="$1"; shift
  if [ -f "$PREFIX/.done-$dir" ]; then echo "=== skip $dir (done)"; return; fi
  (cd "$SRC/$dir" && rm -rf build-mingw \
    && meson setup build-mingw --cross-file "$cross_file" --prefix "$PREFIX" \
         --buildtype release --default-library static --prefer-static --wrap-mode=nodownload "$@" \
    && ninja -C build-mingw -j $JOBS install) >> "$ROOT/build.log" 2>&1
  touch "$PREFIX/.done-$dir"
  echo "=== built $dir"
}

echo "=== zlib"
if [ ! -f "$PREFIX/.done-zlib" ]; then
  (cd "$SRC/zlib-1.3.2" && make -f win32/Makefile.gcc -j "$JOBS" \
       PREFIX="${TRIPLE}-" \
       CFLAGS="-O3 -Wall -ffile-prefix-map=$ROOT=/bs-native -fdebug-prefix-map=$ROOT=/bs-native" \
       prefix="$PREFIX" INCLUDE_PATH="$PREFIX/include" LIBRARY_PATH="$PREFIX/lib" \
       BINARY_PATH="$PREFIX/bin" SHARED_MODE=0 install) >> "$ROOT/build.log" 2>&1
  touch "$PREFIX/.done-zlib"
fi
echo "=== built zlib"

echo "=== freetype"
meson_build freetype-2.14.3 -Dharfbuzz=disabled -Dbrotli=disabled -Dpng=disabled -Dbzip2=disabled -Dzlib=internal -Dtests=disabled
echo "=== fribidi"
meson_build fribidi-1.0.17 -Ddocs=false -Dtests=false
echo "=== harfbuzz"
meson_build harfbuzz-14.5.0 -Dfreetype=enabled -Dglib=disabled -Dgobject=disabled -Dcairo=disabled -Dicu=disabled -Dtests=disabled -Ddocs=disabled
echo "=== libass"
meson_build libass-0.17.5 -Dtest=disabled -Dfontconfig=disabled

echo "=== ffmpeg (LGPL)"
if [ ! -f "$PREFIX/.done-ffmpeg" ]; then
(cd "$SRC/ffmpeg-9.0.2" && make distclean >/dev/null 2>&1 || true
 ./configure --prefix="$PREFIX" --arch=x86_64 --target-os=mingw32 \
   --cross-prefix=${TRIPLE}- --enable-cross-compile --pkg-config=pkg-config \
   --disable-shared --enable-static --disable-autodetect --enable-zlib \
   --disable-programs --disable-doc --disable-debug \
   --disable-encoders --disable-muxers --disable-outdevs --disable-indevs \
   --enable-schannel \
   --extra-cflags="-ffile-prefix-map=$ROOT=/bs-native -fdebug-prefix-map=$ROOT=/bs-native" \
 && "$BS_PYTHON" -B "$BS_BUILD_WORKSPACE/repo/packaging/native_sources.py" --sanitize config.h --root "$ROOT" \
 && make -j $JOBS && make install) >> "$ROOT/build.log" 2>&1
touch "$PREFIX/.done-ffmpeg"
fi
echo "=== built ffmpeg"
BS_FFMPEG_LOG="$SRC/ffmpeg-9.0.2/ffbuild/config.log"
BS_LICENSE="$(grep -E "^license=" "$BS_FFMPEG_LOG" | head -n 1 || true)"
BS_CONFIGURE="$(head -n 1 "$BS_FFMPEG_LOG" || true)"
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

echo "=== libplacebo"
if [ ! -f "$PREFIX/.done-libplacebo" ]; then
  (cd "$SRC/libplacebo-7.360.1" && rm -rf build-mingw \
    && meson setup build-mingw --cross-file "$cross_file" --prefix "$PREFIX" \
         --buildtype release --default-library static --prefer-static --wrap-mode=nodownload \
         -Dvulkan=disabled -Dopengl=disabled -Dd3d11=disabled -Ddemos=false \
         -Dtests=false -Dshaderc=disabled -Dglslang=disabled -Dlcms=disabled \
    && ninja -C build-mingw -j $JOBS install) >> "$ROOT/build.log" 2>&1
  touch "$PREFIX/.done-libplacebo"
fi
echo "=== built libplacebo"

echo "=== mpv (LGPL, libmpv only)"
(cd "$SRC/mpv-0.41.0" && rm -rf build-mingw \
 && meson setup build-mingw --cross-file "$cross_file" --prefix "$PREFIX" \
      --buildtype release --prefer-static --default-library shared --wrap-mode=nodownload \
      --sysconfdir=/bs-native/etc --datadir=/bs-native/share \
      -Dgpl=false -Dlibmpv=true -Dcplayer=false -Dtests=false \
      -Dlua=disabled -Djavascript=disabled -Dlibarchive=disabled -Dlibbluray=disabled \
      -Duchardet=disabled -Dzimg=disabled -Djpeg=disabled -Dvapoursynth=disabled \
      -Drubberband=disabled -Dlcms2=disabled \
 && "$BS_PYTHON" -B "$BS_BUILD_WORKSPACE/repo/packaging/native_sources.py" --sanitize build-mingw/config.h --root "$ROOT" \
 && ninja -C build-mingw -j $JOBS) >> "$ROOT/build.log" 2>&1
echo "=== built mpv"

DLL=$(find "$SRC/mpv-0.41.0/build-mingw" -name "libmpv-2.dll" | head -1)
[ -n "$DLL" ] || { echo "!!! libmpv-2.dll not produced"; exit 1; }
mkdir -p "$ROOT/out"
cp "$DLL" "$ROOT/out/libmpv-2.dll"
${TRIPLE}-strip "$ROOT/out/libmpv-2.dll"
"$BS_PYTHON" -B "$BS_BUILD_WORKSPACE/repo/packaging/native_sources.py" --record "$ROOT/out/libmpv-2.dll"
BS_CACHE="$BS_BUILD_WORKSPACE/.cache/windows-build/libmpv-lgpl"
mkdir -p "$BS_BUILD_WORKSPACE/.cache/windows-build"
if [[ -e "$BS_CACHE" ]]; then
    BS_PREVIOUS="$(mktemp -d "$ROOT/previous-native.XXXXXX")"
    mv -- "$BS_CACHE" "$BS_PREVIOUS/libmpv-lgpl"
fi
mv -- "$ROOT/out" "$BS_CACHE"
echo "=== DLL: $(ls -la "$BS_CACHE/libmpv-2.dll" | awk '{print $5}') bytes"
printf 'Native Windows library prepared: %s/libmpv-2.dll\n' "$BS_CACHE"
echo "=== imports:"
${TRIPLE}-objdump -p "$BS_CACHE/libmpv-2.dll" | grep "DLL Name" | sort
echo "=== BUILD COMPLETE"
