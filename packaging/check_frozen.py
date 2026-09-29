"""Inspect a frozen bundle without starting its GUI or opening a user profile."""
import argparse
from pathlib import Path, PureWindowsPath
from types import CodeType
from PyInstaller.archive.readers import CArchiveReader


def check(root, private_root=None):
    root=root.resolve()
    tokens=[]
    if private_root:
        tokens.extend((str(private_root),str(private_root/'repo')))
        deny=private_root/'.config/source-deny.txt'
        if deny.is_file(): tokens.extend(v.strip() for v in deny.read_text().splitlines() if v.strip())
    narrow=[v.replace('\\','/').casefold().encode() for v in tokens]
    wide=[v.encode('utf-16-le') for v in tokens]
    forbidden={'.git','.codex','.claude','.agents','auth.json','credentials.json','direct_url.json'}
    paths=[p for p in root.rglob('*') if p.is_file() and not p.is_symlink()]
    for path in paths:
        relative=path.relative_to(root)
        if set(relative.parts)&forbidden or path.suffix.casefold() in {'.db','.sqlite','.sqlite3','.log','.pcap'}:
            raise ValueError('Private bundle file: '+relative.as_posix())
        raw=path.read_bytes()
        normalized=raw.replace(b'\\',b'/').lower()
        if any(v in normalized for v in narrow) or any(v in raw for v in wide):
            raise ValueError('Known private reference in '+relative.as_posix())
        if 'mutagen' in relative.as_posix().casefold(): raise ValueError('Retired dependency found.')
    executable=root/('Contents/MacOS/BS Podcasts' if root.suffix=='.app' else 'BS Podcasts.exe')
    archive=CArchiveReader(str(executable))
    pyz=archive.open_embedded_archive(next(n for n in archive.toc if n.endswith('.pyz')))
    if any(n=='mutagen' or n.startswith('mutagen.') for n in pyz.toc):
        raise ValueError('Retired module in frozen archive.')
    def code_check(code):
        if not isinstance(code,CodeType): return
        if Path(code.co_filename).is_absolute() or PureWindowsPath(code.co_filename).is_absolute():
            raise ValueError('Absolute application code filename.')
        for constant in code.co_consts:
            if isinstance(constant,CodeType): code_check(constant)
            elif isinstance(constant,str) and any(v in constant.replace('\\','/').casefold().encode() for v in narrow):
                raise ValueError('Private application constant.')
    count=0
    for name in pyz.toc:
        if name=='bs_podcasts' or name.startswith('bs_podcasts.'):
            count+=1; code_check(pyz.extract(name))
    print(f'Frozen privacy check: {len(paths)} files, {count} app modules; PASS')


if __name__=='__main__':
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('bundle',type=Path)
    parser.add_argument('--private-root',type=Path)
    args=parser.parse_args()
    check(args.bundle,args.private_root)
