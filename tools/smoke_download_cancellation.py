"""Queued cancellation and failure-safe discard; tiny offline fixtures only."""
import sys
from pathlib import Path
from tempfile import TemporaryDirectory
from threading import Event
from concurrent.futures import ThreadPoolExecutor
from unittest.mock import patch

ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT/'src'))
from bs_podcasts.data import Database
from bs_podcasts.data.repositories import LibraryRepository,DownloadRepository
from bs_podcasts.domain import FeedData,FeedEpisodeData,DownloadState
from bs_podcasts.downloads.service import DownloadService,DownloadError


def main():
    with TemporaryDirectory(dir=ROOT.parent/'tmp',prefix='download-cancel-') as folder:
        root=Path(folder)
        library=LibraryRepository(Database(root/'library.db'))
        downloads=DownloadRepository(library.database)
        show=library.add_show('https://f.invalid/feed','Show')
        library.import_feed(show.id,FeedData('Show',episodes=tuple(
            FeedEpisodeData(str(i),str(i),media_url=f'https://m.invalid/{i}.mp3') for i in range(3))))
        ids=[e.id for e in library.list_episodes(show.id)]
        service=DownloadService(library,downloads,root)
        service.MIN_FREE_BYTES=0
        entered=Event(); calls=[]
        def body(episode_id,cancel):
            calls.append(episode_id)
            entered.set()
            assert cancel.wait(2)
            return service._park(episode_id,Path(downloads.get(episode_id).partial_path))
        with patch.object(service,'_download_once',body), ThreadPoolExecutor(1) as pool:
            tickets=[service.queue(i) for i in ids]
            futures=[pool.submit(service.download,i,t) for i,t in zip(ids,tickets)]
            assert entered.wait(2)
            assert len(service.records())==3
            assert service.pause_all()==3
            assert all(f.result(3).state==DownloadState.PAUSED for f in futures)
            assert calls==[ids[0]],calls
        old=service.queue(ids[1]); service.cancel(ids[1]); new=service.queue(ids[1])
        service.download(ids[1],old)
        assert service._queued[ids[1]] is new
        service.discard(ids[1])
        assert downloads.get(ids[1]) is None
        service.download(ids[1],new)
        assert downloads.get(ids[1]) is None

        item=ids[2]; record=downloads.get(item); partial=Path(record.partial_path)
        partial.write_bytes(b'kept')
        original=Path.unlink
        def locked(path,*args,**kwargs):
            if path==partial: raise PermissionError('simulated file lock')
            return original(path,*args,**kwargs)
        with patch.object(Path,'unlink',locked):
            try: service.discard(item)
            except DownloadError: pass
            else: raise AssertionError('failed discard reported success')
        assert partial.exists() and downloads.get(item).state==DownloadState.ERROR
        assert downloads.get(item).partial_path==str(partial)
        service.discard(item)
        assert not partial.exists() and downloads.get(item) is None
    print('A12/A13: PASS queued Pause all, replacement tickets, no resurrection and recoverable discard failure')


if __name__=='__main__': main()
