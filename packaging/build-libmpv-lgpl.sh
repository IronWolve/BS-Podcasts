#!/usr/bin/env bash
# Cross-compile an LGPL libmpv-2.dll (mpv -Dgpl=false + LGPL FFmpeg) for x86_64 Windows.
set -euo pipefail
ROOT="/path/to/work/podcast-codex/tmp/lgpl-mpv"
PREFIX="$ROOT/prefix"
SRC="$ROOT/src"
JOBS=28
TRIPLE=x86_64-w64-mingw32
mkdir -p "$PREFIX" "$SRC"
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
c_link_args = ['-static-libgcc', '-static']
cpp_link_args = ['-static-libgcc', '-static-libstdc++', '-static']

[host_machine]
system = 'windows'
cpu_family = 'x86_64'
cpu = 'x86_64'
endian = 'little'
CROSS

fetch() { # name url
  local file="$SRC/${2##*/}"
  [ -f "$file" ] || curl -L --fail --retry 3 -o "$file" "$2"
  case "$file" in
    *.tar.xz) tar -xJf "$file" -C "$SRC" ;;
    *.tar.gz) tar -xzf "$file" -C "$SRC" ;;
  esac
}

echo "=== fetch sources"
fetch freetype https://download.savannah.gnu.org/releases/freetype/freetype-2.13.3.tar.xz
fetch fribidi  https://github.com/fribidi/fribidi/releases/download/v1.0.16/fribidi-1.0.16.tar.xz
fetch harfbuzz https://github.com/harfbuzz/harfbuzz/releases/download/11.0.0/harfbuzz-11.0.0.tar.xz
fetch libass   https://github.com/libass/libass/releases/download/0.17.5/libass-0.17.5.tar.xz
fetch ffmpeg   https://ffmpeg.org/releases/ffmpeg-7.1.tar.xz
fetch mpv      https://github.com/mpv-player/mpv/archive/refs/tags/v0.41.0.tar.gz

meson_build() { # dir extra-args...
  local dir="$1"; shift
  if [ -f "$PREFIX/.done-$dir" ]; then echo "=== skip $dir (done)"; return; fi
  (cd "$SRC/$dir" && rm -rf build-mingw \
    && meson setup build-mingw --cross-file "$cross_file" --prefix "$PREFIX" \
         --buildtype release --default-library static --prefer-static "$@" \
    && ninja -C build-mingw -j $JOBS install) >> "$ROOT/build.log" 2>&1
  touch "$PREFIX/.done-$dir"
  echo "=== built $dir"
}

echo "=== freetype"
meson_build freetype-2.13.3 -Dharfbuzz=disabled -Dbrotli=disabled -Dpng=disabled -Dbzip2=disabled -Dzlib=internal -Dtests=disabled
echo "=== fribidi"
meson_build fribidi-1.0.16 -Ddocs=false -Dtests=false
echo "=== harfbuzz"
meson_build harfbuzz-11.0.0 -Dfreetype=enabled -Dglib=disabled -Dgobject=disabled -Dcairo=disabled -Dicu=disabled -Dtests=disabled -Ddocs=disabled
echo "=== libass"
meson_build libass-0.17.5 -Dtest=disabled -Dfontconfig=disabled

echo "=== ffmpeg (LGPL)"
if [ ! -f "$PREFIX/.done-ffmpeg" ]; then
(cd "$SRC/ffmpeg-7.1" && make distclean >/dev/null 2>&1 || true
 ./configure --prefix="$PREFIX" --arch=x86_64 --target-os=mingw32 \
   --cross-prefix=${TRIPLE}- --enable-cross-compile --pkg-config=pkg-config \
   --disable-shared --enable-static --disable-programs --disable-doc \
   --disable-encoders --disable-muxers --disable-outdevs --disable-indevs \
   --enable-schannel --disable-debug \
 && make -j $JOBS && make install) >> "$ROOT/build.log" 2>&1
touch "$PREFIX/.done-ffmpeg"
fi
echo "=== built ffmpeg"
grep -iE "^(License|version)" "$SRC/ffmpeg-7.1/ffbuild/config.log" 2>/dev/null | head -3 || true

echo "=== libplacebo"
if [ ! -f "$PREFIX/.done-libplacebo" ]; then
  [ -d "$SRC/libplacebo" ] || git clone --depth 1 --branch v7.351.0 --recursive https://code.videolan.org/videolan/libplacebo.git "$SRC/libplacebo" >> "$ROOT/build.log" 2>&1
  (cd "$SRC/libplacebo" && rm -rf build-mingw \
    && meson setup build-mingw --cross-file "$cross_file" --prefix "$PREFIX" \
         --buildtype release --default-library static --prefer-static \
         -Dvulkan=disabled -Dopengl=disabled -Dd3d11=disabled -Ddemos=false \
         -Dtests=false -Dshaderc=disabled -Dglslang=disabled -Dlcms=disabled \
    && ninja -C build-mingw -j $JOBS install) >> "$ROOT/build.log" 2>&1
  touch "$PREFIX/.done-libplacebo"
fi
echo "=== built libplacebo"

echo "=== mpv (LGPL, libmpv only)"
(cd "$SRC/mpv-0.41.0" && rm -rf build-mingw \
 && meson setup build-mingw --cross-file "$cross_file" --prefix "$PREFIX" \
      --buildtype release --prefer-static --default-library shared \
      -Dgpl=false -Dlibmpv=true -Dcplayer=false -Dtests=false \
      -Dlua=disabled -Djavascript=disabled -Dlibarchive=disabled -Dlibbluray=disabled \
      -Duchardet=disabled -Dzimg=disabled -Djpeg=disabled -Dvapoursynth=disabled \
      -Drubberband=disabled -Dlcms2=disabled \
 && ninja -C build-mingw -j $JOBS) >> "$ROOT/build.log" 2>&1
echo "=== built mpv"

DLL=$(find "$SRC/mpv-0.41.0/build-mingw" -name "libmpv-2.dll" | head -1)
[ -n "$DLL" ] || { echo "!!! libmpv-2.dll not produced"; exit 1; }
mkdir -p "$ROOT/out"
cp "$DLL" "$ROOT/out/libmpv-2.dll"
${TRIPLE}-strip "$ROOT/out/libmpv-2.dll"
echo "=== DLL: $(ls -la "$ROOT/out/libmpv-2.dll" | awk '{print $5}') bytes"
echo "=== imports:"
${TRIPLE}-objdump -p "$ROOT/out/libmpv-2.dll" | grep "DLL Name" | sort
echo "=== BUILD COMPLETE"
