"""Inspect a frozen bundle without starting its GUI or opening a user profile."""
import argparse
from pathlib import Path, PureWindowsPath
from types import CodeType
from PyInstaller.archive.readers import CArchiveReader
from report import Reporter
from pyinstaller_payload import missing_app_modules
from source_manifest import privacy_scan_bytes, read_deny_file

_ENCODINGS=('utf-8','utf-16-le','utf-16-be')
# Byte-wise separator and ASCII-case fold. Applied identically to text in any of
# the scanned encodings, so a folded needle matches wherever its raw form does.
_FOLD=bytes.maketrans(b'\\'+bytes(range(65,91)),b'/'+bytes(range(97,123)))


def _fold_text(text):
    return text.replace('\\','/').casefold()


def token_views(tokens):
    """Raw and folded needles in each scanned encoding, normalised exactly like the views they meet."""
    exact,folded=set(),set()
    for token in tokens:
        if not token: continue
        for encoding in _ENCODINGS:
            encoded=token.encode(encoding,'surrogatepass')
            exact.add(encoded)
            folded.add(encoded.translate(_FOLD))
            folded.add(_fold_text(token).encode(encoding,'surrogatepass').translate(_FOLD))
    return frozenset(exact),frozenset(folded)


def denied_bytes(raw, views):
    """Raw bytes against raw needles, then the folded bytes against folded needles."""
    exact,folded=views
    scanned=privacy_scan_bytes(raw)
    if any(needle in scanned for needle in exact): return True
    scanned=scanned.translate(_FOLD)
    return any(needle in scanned for needle in folded)


def denied_constant(constant, views):
    """A code string, raw and casefolded/'/'-normalised, in each scanned encoding."""
    exact,folded=views
    # Approved public URLs are excluded before folding changes their spelling.
    text=privacy_scan_bytes(constant.encode('utf-8','surrogatepass')).decode('utf-8','surrogatepass')
    normalised=_fold_text(text)
    for encoding in _ENCODINGS:
        if any(needle in text.encode(encoding,'surrogatepass') for needle in exact): return True
        view=normalised.encode(encoding,'surrogatepass').translate(_FOLD)
        if any(needle in view for needle in folded): return True
    return False


def check(root, private_root):
    root=root.resolve()
    payload = root / ('Contents/Resources' if root.suffix == '.app' else '_internal')
    if not (payload / 'THIRD-PARTY-NOTICES.txt').is_file() or not (payload / 'licenses').is_dir():
        raise ValueError('Bundled notices or their supporting text folder are missing.')
    if not private_root:
        raise ValueError('The frozen privacy check needs the project root and its private deny file.')
    tokens=[str(private_root),str(private_root/'repo')]
    deny=read_deny_file(private_root/'.config/source-deny.txt', required=True)
    tokens.extend(deny)
    views=token_views(tokens)
    # Tool-state folder and file names come from the private deny file, matched in any case.
    path_tokens=[_fold_text(token) for token in deny if token]
    forbidden={'.git','auth.json','credentials.json','direct_url.json'}
    seen=set()
    paths=[p for p in root.rglob('*') if p.is_symlink() or (p.is_file() and not p.is_symlink())]
    for path in paths:
        relative=path.relative_to(root)
        folded_path=_fold_text(relative.as_posix())
        if (set(relative.parts)&forbidden or path.suffix.casefold() in {'.db','.sqlite','.sqlite3','.log','.pcap'}
                or any(token in folded_path for token in path_tokens)):
            raise ValueError('Private bundle file: '+relative.as_posix())
        blobs=[]
        target=path
        if path.is_symlink():
            blobs.append(str(path.readlink()).encode('utf-8','surrogateescape'))
            try:
                resolved=path.resolve(strict=True)
            except OSError:
                resolved=None
            if resolved is not None and resolved.is_file() and resolved.is_relative_to(root):
                target=resolved
            else:
                target=None
        if target is not None and target not in seen:
            seen.add(target)
            blobs.append(target.read_bytes())
        if any(denied_bytes(blob, views) for blob in blobs):
            raise ValueError('Known private reference in '+relative.as_posix())
        if 'mutagen' in relative.as_posix().casefold(): raise ValueError('Retired dependency found.')
    executable=root/('Contents/MacOS/BS Podcasts' if root.suffix=='.app' else 'BS Podcasts.exe')
    archive=CArchiveReader(str(executable))
    # The bundled Inter fonts ship under the OFL, which requires its licence text with each copy. This runs after the
    # payload rules above so that a bundle refused for another reason is still refused for that reason.
    if not (payload / 'bs_podcasts/assets/fonts/OFL.txt').is_file():
        raise ValueError('The font licence (bs_podcasts/assets/fonts/OFL.txt) is missing from the bundle.')
    pyz=archive.open_embedded_archive(next(n for n in archive.toc if n.endswith('.pyz')))
    if any(n=='mutagen' or n.startswith('mutagen.') for n in pyz.toc):
        raise ValueError('Retired module in frozen archive.')
    def code_check(code):
        if not isinstance(code,CodeType): return
        if Path(code.co_filename).is_absolute() or PureWindowsPath(code.co_filename).is_absolute():
            raise ValueError('Absolute application code filename.')
        for constant in code.co_consts:
            if isinstance(constant,CodeType): code_check(constant)
            elif isinstance(constant,str):
                if denied_constant(constant, views):
                    raise ValueError('Private application constant.')
    count=0
    for name in pyz.toc:
        if name=='bs_podcasts' or name.startswith('bs_podcasts.'):
            count+=1; code_check(pyz.extract(name))
    # A package that loads a submodule by name at run time hides it from the build's import analysis, and the
    # app then fails at startup in the built copy only. Every source module must be in the archive.
    absent = missing_app_modules(pyz.toc, Path(__file__).resolve().parents[1] / 'src')
    if absent:
        raise ValueError('Application modules missing from the frozen archive: ' + ', '.join(absent))
    console = Reporter()
    console.ok('Frozen privacy check passed')
    console.stats([('files', str(len(paths))), ('app modules', str(count))])


if __name__=='__main__':
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('bundle',type=Path)
    parser.add_argument('--private-root',type=Path,required=True)
    args=parser.parse_args()
    check(args.bundle,args.private_root)
