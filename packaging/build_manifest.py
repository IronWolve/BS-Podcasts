"""Record source, build runtime, native inventory and final artifact hashes."""
import argparse
from datetime import datetime, timezone
import hashlib
import importlib.metadata
import json
import os
from pathlib import Path
import platform
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
    packages = sorted(({
        "name": distribution.metadata.get("Name", ""), "version": distribution.version,
        "license": distribution.metadata.get("License-Expression") or distribution.metadata.get("License") or "",
    } for distribution in importlib.metadata.distributions()), key=lambda item: item["name"].casefold())
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
        receipt=json.loads(Path(os.environ['BS_PODCASTS_NATIVE_RECEIPT']).read_text())
        native=artifact/'_internal/libmpv-2.dll'
        lock=ROOT/'packaging/native-sources.json'
        if receipt['sha256']!=digest(native) or receipt['inputs_sha256']!=digest(lock):
            raise ValueError('Native artifact does not match its approved inputs.')
        result['runtime']['native']={'file':'_internal/libmpv-2.dll','sha256':receipt['sha256'],
                                    'inputs':json.loads(lock.read_text())}
    return result


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--artifact", type=Path, required=True)
    parser.add_argument("--archive", type=Path)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--require-clean", action="store_true")
    args = parser.parse_args()
    try:
        result = create_manifest(args.artifact, args.archive, args.require_clean)
        args.output.parent.mkdir(parents=True, exist_ok=True)
        atomic_write(args.output, (json.dumps(result, indent=2) + "\n").encode())
    except (OSError, ValueError) as exc:
        raise SystemExit(str(exc))
    print(args.output)
