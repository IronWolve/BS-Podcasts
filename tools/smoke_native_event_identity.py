"""B12: native entry provenance, not a fake service-supplied generation."""
from concurrent.futures import Future
from pathlib import Path
from tempfile import TemporaryDirectory
from threading import RLock
from types import SimpleNamespace as NS
from unittest.mock import patch
import bs_podcasts.playback.engine as adapter
from bs_podcasts.playback.service import PlaybackService
from smoke_playback_ordering import make


class Player:
    def __init__(self): self.serial=0; self.playlist=[]
    def command_async(self,*args):
        result=Future(); result.set_result(None); return result
    def loadfile(self,source,mode,**kwargs):
        assert kwargs['pause']=='yes'
        self.serial+=1; self.playlist=[{'id':self.serial,'filename':source}]
    def seek(self,*args): pass
    def terminate(self): pass


def main():
    root=Path(__file__).resolve().parents[2]
    with TemporaryDirectory(dir=root/'tmp',prefix='native-identity-') as folder:
        repo,_,unused,ids=make(folder); unused.shutdown()
        engine=adapter.MpvEngine.__new__(adapter.MpvEngine)
        engine.__dict__.update(_player=Player(),_handler=lambda e:None,_dead=False,
            _activated=False,_state_lock=RLock(),_generation=0,_expected_entry_id=None,
            _event_entry_id=None,_resolving=False,_loading=False,_pending_position=0,
            _pending_autoplay=False,_last_error='')
        service=PlaybackService(repo,engine)
        def event(kind,entry=None,reason=0):
            engine._mpv_event(NS(event_id=NS(value=kind),data=NS(
                playlist_entry_id=entry,reason=reason,EOF=0,ERROR=4,error=-1)))
        try:
            with patch.object(adapter,'mpv',NS(MpvEventID=NS(START_FILE=6,FILE_LOADED=8,END_FILE=7,SHUTDOWN=1))), patch('bs_podcasts.playback.service.is_web_url',return_value=False):
                service.load_episode(ids['a'],autoplay=False); first=engine._expected_entry_id
                event(6,first)
                service.load_episode(ids['b'],autoplay=False); second=engine._expected_entry_id
                event(8)  # A's queued completion, before B's START_FILE.
                engine._position_changed('time-pos',42)
                event(7,first,4)  # outgoing error must not rescue/fail B
                assert engine._loading and repo.get_episode(ids['b']).position_seconds==0
                event(6,second); event(8)
                engine._position_changed('time-pos',17)
                assert repo.get_episode(ids['b']).position_seconds==17
                service.load_episode(ids['b'],autoplay=False); third=engine._expected_entry_id
                assert third!=second
                event(8); event(7,second,0)
                assert engine._loading and not repo.get_episode(ids['b']).played
                event(6,third); event(8)
                service.stop(); engine._position_changed('time-pos',81)
                assert service.snapshot.episode_id is None
        finally:
            service.shutdown(); repo.database.close()
    print('B12: PASS outgoing completion/error/position, same-URL replacement, current entry and stop')


if __name__=='__main__': main()
