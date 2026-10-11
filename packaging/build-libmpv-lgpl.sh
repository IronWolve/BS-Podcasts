#!/usr/bin/env bash
# Cross-compile an LGPL libmpv-2.dll (mpv -Dgpl=false + LGPL FFmpeg) for x86_64 Windows.
# Every run builds from scratch in its own work folder: nothing is resumed, so there
# are no completion markers.
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

# The fetched directory for a pinned name, read from native-sources.json, so no
# version is written into this recipe and a pin change cannot leave a stale path.
pinned_dir() {
  "$BS_PYTHON" -B -c '
import json, sys
for entry in json.load(open(sys.argv[1])):
    if entry["name"] == sys.argv[2]:
        print(entry["name"] + "-" + entry["version"])
        break
else:
    sys.exit("not pinned: " + sys.argv[2])
' "$BS_BUILD_WORKSPACE/repo/packaging/native-sources.json" "$1"
}
for name in zlib freetype fribidi harfbuzz libass ffmpeg libplacebo mpv; do
  fetched="$(pinned_dir "$name")"
  [ -d "$SRC/$fetched" ] || { echo "Fetched source is missing: $fetched" >&2; exit 1; }
done
ZLIB_DIR="$(pinned_dir zlib)"
FFMPEG_DIR="$(pinned_dir ffmpeg)"
LIBPLACEBO_DIR="$(pinned_dir libplacebo)"
MPV_DIR="$(pinned_dir mpv)"

meson_build() { # name extra-args...
  local name="$1"; shift
  local dir
  dir="$(pinned_dir "$name")"
  (cd "$SRC/$dir" && rm -rf build-mingw \
    && meson setup build-mingw --cross-file "$cross_file" --prefix "$PREFIX" \
         --buildtype release --default-library static --prefer-static --wrap-mode=nodownload "$@" \
    && ninja -C build-mingw -j $JOBS install) >> "$ROOT/build.log" 2>&1
  echo "=== built $dir"
}

echo "=== zlib"
(cd "$SRC/$ZLIB_DIR" && make -f win32/Makefile.gcc -j "$JOBS" \
     PREFIX="${TRIPLE}-" \
     CFLAGS="-O3 -Wall -ffile-prefix-map=$ROOT=/bs-native -fdebug-prefix-map=$ROOT=/bs-native" \
     prefix="$PREFIX" INCLUDE_PATH="$PREFIX/include" LIBRARY_PATH="$PREFIX/lib" \
     BINARY_PATH="$PREFIX/bin" SHARED_MODE=0 install) >> "$ROOT/build.log" 2>&1
echo "=== built zlib"

echo "=== freetype"
meson_build freetype -Dharfbuzz=disabled -Dbrotli=disabled -Dpng=disabled -Dbzip2=disabled -Dzlib=internal -Dtests=disabled
echo "=== fribidi"
meson_build fribidi -Ddocs=false -Dtests=false
echo "=== harfbuzz"
meson_build harfbuzz -Dfreetype=enabled -Dglib=disabled -Dgobject=disabled -Dcairo=disabled -Dicu=disabled -Dtests=disabled -Ddocs=disabled
echo "=== libass"
meson_build libass -Dtest=disabled -Dfontconfig=disabled

echo "=== ffmpeg (LGPL)"
(cd "$SRC/$FFMPEG_DIR" && make distclean >/dev/null 2>&1 || true
 ./configure --prefix="$PREFIX" --arch=x86_64 --target-os=mingw32 \
   --cross-prefix=${TRIPLE}- --enable-cross-compile --pkg-config=pkg-config \
   --disable-shared --enable-static --disable-autodetect --enable-zlib \
   --disable-programs --disable-doc --disable-debug \
   --disable-encoders --disable-muxers --disable-outdevs --disable-indevs \
   --enable-schannel \
   --extra-cflags="-ffile-prefix-map=$ROOT=/bs-native -fdebug-prefix-map=$ROOT=/bs-native" \
 && "$BS_PYTHON" -B "$BS_BUILD_WORKSPACE/repo/packaging/native_sources.py" --sanitize config.h --root "$ROOT" \
 && make -j $JOBS && make install) >> "$ROOT/build.log" 2>&1
echo "=== built ffmpeg"
BS_FFMPEG_LOG="$SRC/$FFMPEG_DIR/ffbuild/config.log"
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
(cd "$SRC/$LIBPLACEBO_DIR" && rm -rf build-mingw \
  && meson setup build-mingw --cross-file "$cross_file" --prefix "$PREFIX" \
       --buildtype release --default-library static --prefer-static --wrap-mode=nodownload \
       -Dvulkan=disabled -Dopengl=disabled -Dd3d11=disabled -Ddemos=false \
       -Dtests=false -Dshaderc=disabled -Dglslang=disabled -Dlcms=disabled \
  && ninja -C build-mingw -j $JOBS install) >> "$ROOT/build.log" 2>&1
echo "=== built libplacebo"

echo "=== mpv (LGPL, libmpv only)"
(cd "$SRC/$MPV_DIR" && rm -rf build-mingw \
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

DLL=$(find "$SRC/$MPV_DIR/build-mingw" -name "libmpv-2.dll" | head -1)
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
