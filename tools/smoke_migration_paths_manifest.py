"""B03/B11: source-root mapping and OS-independent source digest."""
from pathlib import Path, PureWindowsPath
from tempfile import TemporaryDirectory
import sys
import json
import migrate_data_dir as migration
sys.path.insert(0,str(Path(__file__).resolve().parents[1]/'packaging'))
import build_manifest


class WindowsOrderPath:
    def __init__(self,path): self.path=Path(path)
    def __truediv__(self,name): return type(self)(self.path/name)
    def __lt__(self,other): return PureWindowsPath(str(self.path))<PureWindowsPath(str(other.path))
    def rglob(self,pattern): return (type(self)(p) for p in self.path.rglob(pattern))
    def relative_to(self,other): return self.path.relative_to(other.path)
    def __getattr__(self,name): return getattr(self.path,name)
    def __fspath__(self): return str(self.path)


def main():
    with TemporaryDirectory(dir=Path(__file__).resolve().parents[2]/'tmp',prefix='paths-manifest-') as folder:
        root=Path(folder); source=root/'source'
        folders=[source/'artwork',source/'cache/artwork']
        for directory,body in zip(folders,(b'old',b'new')):
            directory.mkdir(parents=True); (directory/'cover.img').write_bytes(body)
        mapping=migration._copy_assets(source,root/'copied',folders)
        for directory,body in zip(folders,(b'old',b'new')):
            assert Path(migration._mapped(str(directory/'cover.img'),mapping)).read_bytes()==body
        try: migration._mapped('C:/custom/cover.img',mapping)
        except ValueError: pass
        else: raise AssertionError('ambiguous basename guessed')
        (root/'src').mkdir(); (root/'packaging').mkdir()
        (root/'pyproject.toml').write_text('[project]\nname="fixture"\n')
        for name in ('Z.txt','a.txt'):
            (root/'packaging'/name).write_text(name)
        (root/'source-manifest.json').write_text(json.dumps({'files':['source-manifest.json','pyproject.toml','packaging/Z.txt','packaging/a.txt'],'runtime':{}}))
        assert build_manifest.source_inventory(root)==build_manifest.source_inventory(WindowsOrderPath(root))
    print('B03/B11: PASS distinct asset roots, ambiguous-reference refusal and identical cross-OS source digest')


if __name__=='__main__': main()
