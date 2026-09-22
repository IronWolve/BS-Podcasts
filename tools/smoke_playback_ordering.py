"""Deterministic offline load/intent and persistence ordering regressions."""
import sys
import time
from pathlib import Path
from tempfile import TemporaryDirectory
from threading import Event, Thread
from unittest.mock import patch

ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT/'src'))
from bs_podcasts.data import Database
from bs_podcasts.data.repositories import LibraryRepository
from bs_podcasts.domain import FeedData,FeedEpisodeData
from bs_podcasts.playback.service import PlaybackService
from bs_podcasts.playback.engine import EngineCapabilities,EngineEvent


class Engine:
    capabilities=EngineCapabilities()
    def __init__(self): self.loads=[]; self.generation=0
    def set_event_handler(self,h): self.handler=h
    def prepare_load(self): self.generation+=1; return self.generation
    def load(self,source,start_position=0,autoplay=True):
        self.loads.append((source,start_position,autoplay))
        self.handler(EngineEvent('file_loaded',generation=self.generation))
        self.handler(EngineEvent('paused',not autoplay,self.generation))
    def set_volume(self,v): pass
    def set_speed(self,v): pass
    def set_silence_trim(self,v): pass
    def pause(self): pass
    def play(self): pass
    def unload(self): pass
    def shutdown(self): pass


def make(folder):
    repo=LibraryRepository(Database(Path(folder)/'library.db'))
    show=repo.add_show('https://f.invalid/feed','Show')
    repo.import_feed(show.id,FeedData('Show',episodes=(
        FeedEpisodeData('a','A',media_url='https://cdn.invalid/a.mp3',duration_seconds=100),
        FeedEpisodeData('b','B',media_url='https://podtrac.com/b.mp3',duration_seconds=100))))
    ids={e.external_id:e.id for e in repo.list_episodes(show.id)}
    engine=Engine()
    service=PlaybackService(repo,engine)
    return repo,engine,service,ids


def resolve_case(mode):
    with TemporaryDirectory(dir=ROOT.parent/'tmp',prefix='play-order-') as folder:
        repo,engine,service,ids=make(folder)
        entered,release,done=Event(),Event(),Event()
        def resolve(source):
            entered.set(); assert release.wait(3)
            return 'https://cdn.invalid/b.mp3'
        original=service._resolve_then_load
        def tracked(*args):
            try: original(*args)
            finally: done.set()
        service.load_episode(ids['a'])
        end=time.monotonic()+2
        while not engine.loads and time.monotonic()<end: time.sleep(.005)
        assert engine.loads
        service._resolve_final=resolve
        service._resolve_then_load=tracked
        old_generation=engine.generation
        service.load_episode(ids['b'])
        assert entered.wait(3)
        engine.handler(EngineEvent('position',80,old_generation))
        engine.handler(EngineEvent('eof',generation=old_generation))
        assert repo.get_episode(ids['b']).position_seconds==0
        assert not repo.get_episode(ids['b']).played
        if mode=='timeout': service._load_timed_out(service._load_token)
        else: service.pause(); service.seek(42)
        count=len(engine.loads)
        release.set(); assert done.wait(3)
        if mode=='timeout': assert len(engine.loads)==count
        else:
            assert engine.loads[-1][1:]==(42,False)
            engine.handler(EngineEvent('position',80,old_generation))
            assert service.snapshot.position==42
        service.shutdown()


def aba():
    with TemporaryDirectory(dir=ROOT.parent/'tmp',prefix='play-aba-') as folder:
        repo,engine,service,ids=make(folder)
        entered=[Event(),Event()]; release=[Event(),Event()]; done=[Event(),Event()]
        generations={}
        original=service._resolve_then_load
        def resolve(source):
            index=0 if not entered[0].is_set() else 1
            entered[index].set(); assert release[index].wait(3)
            return f'https://cdn.invalid/{index}.mp3'
        def tracked(source,position,autoplay,generation,tracker=True):
            index=generations.setdefault(generation,len(generations))
            try: original(source,position,autoplay,generation,tracker)
            finally: done[index].set()
        service._resolve_final=resolve; service._resolve_then_load=tracked
        service.load_episode(ids['b']); assert entered[0].wait(3)
        service.stop()
        service.load_episode(ids['b']); assert entered[1].wait(3)
        release[0].set(); assert done[0].wait(3)
        assert not engine.loads
        release[1].set(); assert done[1].wait(3)
        assert len(engine.loads)==1 and engine.loads[0][0].endswith('/1.mp3')
        service.shutdown()


def ordered_writes():
    with TemporaryDirectory(dir=ROOT.parent/'tmp',prefix='play-writes-') as folder:
        repo,engine,service,ids=make(folder)
        with patch('bs_podcasts.playback.service.is_web_url',lambda source:False):
            service.load_episode(ids['a'])
            entered,release=Event(),Event()
            original=repo.set_current_playback
            def delayed(episode_id,state,stamp=None):
                if episode_id==ids['a'] and state=='paused':
                    entered.set(); assert release.wait(3)
                return original(episode_id,state,stamp)
            repo.set_current_playback=delayed
            worker=Thread(target=engine.handler,args=(EngineEvent('paused',True,engine.generation),))
            worker.start(); assert entered.wait(3)
            service.load_episode(ids['b'])
            release.set(); worker.join(3); assert not worker.is_alive()
            assert repo.current_playback()[0]==ids['b']
            entered,release=Event(),Event()
            original_pos=repo.update_position
            def delayed_pos(episode_id,seconds,stamp=None):
                if seconds==50:
                    entered.set(); assert release.wait(3)
                return original_pos(episode_id,seconds,stamp)
            repo.update_position=delayed_pos
            worker=Thread(target=engine.handler,args=(EngineEvent('position',50,engine.generation),))
            worker.start(); assert entered.wait(3)
            engine.handler(EngineEvent('position',75,engine.generation))
            release.set(); worker.join(3); assert not worker.is_alive()
            assert repo.get_episode(ids['b']).position_seconds==75
            service.shutdown()


if __name__=='__main__':
    with patch('bs_podcasts.urlguard._addresses',lambda host:[]):
        resolve_case('pause'); resolve_case('timeout'); aba(); ordered_writes()
    print('A01/A02/A03: PASS pause/seek, timeout, same-URL retry, retired events, ordered state and progress')
