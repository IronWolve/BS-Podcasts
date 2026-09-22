"""Worker preparation and honest playback controls; no real engine or opener."""
import threading
from types import SimpleNamespace
from unittest.mock import patch
from smoke_gui_state import create_application, build, settle
from bs_podcasts.playback.engine import EngineCapabilities, EngineEvent
from bs_podcasts.playback.external import ExternalPlayerEngine
from bs_podcasts.playback.service import PlaybackService, PlaybackSnapshot, PlaybackState


class Engine:
    capabilities = EngineCapabilities()
    def __init__(self): self.loads=[]
    def set_event_handler(self, handler): self.handler=handler
    def load(self, source, start_position=0, autoplay=True): self.loads.append((source,start_position,autoplay))
    def set_volume(self, value): pass
    def set_speed(self, value): pass
    def set_silence_trim(self, value): pass
    def pause(self): pass
    def play(self): pass
    def shutdown(self): pass


def main():
    app=create_application(["gui-recovery"])
    window,repo,show,episodes,jobs=build(app)
    main_thread=threading.get_ident()
    captured=[]
    original=window.library.removal_preview
    def preview(identifier):
        assert threading.get_ident()!=main_thread
        return original(identifier)
    window.library.removal_preview=preview
    window._confirm_reset_library=lambda data: captured.append(data)
    window._reset_library()
    settle(app,window)
    assert captured and captured[0][0][0].id==show.id
    def integrity():
        assert threading.get_ident()!=main_thread
        return "ok"
    with patch.object(repo.database,"check_integrity",integrity):
        window._database_repair()
        settle(app,window)
    assert "Healthy" in window.settings_page.database_status.text()
    original=window.library.episode
    def episode(identifier):
        assert threading.get_ident()!=main_thread
        return original(identifier)
    with patch.object(window.library,"episode",episode):
        window._playback_changed(PlaybackSnapshot(episode_id=episodes[0].id,show_id=show.id,
            source=episodes[0].media_url,state=PlaybackState.PAUSED,title="Test"))
        settle(app,window)
    print("A14: PASS reset/integrity/playback metadata run off the GUI thread")

    external=ExternalPlayerEngine(command="true")
    external.set_event_handler(lambda event: None)
    with patch("bs_podcasts.playback.external.subprocess.Popen") as opener:
        external.load("https://media.invalid/e.mp3",autoplay=False)
        assert not opener.called
        external.play()
        assert opener.call_count==1
    window.playback=SimpleNamespace(engine=external)
    window._playback_changed(PlaybackSnapshot(source="https://media.invalid/e.mp3",state=PlaybackState.EXTERNAL))
    assert not window.player.slider.isEnabled() and not window.player.speed.isEnabled()
    window.playback=None
    settle(app,window)
    print("A31: PASS live fallback controls and no external launch for paused load")

    # URL screening has its own race/security checks; keep fake control checks synchronous.
    with patch('bs_podcasts.playback.service.is_web_url',lambda source:False):
        engine=Engine()
        service=PlaybackService(repo,engine)
        service._start_redirect_rescue=lambda: False
        for stream in (False,True):
            if stream: service.load_stream("https://media.invalid/preview.mp3","Preview")
            else: service.load_episode(episodes[0].id)
            for control in (service.play,service.play_pause):
                engine.handler(EngineEvent("error","Failed to open"))
                assert service.snapshot.state==PlaybackState.ERROR
                count=len(engine.loads)
                control()
                assert len(engine.loads)==count+1
        service.shutdown()
        print("A32: PASS Play and toggle reload failed episodes and previews")
    window.close()
    jobs.join(3)
    app.processEvents()


if __name__=="__main__": main()
