"""Offline protocol/unsafe-file regressions with actual scratch download records."""
import sys
from pathlib import Path
from tempfile import TemporaryDirectory
from threading import Event
from unittest.mock import patch

ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT/'src'))
from bs_podcasts.data import Database
from bs_podcasts.data.repositories import LibraryRepository,DownloadRepository
from bs_podcasts.domain import FeedData,FeedEpisodeData,DownloadState
from bs_podcasts.downloads.service import DownloadService,DownloadError
from bs_podcasts.urlguard import ensure_external_media_file,UnsafeUrl


class Response:
    def __init__(self,body,headers,status=200): self.body=body; self.headers=headers; self.status_code=status
    def raise_for_status(self): pass
    def iter_content(self,n): yield self.body
    def close(self): pass


class Session:
    def __init__(self,response): self.response=response; self.headers=None
    def get(self,url,**kwargs): self.headers=kwargs['headers']; return self.response


def check_case(mode):
    with TemporaryDirectory(dir=ROOT.parent/'tmp',prefix='download-integrity-') as folder:
        root=Path(folder)
        library=LibraryRepository(Database(root/'library.db'))
        downloads=DownloadRepository(library.database)
        show=library.add_show('https://f.invalid/feed','Show')
        url='http://127.0.0.1/e.mp3' if mode=='blocked' else 'https://m.invalid/e.exe' if mode=='executable' else 'https://m.invalid/e.mp3'
        library.import_feed(show.id,FeedData('Show',episodes=(FeedEpisodeData('e','Episode',media_url=url),)))
        episode_id=library.list_episodes(show.id)[0].id
        headers={'Content-Type':'audio/mpeg','Content-Length':'4','Content-Range':'bytes 4-7/8','ETag':'"v1"'}
        body,status=b'BBBB',206
        if mode=='short': headers.update({'Content-Length':'2','Content-Range':'bytes 4-5/10'}); body=b'BB'
        if mode=='changed': headers['ETag']='"v2"'
        if mode=='no_validator': body=b'NEW-FILE'; status=200; headers={'Content-Type':'audio/mpeg','Content-Length':'8'}
        if mode=='executable': body=b'MZ'+bytes(20); status=200; headers={'Content-Type':'audio/mpeg','Content-Length':'22'}
        session=Session(Response(body,headers,status))
        service=DownloadService(library,downloads,root,session=session)
        service.MIN_FREE_BYTES=0
        if mode=='blocked':
            try: service.download(episode_id)
            except DownloadError: pass
            else: raise AssertionError('blocked URL accepted')
            assert downloads.get(episode_id).state==DownloadState.ERROR
            assert not service.is_active(episode_id) and session.headers is None
            return
        target=service._target(episode_id,url); partial=target.with_suffix(target.suffix+'.part')
        downloads.prepare(episode_id,url,target,partial)
        if mode!='executable':
            partial.write_bytes(b'AAAA')
            if mode!='no_validator': downloads.response_metadata(episode_id,'"v1"','',10 if mode=='short' else 8)
        with patch('bs_podcasts.downloads.service.ensure_fetchable',lambda *args:args[0]):
            try: result=service._download_once(episode_id,Event())
            except DownloadError:
                assert mode in {'short','changed','executable'},mode
                assert downloads.get(episode_id).state!=DownloadState.COMPLETE
                assert not target.exists()
            else:
                assert mode in {'valid','no_validator'},mode
                assert result.state==DownloadState.COMPLETE
                assert target.read_bytes()==(b'NEW-FILE' if mode=='no_validator' else b'AAAABBBB')
        if mode=='valid': assert session.headers['If-Range']=='"v1"'
        if mode=='no_validator': assert 'Range' not in session.headers
        if mode=='executable':
            assert target.suffix=='.media'
            legacy=root/'legacy.exe'; legacy.write_bytes(body)
            try: ensure_external_media_file(str(legacy))
            except UnsafeUrl: pass
            else: raise AssertionError('external executable accepted')


if __name__=='__main__':
    for case in ('valid','short','changed','no_validator','executable','blocked'):
        check_case(case)
        print(case+': PASS')
    print('A05/A06/A34: PASS')
