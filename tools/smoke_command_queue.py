"""B06: FIFO commands, supersession, Qt responsiveness and bounded finalization."""
from pathlib import Path
from threading import Event, get_ident
import time
from unittest.mock import patch
from PySide6.QtCore import QTimer
from smoke_gui_state import create_application,build,settle
from smoke_playback_ordering import Engine
from bs_podcasts.jobs.commands import CommandQueue
from bs_podcasts.playback.service import PlaybackService


def queue_lifecycle():
    queue=CommandQueue(); entered,release=Event(),Event(); order=[]
    def held():
        entered.set(); assert release.wait(2); order.append('active')
    queue.submit(held); assert entered.wait(2)
    unwanted=queue.submit(lambda:order.append('cancelled'))
    queue.submit(lambda:order.append('settings'),keep=True)
    queue.finish(lambda:order.append('shutdown'))
    assert unwanted.cancelled()
    release.set(); assert queue.join(2)==0
    assert order==['active','settings','shutdown'],order


def main():
    queue_lifecycle()
    app=create_application(['command-queue'])
    window,repo,show,episodes,jobs=build(app)
    engine=Engine(); playback=PlaybackService(repo,engine); window.playback=playback
    main_thread=get_ident(); original=repo.get_episode
    entered,release=Event(),Event()
    def held(identifier):
        assert get_ident()!=main_thread
        entered.set(); assert release.wait(2)
        return original(identifier)
    try:
        with patch('bs_podcasts.playback.service.is_web_url',return_value=False):
            with patch.object(repo,'get_episode',side_effect=held):
                window._play_episode(episodes[0].id,True); assert entered.wait(2)
                ticks=[]; QTimer.singleShot(0,lambda:ticks.append(True)); app.processEvents()
                assert ticks,'Qt stopped processing during a blocked playback read'
                window._play_pause(); release.set(); settle(app,window)
            assert not engine.loads,'Cancelled preparation started playback'
            window._play_episode(episodes[1].id,True); settle(app,window)
            assert playback.snapshot.episode_id==episodes[1].id
            # Settings mirrors do not regress when equal values recur (A/B/A).
            a=repo.stage_settings({'probe':'A'}); b=repo.stage_settings({'probe':'B'}); c=repo.stage_settings({'probe':'A'})
            repo.persist_settings({'probe':'A'},a); repo.persist_settings({'probe':'B'},b)
            assert repo.get_setting('probe')=='A'
            repo.persist_settings({'probe':'A'},c)
            entered.clear(); release.clear()
            with patch.object(repo,'get_episode',side_effect=held):
                window._play_episode(episodes[0].id,True); assert entered.wait(2)
                started=time.monotonic(); window.close()
                assert time.monotonic()-started<.25,'Close waited for the command worker'
                release.set(); assert window.commands.join(2)==0
            assert playback._dead
            assert repo.get_setting('ui.geometry')
        print('B06: PASS Qt responsiveness, cancelled load, normal load, settings ABA, queued settings/finalizer and nonblocking close')
    finally:
        release.set()
        if not window._closed: window.close()
        window.commands.join(2); jobs.join(2); app.processEvents(); repo.database.close()


if __name__=='__main__': main()
