#!/usr/bin/env bash
# Cross-compile an LGPL libmpv-2.dll (mpv -Dgpl=false + LGPL FFmpeg) for x86_64 Windows.
# Every run builds from scratch in its own work folder: nothing is resumed, so there
# are no completion markers.
set -euo pipefail
unset PYTHONPATH
BS_BUILD_WORKSPACE="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/../.." && pwd -P)"
# --prune-kept lists the work folders that stopped runs left in tmp, with their sizes, and deletes
# them only after a typed yes. It is the only way these folders are removed; a build never prunes.
if [[ "${1:-}" == --prune-kept ]]; then
    shopt -s nullglob
    kept=("$BS_BUILD_WORKSPACE"/tmp/lgpl-mpv.*)
    if (( ${#kept[@]} == 0 )); then
        printf 'No kept lgpl-mpv work folders in tmp.\n'
        exit 0
    fi
    du -sh -- "${kept[@]}"
    du -shc -- "${kept[@]}" | tail -n 1 | sed 's/^/total  /'
    printf 'Delete these %s folder(s)? Type yes to delete: ' "${#kept[@]}"
    read -r answer || answer=''
    if [[ "$answer" == yes ]]; then
        rm -rf -- "${kept[@]}"
        printf 'Deleted %s kept work folder(s).\n' "${#kept[@]}"
    else
        printf 'Nothing deleted.\n'
    fi
    exit 0
fi
ROOT="$(mktemp -d "$BS_BUILD_WORKSPACE/tmp/lgpl-mpv.XXXXXX")"
BS_PYTHON="$BS_BUILD_WORKSPACE/dists/linux/.venv/bin/python"
# The receipt attests the recipe, helper and pinned inputs as they were when this run started. They are
# hashed now, kept in the work folder, and compared again before the receipt is written (as the Linux recipe does).
bs_identity() {
    "$BS_PYTHON" -B -I -c '
import hashlib, pathlib, sys
root = pathlib.Path(sys.argv[1])
for name in sys.argv[2:]:
    print(hashlib.sha256((root / name).read_bytes()).hexdigest(), name)
' "$BS_BUILD_WORKSPACE/repo/packaging" build-libmpv-lgpl.sh native_sources.py native-sources.json
}
bs_identity > "$ROOT/identity.txt"
PREFIX="$ROOT/prefix"
SRC="$ROOT/src"
JOBS="${BS_NATIVE_JOBS:-2}"
[[ "$JOBS" =~ ^[1-4]$ ]] || { printf 'BS_NATIVE_JOBS must be 1 to 4.\n' >&2; exit 1; }
TRIPLE=x86_64-w64-mingw32
mkdir -p "$PREFIX" "$SRC" "$ROOT/tmp"
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
    if [[ -f "$ROOT/build.log" ]]; then
        mkdir -p "$BS_BUILD_WORKSPACE/logs"
        cp -- "$ROOT/build.log" "$BS_BUILD_WORKSPACE/logs/native-windows-$(date -u +%Y%m%dT%H%M%SZ)-status$status.log"
    fi
    if (( status == 0 )); then
        printf "%s Native Windows library prepared: $BS_CACHE/libmpv-2.dll\n" "$(bs_paint 32 OK)"
        bs_stats "$BS_CACHE/libmpv-2.dll"
        # The output and the log are kept outside tmp, so this run's own work folder is no longer needed.
        if [[ "$ROOT" == "$BS_BUILD_WORKSPACE"/tmp/lgpl-mpv.* ]]; then
            rm -rf -- "$ROOT"
            printf 'Work folder removed (output kept in .cache): %s\n' "$ROOT"
        fi
    else
        printf "%s Native Windows build stopped with status $status; work folder kept: %s\n" "$(bs_paint 31 FAIL)" "$ROOT"
    fi
    exit "$status"
}
trap bs_finish EXIT
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
"$BS_PYTHON" -B -I "$BS_BUILD_WORKSPACE/repo/packaging/native_sources.py" \
  --source "$SRC" --cache "$BS_BUILD_WORKSPACE/.cache/native-sources"

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
 && "$BS_PYTHON" -B -I "$BS_BUILD_WORKSPACE/repo/packaging/native_sources.py" --sanitize config.h --root "$ROOT" \
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
 && "$BS_PYTHON" -B -I "$BS_BUILD_WORKSPACE/repo/packaging/native_sources.py" --sanitize build-mingw/config.h --root "$ROOT" \
 && ninja -C build-mingw -j $JOBS) >> "$ROOT/build.log" 2>&1
echo "=== built mpv"

DLL=$(find "$SRC/$MPV_DIR/build-mingw" -name "libmpv-2.dll" | head -1)
[ -n "$DLL" ] || { echo "!!! libmpv-2.dll not produced"; exit 1; }
mkdir -p "$ROOT/out"
cp "$DLL" "$ROOT/out/libmpv-2.dll"
${TRIPLE}-strip "$ROOT/out/libmpv-2.dll"
# Refuse before the receipt is written if the recipe, helper or inputs changed during the build.
bs_identity | cmp -s - "$ROOT/identity.txt" || { printf 'Native recipe, helper or pinned inputs changed during the build; refusing provenance.\n' >&2; exit 1; }
"$BS_PYTHON" -B -I "$BS_BUILD_WORKSPACE/repo/packaging/native_sources.py" --record "$ROOT/out/libmpv-2.dll"
BS_CACHE="$BS_BUILD_WORKSPACE/.cache/windows-build/libmpv-lgpl"
mkdir -p "$BS_BUILD_WORKSPACE/.cache/windows-build"
if [[ -e "$BS_CACHE" ]]; then
    BS_PREVIOUS="$(mktemp -d "$BS_BUILD_WORKSPACE/.cache/windows-build/previous-libmpv-lgpl-$(date -u +%Y%m%d)-XXXXXX")"
    mv -- "$BS_CACHE" "$BS_PREVIOUS/libmpv-lgpl"
fi
mv -- "$ROOT/out" "$BS_CACHE"
echo "=== DLL: $(ls -la "$BS_CACHE/libmpv-2.dll" | awk '{print $5}') bytes"
echo "=== imports:"
${TRIPLE}-objdump -p "$BS_CACHE/libmpv-2.dll" | grep "DLL Name" | sort
echo "=== BUILD COMPLETE"
