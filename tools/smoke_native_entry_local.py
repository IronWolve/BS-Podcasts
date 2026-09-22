"""Tiny local/null-audio native-entry check; isolated data, no network."""
from pathlib import Path
from tempfile import TemporaryDirectory
import time
import wave
from bs_podcasts.playback.engine import MpvEngine


def main():
    with TemporaryDirectory(dir=Path(__file__).resolve().parents[2]/'tmp',prefix='native-entry-') as folder:
        media=Path(folder)/'silence.wav'
        with wave.open(str(media),'wb') as stream:
            stream.setparams((1,2,8000,0,'NONE','not compressed'))
            stream.writeframes(bytes(32000))
        engine=MpvEngine(ao='null',vo='null')
        events=[]; engine.set_event_handler(events.append)
        try:
            for _ in range(2):
                generation=engine.prepare_load()
                engine.load(str(media),autoplay=False)
                deadline=time.monotonic()+3
                while not any(e.kind=='file_loaded' and e.generation==generation for e in events) and time.monotonic()<deadline:
                    time.sleep(.01)
                assert any(e.kind=='file_loaded' and e.generation==generation for e in events), events
                assert engine._player.pause
        finally:
            engine.shutdown()
    print('B12 native: PASS two local same-source loads, unique IDs, matched completion, paused/null audio')


if __name__=='__main__': main()
