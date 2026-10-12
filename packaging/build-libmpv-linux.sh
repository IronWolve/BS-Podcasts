#!/usr/bin/env bash
# Build a project-local LGPL player with pinned, current FFmpeg; no system installs.
# The native receipt records the hash of this file. Any edit, even to a comment or
# an option that changes nothing, makes the receipt stale and the Linux deploy
# refuses it: rebuild the player after editing. Never edit the receipt to match.
set -euo pipefail
unset PYTHONPATH
BS_PROJECT="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/../.." && pwd -P)"
BS_PYTHON="$BS_PROJECT/dists/linux/.venv/bin/python"
BS_INPUTS_SHA="$(sha256sum "$BS_PROJECT/repo/packaging/native-sources.json" | cut -d ' ' -f 1)"
BS_RECIPE_SHA="$(sha256sum "$BS_PROJECT/repo/packaging/build-libmpv-linux.sh" | cut -d ' ' -f 1)"
BS_HELPER_SHA="$(sha256sum "$BS_PROJECT/repo/packaging/native_sources.py" | cut -d ' ' -f 1)"
BS_BUILD_SHA="$("$BS_PYTHON" -B -I -c '
import hashlib, json, os, pathlib, shlex, subprocess, sys
root = pathlib.Path(sys.argv[1])
files = ["build-libmpv-linux.sh", "native_sources.py", "native-sources.json"]
identity = {name: hashlib.sha256((root/name).read_bytes()).hexdigest() for name in files}
identity["environment"] = {name: os.environ.get(name, "") for name in ("CC", "CXX", "LDFLAGS", "CPPFLAGS", "LIBRARY_PATH", "CPATH", "C_INCLUDE_PATH", "CPLUS_INCLUDE_PATH", "PATH", "PKG_CONFIG_LIBDIR")}
identity["tools"] = {name: subprocess.check_output(shlex.split(command)+["--version"]).decode(errors="replace") for name, command in (("cc",os.environ.get("CC","cc")),("cxx",os.environ.get("CXX","c++")),("meson","meson"),("ninja","ninja"),("pkg-config","pkg-config"))}
print(hashlib.sha256(json.dumps(identity,sort_keys=True).encode()).hexdigest())
' "$BS_PROJECT/repo/packaging")"
if [[ -n "${BS_NATIVE_RESUME:-}" ]]; then
    BS_NATIVE_ROOT="$(cd -- "$BS_NATIVE_RESUME" && pwd -P)"
    [[ "$BS_NATIVE_ROOT" == "$BS_PROJECT"/tmp/linux-native.* ]] || { printf 'Resume only an owned project native build.\n' >&2; exit 1; }
    [[ -f "$BS_NATIVE_ROOT/inputs.sha256" && "$(< "$BS_NATIVE_ROOT/inputs.sha256")" == "$BS_INPUTS_SHA" ]] || { printf 'Native inputs changed; start a fresh build.\n' >&2; exit 1; }
    [[ -f "$BS_NATIVE_ROOT/build-identity.sha256" && "$(< "$BS_NATIVE_ROOT/build-identity.sha256")" == "$BS_BUILD_SHA" ]] || { printf 'Native recipe, helper, toolchain or environment changed; start a fresh build.\n' >&2; exit 1; }
else
    BS_NATIVE_ROOT="$(mktemp -d "$BS_PROJECT/tmp/linux-native.XXXXXX")"
fi
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
    if [[ -f "$BS_NATIVE_ROOT/build.log" ]]; then
        mkdir -p "$BS_PROJECT/logs"
        cp -- "$BS_NATIVE_ROOT/build.log" "$BS_PROJECT/logs/native-linux-$(date -u +%Y%m%dT%H%M%SZ)-status$status.log"
    fi
    if (( status == 0 )); then
        printf "%s Native Linux library prepared: $BS_CACHE/libmpv.so.2\n" "$(bs_paint 32 OK)"
        printf 'No system package or deployed application was changed.\n'
        bs_stats "$BS_CACHE/libmpv.so.2"
        # The library is in .cache and the log in logs/, so this run's work folder is removed. A failed run keeps it.
        if [[ "$BS_NATIVE_ROOT" == "$BS_PROJECT"/tmp/linux-native.* ]]; then
            rm -rf -- "$BS_NATIVE_ROOT"
        fi
    else
        printf "%s Native Linux build stopped with status $status; work folder kept: %s\n" "$(bs_paint 31 FAIL)" "$BS_NATIVE_ROOT"
    fi
    exit "$status"
}
trap bs_finish EXIT
BS_PREFIX="$BS_NATIVE_ROOT/prefix"
BS_SRC="$BS_NATIVE_ROOT/src"
BS_JOBS="${BS_NATIVE_JOBS:-2}"
[[ "$BS_JOBS" =~ ^[1-4]$ ]] || { printf 'BS_NATIVE_JOBS must be 1 to 4.\n' >&2; exit 1; }
pkg-config --exists alsa libpulse || { printf 'ALSA and PulseAudio development headers are required. No packages were installed.\n' >&2; exit 1; }
mkdir -p "$BS_PREFIX" "$BS_SRC" "$BS_NATIVE_ROOT/tmp"
export TMPDIR="$BS_NATIVE_ROOT/tmp" PYTHONDONTWRITEBYTECODE=1
# pkg-config sees only the prefix and the two system packages this build enables, so a
# host .pc file for any other library cannot change the build. Their files are linked
# into this run's folder; a dependency they gain later fails the configure step loudly.
BS_SYSTEM_PC="$BS_NATIVE_ROOT/system-pkgconfig"
mkdir -p "$BS_SYSTEM_PC"
for name in alsa libpulse; do
    ln -sfn "$(pkg-config --variable=pcfiledir "$name")/$name.pc" "$BS_SYSTEM_PC/$name.pc"
done
export PKG_CONFIG_PATH="$BS_PREFIX/lib/pkgconfig"
export PKG_CONFIG_LIBDIR="$BS_PREFIX/lib/pkgconfig:$BS_SYSTEM_PC"
export CFLAGS="-O2 -fPIC -ffile-prefix-map=$BS_NATIVE_ROOT=/bs-native -fdebug-prefix-map=$BS_NATIVE_ROOT=/bs-native"
export CXXFLAGS="$CFLAGS"
printf 'Native work/log directory: %s\n' "$BS_NATIVE_ROOT"
if [[ -z "${BS_NATIVE_RESUME:-}" ]]; then
    "$BS_PYTHON" -B -I "$BS_PROJECT/repo/packaging/native_sources.py" --source "$BS_SRC" --cache "$BS_PROJECT/.cache/native-sources"
    printf '%s\n' "$BS_INPUTS_SHA" > "$BS_NATIVE_ROOT/inputs.sha256"
    printf '%s\n' "$BS_BUILD_SHA" > "$BS_NATIVE_ROOT/build-identity.sha256"
fi
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
build_static() {
    local name="$1"; shift
    local source
    source="$(pinned_dir "$name")"
    local reconfigure=()
    [[ -f "$BS_NATIVE_ROOT/build-$source/meson-private/coredata.dat" ]] && reconfigure=(--reconfigure)
    meson setup "${reconfigure[@]}" "$BS_NATIVE_ROOT/build-$source" "$BS_SRC/$source" --prefix "$BS_PREFIX" --libdir lib \
        --buildtype release --default-library static --prefer-static --wrap-mode=nodownload -Db_staticpic=true "$@" >> "$BS_NATIVE_ROOT/build.log" 2>&1
    ninja -C "$BS_NATIVE_ROOT/build-$source" -j "$BS_JOBS" install >> "$BS_NATIVE_ROOT/build.log" 2>&1
    printf 'Built %s\n' "$source"
}
build_static freetype -Dharfbuzz=disabled -Dbrotli=disabled -Dpng=disabled -Dbzip2=disabled -Dzlib=internal -Dtests=disabled
build_static fribidi -Ddocs=false -Dtests=false
build_static harfbuzz -Dfreetype=enabled -Dglib=disabled -Dgobject=disabled -Dcairo=disabled -Dicu=disabled -Dtests=disabled -Ddocs=disabled
build_static libass -Dtest=disabled -Dfontconfig=disabled -Drequire-system-font-provider=false
if [[ ! -f "$BS_PREFIX/.done-ffmpeg" ]]; then
(
    cd "$BS_SRC/$BS_FFMPEG_DIR"
    ./configure --prefix="$BS_PREFIX" --cpu=generic --enable-pic --disable-shared --enable-static \
        --disable-autodetect --enable-zlib --disable-programs --disable-doc --disable-debug \
        --disable-encoders --disable-muxers --disable-outdevs --disable-indevs \
        --extra-cflags="$CFLAGS"
    "$BS_PYTHON" -B -I "$BS_PROJECT/repo/packaging/native_sources.py" --sanitize config.h --root "$BS_NATIVE_ROOT"
    make -j "$BS_JOBS"
    make install
) >> "$BS_NATIVE_ROOT/build.log" 2>&1
touch "$BS_PREFIX/.done-ffmpeg"
fi
printf 'Built FFmpeg %s\n' "$BS_FFMPEG_DIR"
# The same licence and configure checks the Windows recipe makes: the player is
# built only from an FFmpeg configured as LGPL, with autodetection off.
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
BS_RECONFIGURE=()
[[ -f "$BS_NATIVE_ROOT/build-mpv/meson-private/coredata.dat" ]] && BS_RECONFIGURE=(--reconfigure)
meson setup "${BS_RECONFIGURE[@]}" "$BS_NATIVE_ROOT/build-mpv" "$BS_SRC/$BS_MPV_DIR" --prefix "$BS_PREFIX" --libdir lib \
    --buildtype release --prefer-static --default-library shared --wrap-mode=nodownload --auto-features=disabled \
    --sysconfdir=/bs-native/etc --datadir=/bs-native/share \
    -Dgpl=false -Dlibmpv=true -Dcplayer=false -Dtests=false -Dgl=disabled -Dalsa=enabled -Dpulse=enabled \
    "-Dc_link_args=['-Wl,--no-undefined','-Wl,--no-as-needed','-lstdc++','-Wl,--as-needed']" >> "$BS_NATIVE_ROOT/build.log" 2>&1
"$BS_PYTHON" -B -I "$BS_PROJECT/repo/packaging/native_sources.py" --sanitize "$BS_NATIVE_ROOT/build-mpv/config.h" --root "$BS_NATIVE_ROOT"
ninja -C "$BS_NATIVE_ROOT/build-mpv" -j "$BS_JOBS" >> "$BS_NATIVE_ROOT/build.log" 2>&1
mkdir -p "$BS_NATIVE_ROOT/out"
cp -L "$BS_NATIVE_ROOT/build-mpv/libmpv.so.2" "$BS_NATIVE_ROOT/out/libmpv.so.2"
strip --strip-unneeded "$BS_NATIVE_ROOT/out/libmpv.so.2"
"$BS_PYTHON" -B -I -c 'import ctypes,sys; ctypes.CDLL(sys.argv[1])' "$BS_NATIVE_ROOT/out/libmpv.so.2"
BS_NEEDED="$(readelf -d "$BS_NATIVE_ROOT/out/libmpv.so.2")"
if grep -Eq 'NEEDED.*lib(avcodec|avformat|avutil|avfilter|swscale|swresample)' <<< "$BS_NEEDED"; then
    printf 'The native player unexpectedly depends on system FFmpeg.\n' >&2
    exit 1
fi
# Allow list: the glibc baseline, libstdc++, and the ALSA and PulseAudio clients
# this build enables. Any other linked library would be a dependency the deploy
# does not ship, so it fails the build here rather than at run time.
BS_ALLOWED_LIBS='^(libc\.so\.6|libm\.so\.6|libdl\.so\.2|libpthread\.so\.0|libgcc_s\.so\.1|libstdc\+\+\.so\.6|libasound\.so\.2|libpulse\.so\.0)$'
BS_UNEXPECTED_LIBS="$(readelf -d "$BS_NATIVE_ROOT/out/libmpv.so.2" | sed -n 's/.*Shared library: \[\(.*\)\]/\1/p' | grep -Ev "$BS_ALLOWED_LIBS" || true)"
if [[ -n "$BS_UNEXPECTED_LIBS" ]]; then
    printf 'The native player links a library outside the allow list: %s\n' "$BS_UNEXPECTED_LIBS" >&2
    exit 1
fi
[[ "$BS_RECIPE_SHA" == "$(sha256sum "$BS_PROJECT/repo/packaging/build-libmpv-linux.sh" | cut -d ' ' -f 1)" && "$BS_HELPER_SHA" == "$(sha256sum "$BS_PROJECT/repo/packaging/native_sources.py" | cut -d ' ' -f 1)" && "$BS_INPUTS_SHA" == "$(sha256sum "$BS_PROJECT/repo/packaging/native-sources.json" | cut -d ' ' -f 1)" ]] || { printf 'Native build inputs changed during compilation; refusing provenance.\n' >&2; exit 1; }
"$BS_PYTHON" -B -I "$BS_PROJECT/repo/packaging/native_sources.py" --record "$BS_NATIVE_ROOT/out/libmpv.so.2"
BS_CACHE="$BS_PROJECT/.cache/linux-build/libmpv-lgpl"
mkdir -p "$BS_PROJECT/.cache/linux-build"
if [[ -e "$BS_CACHE" ]]; then
    BS_PREVIOUS="$(mktemp -d "$BS_PROJECT/.cache/linux-build/previous-libmpv-lgpl-$(date -u +%Y%m%d)-XXXXXX")"
    mv -- "$BS_CACHE" "$BS_PREVIOUS/libmpv-lgpl"
fi
mv -- "$BS_NATIVE_ROOT/out" "$BS_CACHE"
