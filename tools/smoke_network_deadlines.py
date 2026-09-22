"""No-network checks: late headers/body, byte limits, DNS and cancellation."""
import sys
import time
import threading
from pathlib import Path
from unittest.mock import patch

ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT/'src'))
import requests
from requests.adapters import BaseAdapter
from bs_podcasts.net import make_session
from bs_podcasts.netlimits import Deadline,bounded_call,NetworkDeadline,DNS


class Raw:
    def __init__(self,body,hold=None): self.body=body; self.hold=hold; self.closed=False
    def stream(self,amount,decode_content=True):
        if self.hold is not None: assert self.hold.wait(2)
        for start in range(0,len(self.body),amount): yield self.body[start:start+amount]
    def close(self): self.closed=True
    def release_conn(self): pass


class Adapter(BaseAdapter):
    def __init__(self,body=b'hello',headers_hold=None,body_hold=None):
        self.body=body; self.headers_hold=headers_hold; self.body_hold=body_hold; self.calls=0
    def send(self,request,**kwargs):
        self.calls+=1
        if self.headers_hold is not None: assert self.headers_hold.wait(2)
        response=requests.Response();response.status_code=200;response.url=request.url;response.request=request
        response.raw=Raw(self.body,self.body_hold)
        return response
    def close(self): pass


def session(adapter,**kwargs):
    client=make_session(**kwargs);client.mount('https://',adapter);return client


def expect_timeout(work):
    start=time.monotonic()
    try: work()
    except (requests.Timeout,NetworkDeadline): pass
    else: raise AssertionError('deadline/limit not enforced')
    assert time.monotonic()-start<.8


def main():
    initial=set(threading.enumerate())
    release=threading.Event()
    client=session(Adapter(headers_hold=release),total_timeout=.1)
    expect_timeout(lambda:client.get('https://test.invalid'))
    release.set()
    release=threading.Event()
    client=session(Adapter(body_hold=release),total_timeout=.1)
    response=client.get('https://test.invalid',stream=True)
    expect_timeout(lambda:list(response.iter_content(4096)))
    release.set()
    client=session(Adapter(b'12345678'),max_response_bytes=4)
    expect_timeout(lambda:client.get('https://test.invalid'))
    adapter=Adapter();cancel=threading.Event();cancel.set()
    expect_timeout(lambda:session(adapter).get('https://test.invalid',cancel_event=cancel))
    assert adapter.calls==0
    client=session(Adapter(b'12345678'),max_response_bytes=None)
    assert client.get('https://test.invalid').content==b'12345678'
    release=threading.Event()
    expect_timeout(lambda:bounded_call(lambda:release.wait(2),Deadline(.1),gate=DNS))
    release.set()
    for thread in set(threading.enumerate())-initial:
        if thread.name.startswith('bs-http'):
            thread.join(1)
            assert not thread.is_alive(),thread.name
    print('A24: PASS bounded headers/body/DNS, cancellation, document cap and uncapped media mode')


if __name__=='__main__':main()
