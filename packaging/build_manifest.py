"""Record source, build runtime, native inventory and final artifact hashes."""
import argparse
from datetime import datetime, timezone
import hashlib
import importlib.metadata
import json
import os
from pathlib import Path
import platform
import re
import subprocess
import sys
import tomllib
import tempfile
from source_manifest import source_digest

ROOT = Path(__file__).resolve().parents[1]
def atomic_write(target, content):
    descriptor, name = tempfile.mkstemp(prefix='.manifest-', dir=target.parent)
    try:
        with os.fdopen(descriptor, 'wb') as handle:
            handle.write(content)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(name, target)
    finally:
        Path(name).unlink(missing_ok=True)


def digest(path):
    with path.open("rb") as handle:
        return hashlib.file_digest(handle, "sha256").hexdigest()


def build_info_record(root=ROOT, native_sha256=None, built_at=None):
    """Create a portable runtime identity from an exact staged-source origin record."""
    root = Path(root).resolve()
    origin_path = root / '.build-origin.json'
    origin = json.loads(origin_path.read_text(encoding='utf-8'))
    if not isinstance(origin, dict):
        raise ValueError('Source origin is not a JSON object.')
    source_sha = origin.get('sha256')
    if not isinstance(source_sha, str) or not re.fullmatch(r'[0-9a-f]{64}', source_sha):
        raise ValueError('Source origin has no valid SHA-256.')
    if source_inventory(root) != source_sha:
        raise ValueError('Staged source changed after its snapshot was taken.')
    commit = origin.get('commit')
    if commit is not None and (not isinstance(commit, str) or not re.fullmatch(r'[0-9a-f]{40}|[0-9a-f]{64}', commit)):
        raise ValueError('Source origin has an invalid commit identifier.')
    dirty = origin.get('dirty')
    if not isinstance(dirty, bool):
        raise ValueError('Source origin has no valid dirty state.')
    stamp = datetime.now(timezone.utc).isoformat(timespec='microseconds') if built_at is None else built_at
    # Parse and normalize timestamps to UTC so offsets and host locale cannot affect identity.
    parsed = datetime.fromisoformat(stamp.replace('Z', '+00:00'))
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise ValueError('Build timestamp must include a timezone.')
    stamp = parsed.astimezone(timezone.utc).isoformat(timespec='microseconds')
    machine = platform.machine()
    safe_platform = f'{sys.platform}-{machine}'
    if not re.fullmatch(r'[A-Za-z0-9_.-]{1,80}', safe_platform):
        raise ValueError('Build platform contains unsupported characters.')
    native_sha = native_sha256
    if native_sha is not None and (not isinstance(native_sha, str) or not re.fullmatch(r'[0-9a-f]{64}', native_sha)):
        raise ValueError('Native library SHA-256 is invalid.')
    fields = {
        'source_sha256': source_sha,
        'commit': commit,
        'dirty': dirty,
        'built_at': stamp,
        'platform': safe_platform,
        'python': platform.python_version(),
        'native_sha256': native_sha,
    }
    identity = json.dumps(fields, sort_keys=True, separators=(',', ':')).encode('utf-8')
    return {'format': 1, 'build_id': hashlib.sha256(identity).hexdigest(), **fields}


def write_build_info(root=ROOT, native_sha256=None, output=None, built_at=None):
    """Write generated, untracked metadata beside the staged Python package."""
    root = Path(root).resolve()
    target = Path(output) if output is not None else root / 'src/bs_podcasts/build-info.json'
    target.parent.mkdir(parents=True, exist_ok=True)
    atomic_write(target, (json.dumps(build_info_record(root, native_sha256, built_at), indent=2) + '\n').encode('utf-8'))
    return target


def require_keys(receipt, keys, name):
    """Return the receipt, or raise ValueError naming the first key it must carry and does not."""
    if not isinstance(receipt, dict):
        raise ValueError(f"{name} is not a JSON object; rebuild the native library.")
    for key in keys:
        if key not in receipt:
            raise ValueError(f"{name} has no {key!r} entry; rebuild the native library.")
    return receipt


def source_inventory(root=ROOT):
    return source_digest(root)


def git_value(*args):
    try:
        top = subprocess.run(["git", "-C", str(ROOT), "rev-parse", "--show-toplevel"], capture_output=True, text=True, timeout=5)
        if top.returncode != 0 or Path(top.stdout.strip()).resolve() != ROOT:
            return None
        result = subprocess.run(["git", "-C", str(ROOT), *args], capture_output=True, text=True, timeout=5)
    except (OSError, subprocess.TimeoutExpired):
        return None
    return result.stdout.strip() if result.returncode == 0 else None


def artifact_inventory(path):
    if not path.exists():
        raise ValueError(f"Artifact not found: {path}")
    root = path if path.is_dir() else path.parent
    paths = sorted(path.rglob("*"), key=lambda item: item.relative_to(root).as_posix()) if path.is_dir() else [path]
    entries = []
    for item in paths:
        relative = item.relative_to(root).as_posix()
        if item.is_symlink():
            if not item.resolve().is_relative_to(root.resolve()):
                raise ValueError(f"Artifact links outside its bundle: {relative}")
            entries.append({"path": relative, "symlink": os.readlink(item)})
        elif item.is_file():
            entries.append({"path": relative, "bytes": item.stat().st_size, "sha256": digest(item)})
    if not entries:
        raise ValueError("Artifact is empty.")
    return entries


# Distributions that no frozen bundle carries: Pillow and setuptools are excluded by both specs, and the rest are
# build tools that the application never imports (THIRD-PARTY-NOTICES names them). The manifest records what remains.
BUILD_ONLY = frozenset({'altgraph', 'pefile', 'pillow', 'pip', 'pyinstaller', 'pyinstaller-hooks-contrib',
                        'pywin32-ctypes', 'setuptools'})


def normalized_name(name):
    """The PEP 503 spelling of a distribution name, so PySide6_Addons and pyside6-addons compare equal."""
    return re.sub(r'[-_.]+', '-', name).lower()


def runtime_packages(distributions):
    """The bundle's distributions, from (name, version, license) records of the build interpreter."""
    kept = [item for item in distributions if normalized_name(item['name']) not in BUILD_ONLY]
    return sorted(kept, key=lambda item: item['name'].casefold())


def create_manifest(artifact, archive=None, require_clean=False):
    status, commit = None, None
    origin_path = ROOT / '.build-origin.json'
    if origin_path.is_file():
        origin = json.loads(origin_path.read_text())
        commit, status = origin.get('commit'), 'dirty' if origin.get('dirty', True) else ''
        if origin.get('sha256') != source_inventory():
            raise ValueError('Staged source changed after its snapshot was taken.')
    else:
        status, commit = git_value("status", "--porcelain"), git_value("rev-parse", "HEAD")
    if require_clean and (commit is None or status != ""):
        raise ValueError("A release manifest requires a clean Git checkout.")
    project = tomllib.loads((ROOT / "pyproject.toml").read_text())["project"]
    files = artifact_inventory(artifact)
    packages = runtime_packages({
        "name": distribution.metadata.get("Name", ""), "version": distribution.version,
        "license": distribution.metadata.get("License-Expression") or distribution.metadata.get("License") or "",
    } for distribution in importlib.metadata.distributions())
    result = {
        "format": 1, "created_at": datetime.now(timezone.utc).isoformat(), "version": project["version"],
        "source": {"commit": commit, "dirty": status != "", "sha256": source_inventory()},
        "runtime": {"python": platform.python_version(), "platform": sys.platform, "architecture": platform.machine(), "packages": packages},
        "artifact": {"name": artifact.name, "files": files,
                     "tree_sha256": hashlib.sha256(json.dumps(files, sort_keys=True).encode()).hexdigest()},
    }
    if archive is not None:
        result["archive"] = {"name": archive.name, "bytes": archive.stat().st_size, "sha256": digest(archive)}
    if sys.platform == 'win32' and os.environ.get('BS_PODCASTS_NATIVE_RECEIPT'):
        receipt=require_keys(json.loads(Path(os.environ['BS_PODCASTS_NATIVE_RECEIPT']).read_text()),
                             ('sha256', 'inputs_sha256', 'recipe_sha256'), 'Windows native receipt')
        native=artifact/'_internal/libmpv-2.dll'
        lock=ROOT/'packaging/native-sources.json'
        recipe = ROOT/'packaging/build-libmpv-lgpl.sh'
        if (receipt['sha256']!=digest(native) or receipt['inputs_sha256']!=digest(lock)
                or receipt.get('recipe_sha256') != digest(recipe)):
            raise ValueError('Native artifact does not match its approved inputs.')
        result['runtime']['native']={'file':'_internal/libmpv-2.dll','sha256':receipt['sha256'],
                                    'inputs':json.loads(lock.read_text()), 'recipe_sha256':receipt['recipe_sha256']}
    if sys.platform == 'darwin' and os.environ.get('BS_PODCASTS_NATIVE_RECEIPT'):
        receipt_path=Path(os.environ['BS_PODCASTS_NATIVE_RECEIPT'])
        receipt=require_keys(json.loads(receipt_path.read_text()),
                             ('sha256', 'inputs_sha256', 'extra_inputs_sha256', 'patch_sha256', 'recipe_sha256',
                              'minimum_macos'), 'macOS native receipt')
        native=artifact/'Contents/Frameworks/libmpv.2.dylib'
        lock=ROOT/'packaging/native-sources.json'
        if (receipt['sha256']!=digest(receipt_path.with_name('libmpv.2.dylib'))
                or receipt['inputs_sha256']!=digest(lock)
                or receipt.get('extra_inputs_sha256')!=digest(ROOT/'packaging/native-sources-macos.json')
                or receipt.get('patch_sha256')!=digest(ROOT/'packaging/mpv-coreaudio-only.patch')
                or receipt['recipe_sha256']!=digest(ROOT/'packaging/build-libmpv-macos.sh')):
            raise ValueError('macOS native build receipt mismatch.')
        # PyInstaller rewrites Mach-O install names and signatures during bundling.
        result['runtime']['native']={'file':'Contents/Frameworks/libmpv.2.dylib',
            'sha256':digest(native),'input_sha256':receipt['sha256'],'inputs':json.loads(lock.read_text()),
            'recipe_sha256':receipt['recipe_sha256'],'minimum_macos':receipt['minimum_macos'],
            'extra_inputs':json.loads((ROOT/'packaging/native-sources-macos.json').read_text()),
            'patch_sha256':receipt['patch_sha256'],
            'extra_inputs_sha256':receipt['extra_inputs_sha256']}
        result['runtime']['signing']='Developer ID' if os.environ.get('BS_PODCASTS_SIGN_IDENTITY') else 'ad-hoc'
        result['runtime']['notarized']=False
    return result


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--artifact", type=Path)
    parser.add_argument("--archive", type=Path)
    parser.add_argument("--output", type=Path)
    parser.add_argument("--require-clean", action="store_true")
    parser.add_argument("--write-build-info", action="store_true",
                        help="write exact staged-source identity beside the bs_podcasts package")
    parser.add_argument("--native-sha256", help="optional native library digest included in the build identity")
    args = parser.parse_args()
    try:
        if args.write_build_info:
            print(write_build_info(ROOT, args.native_sha256))
            raise SystemExit(0)
        if args.artifact is None or args.output is None:
            raise ValueError("--artifact and --output are required when writing an artifact manifest.")
        result = create_manifest(args.artifact, args.archive, args.require_clean)
        args.output.parent.mkdir(parents=True, exist_ok=True)
        atomic_write(args.output, (json.dumps(result, indent=2) + "\n").encode())
    except (OSError, ValueError) as exc:
        raise SystemExit(str(exc))
    print(args.output)
