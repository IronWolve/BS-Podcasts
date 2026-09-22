"""B01/B13: explicit Play intent and paused-time exclusion. No network/audio."""
from pathlib import Path
from tempfile import TemporaryDirectory
from threading import Event
from unittest.mock import patch
from bs_podcasts.playback.service import PlaybackService
from bs_podcasts.playback.engine import EngineEvent
from bs_podcasts.data.repositories import ListeningRepository
from smoke_playback_ordering import make


def main():
    root=Path(__file__).resolve().parents[2]
    with TemporaryDirectory(dir=root/'tmp',prefix='play-intent-') as folder:
        repo,engine,service,ids=make(folder)
        entered,release,done=Event(),Event(),Event()
        original=service._resolve_then_load
        def resolve(url):
            entered.set(); assert release.wait(2); return 'https://cdn.invalid/test.mp3'
        def tracked(*args):
            try: original(*args)
            finally: done.set()
        service._resolve_final=resolve; service._resolve_then_load=tracked
        with patch('bs_podcasts.urlguard._addresses',return_value=[]):
            service.load_episode(ids['b']); assert entered.wait(2)
            service.pause(); service.play(); service.seek(12)
            release.set(); assert done.wait(2)
        assert engine.loads[-1][1:]==(12,True)
        service.shutdown()
        listening=ListeningRepository(repo.database)
        service=PlaybackService(repo,engine,listening=listening)
        clock=[10.0]
        with patch('bs_podcasts.playback.service.is_web_url',return_value=False), patch('bs_podcasts.playback.service.time.monotonic',side_effect=lambda:clock[0]):
            service.load_episode(ids['a'])
            clock[0]=10.1; engine.handler(EngineEvent('position',.1,engine.generation))
            engine.handler(EngineEvent('paused',True,engine.generation))
            before=listening.statistics()['listened_seconds']
            clock[0]=110; engine.handler(EngineEvent('paused',False,engine.generation))
            clock[0]=110.1; engine.handler(EngineEvent('position',.2,engine.generation))
            engine.handler(EngineEvent('paused',True,engine.generation))
            assert abs(listening.statistics()['listened_seconds']-before-.1)<.001
        service.shutdown(); repo.database.close()
    print('B01/B13: PASS explicit Play/seek during resolution and paused-time exclusion')


if __name__=='__main__': main()
