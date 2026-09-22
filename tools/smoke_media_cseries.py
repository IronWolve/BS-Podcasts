"""C08/C09: resumed media validation and shared cache preservation, offline."""
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch
import sys
sys.path.insert(0,str(Path(__file__).resolve().parents[1]/'src'))
from bs_podcasts.data import Database
from bs_podcasts.data.repositories import LibraryRepository,DownloadRepository
from bs_podcasts.domain import FeedData,FeedEpisodeData,DownloadState
from bs_podcasts.downloads.service import DownloadService,DownloadError
from smoke_download_integrity import Response,Session


class Fragmented(Response):
    def iter_content(self,n):
        yield self.body[:1]
        yield self.body[1:]


def main():
    root=Path(__file__).resolve().parents[2]
    with TemporaryDirectory(dir=root/'tmp',prefix='media-cseries-') as folder:
        folder=Path(folder); repo=LibraryRepository(Database(folder/'library.db'))
        records=DownloadRepository(repo.database)
        show=repo.add_show('https://feed.invalid/a','First')
        other=repo.add_show('https://feed.invalid/b','Second')
        url='https://media.invalid/e.mp3'
        repo.import_feed(show.id,FeedData('First',episodes=(FeedEpisodeData('e','E',media_url=url),)))
        episode=repo.list_episodes()[0]
        payload=b'MZnot-audio'
        headers={'Content-Type':'audio/mpeg','Content-Length':str(len(payload)),'ETag':'"v1"'}
        session=Session(Fragmented(payload,headers)); service=DownloadService(repo,records,folder/'downloads',session)
        service.MIN_FREE_BYTES=0
        with patch('bs_podcasts.downloads.service.ensure_fetchable',side_effect=lambda url,*a:url):
            for attempt in range(2):
                if attempt:
                    session.response=Response(payload[1:],dict(headers,**{'Content-Length':str(len(payload)-1),
                        'Content-Range':f'bytes 1-{len(payload)-1}/{len(payload)}'}),206)
                try: service.download(episode.id)
                except DownloadError: pass
                else: raise AssertionError('non-media completed')
                assert records.get(episode.id).state==DownloadState.ERROR
                assert not repo.get_episode(episode.id).downloaded_path
        art=folder/'shared.img'; art.write_bytes(b'cache')
        repo.set_artwork_path(show.id,str(art))
        preview=repo.removal_preview(show.id)
        assert str(art) not in [p for p,_ in preview['files']]
        repo.remove_show(show.id)
        repo.set_artwork_path(other.id,str(art))
        assert art.exists() and repo.get_show(other.id).artwork_path==str(art)
        repo.database.close()
    print('C08/C09: PASS resumed non-media rejected and later artwork reference preserved')


if __name__=='__main__': main()
