"""Guarded native streaming: fake HTTP, real tiny paused/null-audio decoding."""
import io
from pathlib import Path
import sys
from threading import Event, Thread
import time
from unittest.mock import patch
import wave

ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT/'src'))
from bs_podcasts.playback.http_stream import HttpStream
from bs_podcasts.playback.engine import MpvEngine


class Response:
    def __init__(self,body,offset,total,bad=False):
        self.body=body; self.status_code=206 if offset else 200; self.closed=False
        self.headers={'Content-Length':str(len(body)),'ETag':'"fixture"'}
        if offset:
            self.headers['Content-Range']=f'bytes {offset+int(bad)}-{total-1}/{total}'
    def raise_for_status(self): pass
    def iter_content(self,size):
        for start in range(0,len(self.body),size):
            yield self.body[start:start+size]
    def close(self): self.closed=True


class Session:
    def __init__(self,body): self.body=body; self.bad=False; self.closed=False; self.responses=[]
    def get(self,url,headers,**kwargs):
        assert url.startswith('https://fixture.invalid/')
        assert headers['Accept-Encoding']=='identity'
        offset=int(headers.get('Range','bytes=0-')[6:-1])
        response=Response(self.body[offset:],offset,len(self.body),self.bad)
        self.responses.append(response)
        return response
    def close(self): self.closed=True


def ranges(body):
    session=Session(body); stream=HttpStream('https://fixture.invalid/a.wav',session)
    assert stream.read(12)==body[:12]
    assert stream.seek(100)==100 and stream.read(20)==body[100:120]
    session.bad=True
    assert stream.seek(200)==-1 and stream.read(10)==body[120:130]
    assert stream.seek(len(body))==len(body) and stream.read(10)==b''
    session.bad=False
    assert stream.seek(0)==0 and stream.read(4)==b'RIFF'
    stream.cancel(); assert stream.read(10)==b''
    stream.close(); assert session.closed and all(r.closed for r in session.responses)
    errors=[]
    session=Session(body)
    stream=HttpStream('https://fixture.invalid/a.wav',session,on_error=errors.append)
    stream.size=len(body)+1
    try:
        while stream.read(65536): pass
    except OSError:
        pass
    else:
        raise AssertionError('Truncated stream accepted as successful EOF')
    finally:
        stream.close()
    assert errors
    class Capped(Session):
        def get(self,url,headers,**kwargs):
            offset=int(headers.get('Range','bytes=0-')[6:-1])
            end=min(len(self.body),offset+4000)
            response=Response(self.body[offset:end],offset,len(self.body))
            response.status_code=206
            response.headers['Content-Range']=f'bytes {offset}-{end-1}/{len(self.body)}'
            self.responses.append(response)
            return response
    session=Capped(body); stream=HttpStream('https://fixture.invalid/a.wav',session)
    received=[]
    while piece:=stream.read(65536): received.append(piece)
    assert b''.join(received)==body and len(session.responses)>1
    stream.close()


def cancel_open():
    from threading import Lock
    entered=Event(); failures=[]
    class Held:
        def get(self,*args,**kwargs):
            entered.set()
            assert kwargs['cancel_event'].wait(2)
            raise ValueError('cancelled fixture')
        def close(self): pass
    engine=MpvEngine.__new__(MpvEngine)
    engine._http_sources={'bshttp://test':'https://fixture.invalid/a.wav'}
    engine._http_streams=[]; engine._http_lock=Lock(); engine._http_failure=None
    def opened():
        try: engine._open_http('bshttp://test')
        except ValueError: failures.append(True)
    with patch('bs_podcasts.playback.http_stream.make_session',return_value=Held()):
        worker=Thread(target=opened); worker.start(); assert entered.wait(1)
        engine._cancel_http(); worker.join(2)
        assert not worker.is_alive() and failures


def native(body):
    sessions=[]
    def session(**kwargs):
        value=Session(body); sessions.append(value); return value
    with patch('bs_podcasts.playback.http_stream.make_session',side_effect=session):
        engine=MpvEngine(ao='null',vo='null'); events=[]; engine.set_event_handler(events.append)
        try:
            for position in (0,.5):
                generation=engine.prepare_load()
                engine.load('https://fixture.invalid/a.wav',start_position=position,autoplay=False)
                deadline=time.monotonic()+3
                while time.monotonic()<deadline:
                    if any(e.kind=='file_loaded' and e.generation==generation for e in events): break
                    time.sleep(.01)
                assert any(e.kind=='file_loaded' and e.generation==generation for e in events),events
                assert engine._player.playlist[0]['filename'].startswith('bshttp://')
                assert engine._player.pause
                assert abs(engine._player.time_pos-position)<.05
        finally:
            engine.shutdown()
    assert sessions and all(s.closed for s in sessions)


def main():
    data=io.BytesIO()
    with wave.open(data,'wb') as stream:
        stream.setparams((1,2,8000,0,'NONE','not compressed')); stream.writeframes(bytes(32000))
    body=data.getvalue()
    ranges(body); cancel_open(); native(body)
    print('Guarded stream: PASS bytes/ranges, bad-range rejection, seek/EOF, header cancellation, native opaque URI, paused/null-audio decode and cleanup')


if __name__=='__main__': main()
