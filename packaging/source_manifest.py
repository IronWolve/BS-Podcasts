"""Exact source inventory, privacy checks and manifest-only build staging."""
import argparse
import hashlib
import json
from pathlib import Path, PurePosixPath
import re
import shutil
import subprocess
import sys
from functools import lru_cache

ROOT = Path(__file__).resolve().parents[1]

PRIVATE_ROOTS = frozenset({'data', 'docs', 'logs', 'tmp', 'run', 'dist', 'dists', 'build', 'node_modules'})
PRIVATE_NAMES = frozenset({'agents.md', 'agent.md', 'auth.json', 'credentials.json', 'id_rsa', 'id_ed25519'})


def private_path(name, tokens=()):
    """Generic private-path rules, plus any private deny token in the path (case-insensitive)."""
    parts = PurePosixPath(name).parts
    folded = name.casefold()
    return (parts[0].lower() in PRIVATE_ROOTS
            or any(part.lower() in PRIVATE_NAMES for part in parts)
            or any(part.startswith('.') for part in parts if part not in {'.gitignore', '.gitattributes'})
            or PurePosixPath(name).suffix.lower() in {'.db', '.sqlite', '.sqlite3', '.log', '.pcap', '.key', '.pem'}
            or any(token.casefold() in folded for token in tokens if token))


def inventory(root=ROOT):
    root = Path(root).resolve()
    payload = json.loads((root / 'source-manifest.json').read_text(encoding='utf-8'))
    files = payload['files']
    tokens = denied_tokens(root)
    if not isinstance(files, list) or len(files) != len(set(files)):
        raise ValueError('Source manifest must contain unique exact file paths.')
    if 'source-manifest.json' not in files:
        raise ValueError('The source manifest must include itself in its inventory.')
    for name in files:
        path = PurePosixPath(name)
        if path.is_absolute() or '..' in path.parts or str(path) != name or any(c in name for c in '\\:\0\r\n'):
            raise ValueError(f'Invalid manifest path: {name}')
        if private_path(name, tokens):
            raise ValueError(f'Private/agent/runtime path is not an approved source input: {name}')
        source = root / name
        if not source.is_file() or source.is_symlink() or not source.resolve().is_relative_to(root):
            raise ValueError(f'Missing or unsafe source file: {name}')
    runtime = payload['runtime']
    if not isinstance(runtime, dict) or not set(runtime).issubset(files):
        raise ValueError('Runtime inputs must be approved source files.')
    if len(set(runtime.values())) != len(runtime):
        raise ValueError('Duplicate deployment targets.')
    for name in runtime.values():
        path = PurePosixPath(name)
        if path.is_absolute() or '..' in path.parts or str(path) != name or any(c in name for c in '\\:\0\r\n'):
            raise ValueError(f'Invalid runtime path: {name}')
        if private_path(name, tokens):
            raise ValueError(f'Private/agent/runtime path is not an approved deployment output: {name}')
    return files, runtime


def file_digest(path):
    with path.open('rb') as handle:
        return hashlib.file_digest(handle, 'sha256').hexdigest()


def source_digest(root=ROOT):
    files, _ = inventory(root)
    records = [(name, file_digest(Path(root)/name)) for name in sorted(files)]
    return hashlib.sha256(json.dumps(records,separators=(',',':')).encode()).hexdigest()


@lru_cache(maxsize=4)
def _public_url_patterns(root):
    from urllib.parse import urlsplit
    urls = json.loads((Path(root) / 'source-manifest.json').read_text()).get('public_urls', [])
    if not isinstance(urls, list) or any(not isinstance(url, str) for url in urls):
        raise ValueError('Public URL approvals must be an explicit list.')
    patterns = []
    for url in sorted(urls, key=len, reverse=True):
        parts = urlsplit(url)
        if (parts.scheme != 'https' or parts.netloc not in {'github.com', 'api.github.com'}
                or parts.query or parts.fragment or '..' in parts.path.split('/')):
            raise ValueError('Public URL approvals must be canonical HTTPS GitHub project links.')
        for encoding in ('utf-8', 'utf-16-le', 'utf-16-be'):
            ending = b'|'.join(re.escape(c.encode(encoding)) for c in (' ', '\n', '\r', '\t', '"', "'", ')', ']', '}', ',', ';', '\0'))
            needle = url.encode(encoding)
            patterns.append((needle, re.compile(re.escape(needle) + b'(?=$|' + ending + b')')))
    return patterns


def privacy_scan_bytes(raw, root=ROOT):
    """Exclude only approved complete public URLs, never bare account names."""
    for needle, pattern in _public_url_patterns(str(root)):
        if needle in raw:
            raw = pattern.sub(b'[approved-public-project]', raw)
    return raw


_PNG_SIGNATURE = b'\x89PNG\r\n\x1a\n'
# Product names and image-generator markers live only in the private deny file;
# the source gate matches them case-insensitively, so this file names none of them.
_DENIED_ENCODINGS = ('utf-8', 'utf-16-le', 'utf-16-be')
_PNG_SKIP = (b'IHDR', b'PLTE', b'IDAT', b'IEND')
_FORBIDDEN_TRAILER = re.compile(r'(?im)^[ \t]*(?:co-authored-by|generated-with)\b')
_GENERATED_WITH = re.compile(r'(?i)\bgenerated[\s-]+with\b')


def _png_chunks(raw):
    if not raw.startswith(_PNG_SIGNATURE):
        return
    index = 8
    while index + 8 <= len(raw):
        length = int.from_bytes(raw[index:index + 4], 'big')
        chunk_type = raw[index + 4:index + 8]
        if len(chunk_type) != 4 or any(byte < 65 or 90 < byte < 97 or byte > 122 for byte in chunk_type):
            return
        payload_end = index + 8 + length
        if payload_end + 4 > len(raw):
            return
        yield chunk_type, raw[index + 8:payload_end]
        index = payload_end + 4
        if chunk_type == b'IEND':
            return


def _marker_needles(markers):
    # Byte-wise ASCII fold on both sides, so a raw match is always a folded match.
    return [marker.encode(encoding).lower() for marker in markers if marker for encoding in _DENIED_ENCODINGS]


def generator_metadata(raw, markers=(), root=ROOT):
    """True when a PNG carries a caBX chunk, or a private marker appears in any case
    in a PNG text chunk or anywhere in another file (approved public URLs excluded)."""
    needles = _marker_needles(markers)
    if raw.startswith(_PNG_SIGNATURE):
        for chunk_type, payload in _png_chunks(raw):
            if chunk_type == b'caBX':
                return True
            if chunk_type in _PNG_SKIP:
                continue
            folded = privacy_scan_bytes(payload, root).lower()
            if any(needle in folded for needle in needles):
                return True
        return False
    folded = privacy_scan_bytes(raw, root).lower()
    return any(needle in folded for needle in needles)


def deny_file(root):
    return Path(root).resolve().parent / '.config/source-deny.txt'


def read_deny_file(path, required=False):
    path = Path(path)
    if not path.is_file():
        if required:
            raise ValueError('Private-path deny file is required.')
        return []
    tokens = [line.strip() for line in path.read_text(encoding='utf-8').splitlines()
              if line.strip() and not line.strip().startswith('#')]
    if required and not tokens:
        raise ValueError('Private-path deny file has no tokens.')
    return tokens


def denied_tokens(root, required=False):
    return read_deny_file(deny_file(root), required=required)


def contains_denied(raw, tokens, root=ROOT):
    view = privacy_scan_bytes(raw, root)
    for token in tokens:
        if not token:
            continue
        for encoding in _DENIED_ENCODINGS:
            if token.encode(encoding) in view:
                return True
    return False


def git_tracked(root):
    try:
        top = subprocess.run(['git', '-C', str(root), 'rev-parse', '--show-toplevel'],
                             capture_output=True, text=True, timeout=5)
        if top.returncode != 0 or Path(top.stdout.strip()).resolve() != Path(root).resolve():
            return None
        tracked = subprocess.run(['git', '-C', str(root), 'ls-files', '-z'],
                                 capture_output=True, text=True, timeout=5, check=True)
    except (OSError, subprocess.SubprocessError):
        return None
    return set(tracked.stdout.split('\0')) - {''}


def tracking_difference(root, files):
    """None when Git is unavailable. Raises when the manifest and index differ."""
    tracked = git_tracked(root)
    if tracked is None:
        return None
    manifest = set(files)
    if tracked != manifest:
        extra = sorted(tracked - manifest)
        missing = sorted(manifest - tracked)
        detail = []
        if extra:
            detail.append('tracked but not listed: ' + ', '.join(extra))
        if missing:
            detail.append('listed but not tracked: ' + ', '.join(missing))
        raise ValueError('Source manifest and Git tracking differ (' + '; '.join(detail) + ').')
    return tracked


def reject_commit_trailers(root=ROOT):
    """Reject Co-Authored-By and generated-with commit trailers. Publisher entry point."""
    try:
        log = subprocess.run(
            ['git', '-C', str(root), 'log', '--all', '--format=%H%x1e%B%x1f'],
            capture_output=True, text=True, timeout=30, check=True)
    except (OSError, subprocess.SubprocessError) as exc:
        raise ValueError('Commit-message check requires Git history.') from exc
    bad = []
    for record in log.stdout.split('\x1f'):
        if not record.strip():
            continue
        commit, separator, message = record.partition('\x1e')
        if not separator:
            continue
        if _FORBIDDEN_TRAILER.search(message) or _GENERATED_WITH.search(message):
            bad.append(commit.strip()[:12])
    if bad:
        raise ValueError('Commit message contains a forbidden trailer: ' + ', '.join(bad))


_SECRETS = re.compile(r'(?:sk-[A-Za-z0-9]{24,}|ghp_[A-Za-z0-9]{30,}|github_pat_[A-Za-z0-9_]{30,}'
                      r'|(?:AKIA|ASIA)[0-9A-Z]{16}|xox[abprs]-[A-Za-z0-9-]{10,}|AIza[0-9A-Za-z_-]{35}'
                      r'|-----BEGIN [A-Z ]*PRIVATE KEY-----)')


def check(root=ROOT, require_deny=True, ran=None):
    """Run the source gates and return the approved file count.

    The private deny file is required unless a caller opts out explicitly. `ran`,
    when given, receives one line per gate saying what was checked or skipped."""
    root = Path(root).resolve()
    ran = [] if ran is None else ran
    files, _ = inventory(root)
    denied = denied_tokens(root, required=require_deny)
    ran.append(f'manifest inventory: {len(files)} exact paths, private-path rules')
    ran.append(f'private deny tokens: {len(denied)} (paths and contents, any case)' if denied
               else 'private deny tokens: SKIPPED, no deny file (explicit opt-out)')
    ran.append('provenance chunks and credential patterns: all files')
    secrets = _SECRETS
    failures = []
    for name in files:
        path = root / name
        blobs = []
        if path.is_symlink():
            blobs.append(str(path.readlink()).encode('utf-8', 'surrogateescape'))
            try:
                target = path.resolve(strict=True)
            except OSError:
                target = None
            if target is not None and target.is_file() and target.is_relative_to(root):
                blobs.append(target.read_bytes())
        else:
            blobs.append(path.read_bytes())
        if any(contains_denied(blob, denied, root) or generator_metadata(blob, denied, root) for blob in blobs):
            failures.append(name)
            continue
        text = b''.join(blobs).decode('utf-8', 'replace')
        if secrets.search(text):
            failures.append(name)
    if failures:
        raise ValueError('Private references, credential patterns, or generator metadata in: ' + ', '.join(failures))
    tracked = tracking_difference(root, files)
    if tracked is not None:
        reject_commit_trailers(root)
        ran.append('Git tracking parity and commit trailers: checked')
    else:
        ran.append('Git tracking parity and commit trailers: SKIPPED, not a Git checkout')
    return len(files)


def stage(destination, root=ROOT):
    root = Path(root).resolve()
    destination = Path(destination).resolve()
    if destination.is_relative_to(root) or root.is_relative_to(destination):
        raise ValueError('Staging must be outside the source checkout and cannot contain it.')
    check(root, require_deny=True)
    files, _ = inventory(root)
    tracked = tracking_difference(root, files)
    expected = source_digest(root)
    destination.mkdir(parents=True, exist_ok=False)
    for name in files:
        target = destination/name
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(root/name, target)
    if source_digest(destination) != expected or source_digest(root) != expected:
        raise ValueError('Source changed while staging; build cancelled before compilation.')
    origin={'commit':None,'dirty':True,'sha256':expected}
    inherited = root/'.build-origin.json'
    if inherited.is_file():
        previous=json.loads(inherited.read_text())
        if previous.get('sha256') != expected:
            raise ValueError('Imported source origin does not match its exact files.')
        origin.update(commit=previous.get('commit'), dirty=previous.get('dirty',True))
    try:
        top = subprocess.run(['git','-C',str(root),'rev-parse','--show-toplevel'],capture_output=True,text=True,timeout=5)
        if top.returncode==0 and Path(top.stdout.strip()).resolve()==root:
            commit = subprocess.run(['git','-C',str(root),'rev-parse','HEAD'],capture_output=True,text=True,timeout=5)
            status = subprocess.run(['git','-C',str(root),'status','--porcelain'],capture_output=True,text=True,timeout=5)
            differs = tracked is not None and tracked != set(files)
            origin.update(commit=commit.stdout.strip() if commit.returncode==0 else None,
                          dirty=differs or status.returncode!=0 or bool(status.stdout.strip()))
    except (OSError,subprocess.TimeoutExpired):
        pass  # Build hosts without Git still get content-addressed development artifacts.
    (destination/'.build-origin.json').write_text(json.dumps(origin,indent=2)+'\n')
    return destination


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('operation', choices=['check','stage','hash','list'])
    parser.add_argument('--destination', type=Path)
    parser.add_argument('--allow-missing-deny', action='store_true',
                        help='check without the private deny file (reported as skipped)')
    args = parser.parse_args()
    if args.operation == 'check':
        ran = []
        print(f'Approved source files: {check(require_deny=not args.allow_missing_deny, ran=ran)}')
        for line in ran:
            print('  ' + line, file=sys.stderr if 'SKIPPED' in line else sys.stdout)
    elif args.operation == 'hash':
        print(source_digest())
    elif args.operation == 'list':
        # Lists a snapshot that stage() just passed through every gate; it has no
        # private files beside it, so the deny file is read only when present.
        check(require_deny=False)
        sys.stdout.buffer.write(('\0'.join(inventory()[0])+'\0').encode())
    elif args.destination is None:
        parser.error('stage requires --destination')
    else:
        print(stage(args.destination))
