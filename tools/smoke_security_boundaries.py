"""Small offline security/privacy checks; no sockets, app launch or real data."""
import io
import json
import logging
import os
from pathlib import Path
import socket
import stat
import sys
from tempfile import TemporaryDirectory
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT/'src'))
sys.path.insert(0, str(ROOT/'packaging'))
from bs_podcasts.data import Database
from bs_podcasts.data.files import open_private
from bs_podcasts.logging_setup import _PrivateFormatter, _PrivateLog
from bs_podcasts.net import make_session, describe_network_error, _redirect_guard
from bs_podcasts.privacy import redact, feed_label
from bs_podcasts.safe_transport import connect_checked
from bs_podcasts.urlguard import UnsafeUrl, ensure_web_url, ensure_fetchable
from source_manifest import inventory
import requests


def rejects(call):
    try:
        call()
    except (ValueError, OSError):
        return
    raise AssertionError('Unsafe operation was accepted')


def network():
    client = make_session()
    with patch('requests.sessions.get_netrc_auth', side_effect=AssertionError('ambient credentials accessed')):
        prepared = client.prepare_request(requests.Request('GET', 'https://feed.invalid/'))
    assert 'Authorization' not in prepared.headers and not client.trust_env
    redirect = requests.Response()
    redirect.status_code = 302; redirect.url = 'https://feed.invalid/private'
    redirect.headers['Location'] = 'http://feed.invalid/other'
    redirect._content = b''
    redirect._content_consumed = True
    rejects(lambda: _redirect_guard(redirect))
    rejects(lambda: ensure_web_url('http://' + 'sample:fixture@feed.invalid/private'))
    for endpoint in ('http://[fd00:ec2::254]/', 'http://100.100.100.200/'):
        rejects(lambda endpoint=endpoint: ensure_fetchable(endpoint))
    for value in ('https://host.invalid:bad/', 'https://host.invalid:0/', 'https://host.invalid/x\r\nY:z',
                  'https://host.invalid\\@other.invalid/'):
        rejects(lambda value=value: ensure_web_url(value))
    good = (socket.AF_INET, socket.SOCK_STREAM, 6, '', ('93.184.216.34', 443))
    bad = (socket.AF_INET, socket.SOCK_STREAM, 6, '', ('127.0.0.1', 443))
    class Stream:
        def __init__(self): self.address = None
        def setsockopt(self,*args): pass
        def settimeout(self,value): pass
        def connect(self,value): self.address = value
        def close(self): pass
    stream = Stream()
    with patch('socket.getaddrinfo', return_value=[good]) as dns, patch('socket.socket', return_value=stream):
        assert connect_checked('feed.invalid',443,1) is stream
        assert dns.call_count == 1 and stream.address == good[4]
    for answers in ([bad], [good,bad], []):
        with patch('socket.getaddrinfo',return_value=answers), patch('socket.socket') as create:
            rejects(lambda: connect_checked('feed.invalid',443,1))
            assert not create.called
    pool = client.get_adapter('https://').get_connection_with_tls_context(prepared, True)
    assert pool.host == 'feed.invalid' and pool.ConnectionCls.__name__ == 'CheckedHTTPS'
    client.close()


def privacy(folder):
    secret = 'fixture-' + 'secret-value'
    url = 'https://' + 'sample:' + secret + '@feed.invalid/private/' + secret + '?token=' + secret
    assert secret not in redact(url)
    assert feed_label(url) == 'feed.invalid'
    assert secret not in describe_network_error(requests.ConnectionError('Failed: ' + url))
    assert secret not in redact('Max retries exceeded with url: /feed/'+secret+'?private_key='+secret)
    output = io.StringIO()
    handler = logging.StreamHandler(output); handler.setFormatter(_PrivateFormatter('%(message)s'))
    log = logging.Logger('security-fixture'); log.addHandler(handler)
    try:
        raise ValueError(url)
    except ValueError:
        log.exception('Request failed: %s', url)
    assert secret not in output.getvalue()
    file = folder/'private.log'
    handler = _PrivateLog(file,maxBytes=8,backupCount=1)
    handler.emit(logging.LogRecord('fixture',logging.INFO,'fixture',1,'long fixture message',(),None))
    handler.emit(logging.LogRecord('fixture',logging.INFO,'fixture',1,'second fixture message',(),None))
    handler.close()
    if os.name == 'posix':
        assert stat.S_IMODE(file.stat().st_mode) == 0o600
        assert stat.S_IMODE(Path(str(file)+'.1').stat().st_mode) == 0o600


def storage(folder):
    import sqlite3
    class FailedConnection:
        closed=False
        def execute(self,*args): raise sqlite3.OperationalError('fixture initialization failure')
        def close(self): self.closed=True
    failed=FailedConnection(); probe=Database.__new__(Database); probe.path=folder/'unused.db'
    with patch('bs_podcasts.data.database.sqlite3.connect',return_value=failed):
        try: probe._new_connection()
        except sqlite3.OperationalError: pass
        else: raise AssertionError('Connection failure was hidden')
    assert failed.closed
    target = folder/'private.txt'; target.write_bytes(b'keep me')
    link = folder/'partial'
    if os.name == 'posix':
        link.symlink_to(target)
        rejects(lambda: open_private(link))
        link.unlink(); os.link(target,link)
        rejects(lambda: open_private(link))
        link.unlink()
        assert target.read_bytes() == b'keep me'
    database = Database(folder/'library/library.db')
    try:
        backup = database.backup()
        if os.name == 'posix':
            for path in (database.path, backup, Path(str(database.path)+'-wal'), Path(str(database.path)+'-shm')):
                if path.exists(): assert stat.S_IMODE(path.stat().st_mode) == 0o600, path.name
    finally:
        database.close()


def builds(folder):
    source = folder/'source'; source.mkdir()
    for name in ('.codex/auth.json','.env','AGENTS.md','data/library.db'):
        item = source/name; item.parent.mkdir(parents=True,exist_ok=True); item.write_text('fixture')
        (source/'source-manifest.json').write_text(json.dumps({'files':['source-manifest.json',name],'runtime':{}}))
        rejects(lambda: inventory(source))


def main():
    network()
    from bs_podcasts.feeds.listening_fetch import parse_transcript
    from bs_podcasts.ui.models import plain_snippet
    from bs_podcasts.ui.widgets import safe_feed_html, linkify, MAX_SHOW_NOTE_CHARS
    malformed = '<' * 16000
    assert plain_snippet(malformed) == malformed[:240]
    assert parse_transcript(malformed.encode(), 'text/html')[0].text == malformed
    assert linkify('a'*100000) == 'a'*100000
    assert 'href=' in linkify('See https://example.invalid/item and example.invalid/more.')
    rendered = safe_feed_html('x'*(MAX_SHOW_NOTE_CHARS+1))
    assert 'Show notes shortened for display' in rendered
    assert len(rendered) < MAX_SHOW_NOTE_CHARS+200
    with TemporaryDirectory(dir=ROOT.parent/'tmp',prefix='security-') as temporary:
        folder = Path(temporary)
        privacy(folder); storage(folder); builds(folder)
    print('Security: PASS credential isolation, pinned DNS/blocked fallbacks, URL validation, diagnostic redaction, private storage, link safety and build exclusion')


if __name__ == '__main__': main()
