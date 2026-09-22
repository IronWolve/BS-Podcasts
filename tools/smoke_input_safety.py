"""Small offline regressions for metadata, local sources and orphan ownership."""
import sys
import json
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch

ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT/'src'))
from bs_podcasts.data import Database
from bs_podcasts.data.repositories import LibraryRepository
from bs_podcasts.domain import FeedData,FeedEpisodeData
from bs_podcasts.feeds.parser import _duration,parse_feed
from bs_podcasts.feeds.opml import export_opml
from bs_podcasts.feeds.refresh import RefreshService
from bs_podcasts.feeds.fetch import FeedFetcher
from bs_podcasts.ui.widgets import safe_feed_html
from smoke_playback_ordering import Engine
from bs_podcasts.playback.service import PlaybackService
from bs_podcasts.playback.engine import EngineEvent


class Response:
    status_code=200
    url='https://f.invalid/feed'
    def __init__(self,body): self.body=body
    def iter_content(self,size): yield self.body
    def close(self): pass

class Session:
    def __init__(self,body): self.body=body
    def get(self,*args,**kwargs): return Response(self.body)


def main():
    for value in ('inf','nan','1e100','99999999999999999:00:00'):
        assert _duration(value)==0,value
    assert _duration('1:02:03')==3723
    assert 'href' not in safe_feed_html('<p><a href="http://[invalid">link</a></p>')
    assert 'kept' in safe_feed_html('<svg/>kept')
    body=b'<rss><channel><title>T</title><item><title>Old</title><pubDate>2026-01-01T00:00:00</pubDate><enclosure type="audio/mpeg" url="https://m.invalid/a.mp3"/></item><item><title>New</title><pubDate>2026-09-01T00:00:00</pubDate><enclosure type="audio/mpeg" url="https://m.invalid/b.mp3"/></item></channel></rss>'
    with patch('bs_podcasts.urlguard._addresses',lambda host:[]):
        assert FeedFetcher(Session(body)).peek_latest(Response.url)==('New','2026-09-01T00:00:00')
        assert FeedFetcher(Session(body[:-15])).peek_latest(Response.url) is None
    with TemporaryDirectory(dir=ROOT.parent/'tmp',prefix='input-safety-') as folder:
        root=Path(folder); repo=LibraryRepository(Database(root/'library.db'))
        local=root/'local.wav'; local.write_bytes(b'local fixture')
        show=repo.add_show(local.as_uri(),'Local',source='local')
        class Fetcher:
            def fetch(self,*args): raise AssertionError('local source fetched as HTTP')
        for _ in range(3): RefreshService(repo,Fetcher()).refresh(show.id)
        assert not repo.get_show(show.id).suspended
        assert b'file:' not in export_opml(repo.list_shows())
        art=root/'shared.img'; art.write_bytes(b'art')
        repo.set_artwork_path(show.id,str(art))
        repo._remember_orphans([str(art)])
        repo.sweep_orphans()
        assert art.exists() and not repo.orphaned_files()
        repo._remember_orphans([str(root/f'orphan-{i}') for i in range(250)])
        assert len(repo.orphaned_files())==250
        repo.import_feed(show.id,FeedData('Local',episodes=(FeedEpisodeData('e','E',media_url=local.as_uri()),)))
        episode=repo.list_episodes(show.id)[0]; engine=Engine(); service=PlaybackService(repo,engine)
        service.load_episode(episode.id)
        engine.handler(EngineEvent('duration',100,engine.generation))
        engine.handler(EngineEvent('position',100,engine.generation))
        engine.handler(EngineEvent('eof',generation=engine.generation))
        # A refresh with missing duration must not erase observed playback length.
        repo.import_feed(show.id,FeedData('Local',episodes=(FeedEpisodeData('e','E',media_url=local.as_uri()),)))
        service.play()
        assert engine.loads[-1][1]==0
        service.shutdown()
        dated=repo.add_show('https://dates.invalid/feed','Dates')
        repo.import_feed(dated.id,FeedData('Dates',episodes=(
            FeedEpisodeData('d1','Daily',media_url='https://m.invalid/1',published_at='2026-09-01'),
            FeedEpisodeData('d2','Daily',media_url='https://m.invalid/2',published_at='2026-09-02'))))
        latest=repo.list_episodes(dated.id)[0]
        with repo.database.connect() as connection: connection.execute('DELETE FROM episodes WHERE id=?',(latest.id,))
        assert repo.get_show(dated.id).latest_episode_published_at=='2026-09-01'
    print('A04/A08/A09/A25/A29/A30/A33: PASS')


if __name__=='__main__': main()
