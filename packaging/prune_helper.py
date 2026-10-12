"""Keep the newest rollbacks of one platform in the shared previous-deployments folder.

Each platform names its rollbacks with mkdtemp and its own prefix. The folder also holds other
platforms' rollbacks and staging, which are never counted or removed by this platform's prune.
Rollbacks are user recovery data, so the command-line prune lists the exact folders and their sizes
first and deletes only after a typed YES (confirm_and_prune).
"""
import re
import shutil
import sys

from report import size_text, tree_size


def plan_previous_deployments(root, prefix, keep=3, retain=None):
    """The rollbacks named with `prefix` that a prune would remove, oldest last; nothing is removed.

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
    return entries[max(keep - reserved, 0):]


def prune_previous_deployments(root, prefix, keep=3, retain=None):
    """Keep the newest `keep` rollbacks named with `prefix` (always including `retain`); return removed names."""
    removed = []
    for stale in plan_previous_deployments(root, prefix, keep, retain):
        shutil.rmtree(stale)
        removed.append(stale.name)
    return removed


def confirm_and_prune(root, prefix, report, keep=3, retain=None, ask=input, interactive=None):
    """List the rollbacks a prune would remove, with sizes, and delete exactly those after a typed YES.

    Nothing is removed without an interactive terminal or without the answer YES. Returns the removed
    names; the list is empty when nothing was listed or nothing was confirmed.
    """
    stale = plan_previous_deployments(root, prefix, keep, retain)
    if not stale:
        return []
    interactive = sys.stdin.isatty() if interactive is None else interactive
    sizes = {path: tree_size(path)[1] for path in stale}
    total = sum(sizes.values())
    for path in stale:
        report.info(f'{path}  {size_text(sizes[path])}')
    report.warn(f'Rollback folders older than the newest {keep}: {len(stale)} folders, {size_text(total)}')
    if not interactive:
        report.warn('No interactive input, so nothing was removed. Run the command from a terminal to confirm.')
        return []
    try:
        answer = ask('Type YES to delete the folders listed above: ')
    except EOFError:
        answer = ''
    if answer != 'YES':
        report.info('Not confirmed. Nothing was removed.')
        return []
    removed = []
    for path in stale:
        shutil.rmtree(path)
        removed.append(path.name)
    report.ok(f'Removed {len(removed)} rollback folders, {size_text(total)}')
    return removed
