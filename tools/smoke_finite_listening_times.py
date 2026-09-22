"""B08: finite ingestion/storage/rendering and safe repair of existing metadata."""
from pathlib import Path
from tempfile import TemporaryDirectory
from bs_podcasts.data import Database
from bs_podcasts.data.repositories import ListeningRepository
from bs_podcasts.feeds.listening_fetch import parse_chapters, parse_transcript
from bs_podcasts.ui.widgets import PlayerBar, ContextPanel
from smoke_playback_ordering import make


def main():
    assert not parse_chapters(b'{"chapters":[{"startTime":1e309}]}')
    assert parse_transcript(b'{"segments":[{"startTime":1e309,"text":"Keep text"}]}','application/json')[0].start_seconds is None
    for value in (float('inf'),float('nan'),-1,1e100,None):
        assert PlayerBar._time(value)=='0:00' and ContextPanel._time(value)=='0:00'
    root=Path(__file__).resolve().parents[2]
    with TemporaryDirectory(dir=root/'tmp',prefix='finite-times-') as folder:
        repo,engine,service,ids=make(folder); service.shutdown()
        listening=ListeningRepository(repo.database)
        listening.replace_chapters(ids['a'],[(float('inf'),None,'Bad'),(1,0,'Good')])
        assert len(listening.chapters(ids['a']))==1 and listening.chapters(ids['a'])[0].end_seconds is None
        listening.replace_transcript(ids['a'],[(float('inf'),-1,'Keep text')])
        assert listening.transcript(ids['a'])[0].start_seconds is None
        with repo.database.connect() as connection:
            connection.execute("INSERT INTO chapters(episode_id,chapter_index,start_seconds,title) VALUES (?,9,?,'Old bad')",(ids['a'],float('inf')))
            connection.execute('UPDATE transcript_segments SET start_seconds=?',(float('inf'),))
            connection.execute('DELETE FROM schema_migrations WHERE version=22')
        repo.database.close()
        reopened=Database(Path(folder)/'library.db'); listening=ListeningRepository(reopened)
        assert len(listening.chapters(ids['a']))==1
        assert listening.transcript(ids['a'])[0].text=='Keep text'
        assert listening.transcript(ids['a'])[0].start_seconds is None
        assert (Path(folder)/'library.db.pre-migration.bak').exists()
        reopened.close()
    print('B08: PASS bounded times, tolerant formatter, backed-up repair and preserved transcript text')


if __name__=='__main__': main()
