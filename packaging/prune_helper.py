"""Keep the newest rollbacks of one platform in the shared previous-deployments folder.

Each platform names its rollbacks with mkdtemp and its own prefix. The folder also holds other
platforms' rollbacks and staging, which are never counted or removed by this platform's prune.
"""
import re
import shutil


def prune_previous_deployments(root, prefix, keep=3, retain=None):
    """Keep the newest `keep` rollbacks named with `prefix` (always including `retain`); return removed names.

    Only rollbacks that hold something count toward `keep`; an empty folder is neither counted nor removed.
    """
    if not root.is_dir() or root.is_symlink():
        return []
    name = re.compile(re.escape(prefix) + r'[a-z0-9_]+')
    entries = [path for path in root.iterdir()
               if name.fullmatch(path.name) and path.is_dir() and not path.is_symlink()
               and path != retain and any(path.iterdir())]
    entries.sort(key=lambda path: path.stat().st_mtime, reverse=True)
    reserved = 1 if retain is not None and retain.is_dir() and any(retain.iterdir()) else 0
    removed = []
    for stale in entries[max(keep - reserved, 0):]:
        shutil.rmtree(stale)
        removed.append(stale.name)
    return removed
