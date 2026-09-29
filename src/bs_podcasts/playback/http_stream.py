"""Seekable native-player input using the application's guarded HTTP transport."""
import re
import time
from threading import Event, Lock

from ..net import make_session
from ..netlimits import abort_response
from ..privacy import redact
from ..urlguard import ensure_web_url

MAX_STREAM_BYTES = 32 * 1024**3


class HttpStream:
    def __init__(self, url, session=None, defer_open=False, on_error=None):
        self.url = ensure_web_url(url)
        self.session = session or make_session(max_response_bytes=None, total_timeout=24*3600, foreground=True)
        self.cancelled = Event()
        self._lock = Lock()
        self._response = None
        self._chunks = iter(())
        self._buffer = b''
        self.position = 0
        self.size = None
        self._validator = ''
        self._eof = False
        self._part_end = None
        self._expires = time.monotonic() + 24*3600
        self._on_error = on_error or (lambda message: None)
        if not defer_open:
            self.open()

    def open(self):
        try:
            self._open(0)
        except Exception as exc:
            self.close()
            self._on_error(redact(exc))
            raise ValueError(redact(exc)) from None

    def _open(self, offset):
        remaining = self._expires - time.monotonic()
        if remaining <= 0:
            raise ValueError('The audio stream exceeded its time limit.')
        headers = {'Accept-Encoding': 'identity'}
        if offset:
            headers['Range'] = f'bytes={offset}-'
        if self._validator:
            headers['If-Range'] = self._validator
        response = self.session.get(self.url, headers=headers, stream=True, timeout=(8,30),
                                    cancel_event=self.cancelled, total_timeout=remaining)
        try:
            response.raise_for_status()
            if response.headers.get('Content-Encoding','identity').lower() not in {'','identity'}:
                raise ValueError('The audio server returned encoded bytes that cannot be sought safely.')
            total = None
            part_end = None
            if response.status_code == 206:
                match = re.fullmatch(r'bytes (\d+)-(\d+)/(\d+)',response.headers.get('Content-Range',''))
                if match is None:
                    raise ValueError('The audio server returned an invalid byte range.')
                start, end, total = map(int,match.groups())
                if start != offset or end < start or end >= total:
                    raise ValueError('The audio server returned the wrong byte range.')
                part_end = end + 1
            elif response.status_code != 200 or offset:
                raise ValueError('This audio server does not support safe range seeking.')
            elif response.headers.get('Content-Length'):
                total = int(response.headers['Content-Length'])
                part_end = total
            if total is not None and not 0 <= total <= MAX_STREAM_BYTES:
                raise ValueError('The audio stream exceeds the supported size limit.')
            if self.size is not None and total is not None and total != self.size:
                raise ValueError('The audio file changed during playback; reload the episode.')
            validator = response.headers.get('ETag','')
            if validator.startswith('W/') or not validator:
                validator = response.headers.get('Last-Modified','')
            if self._validator and validator and self._validator != validator:
                raise ValueError('The audio file changed during playback; reload the episode.')
            with self._lock:
                if self.cancelled.is_set():
                    raise ValueError('Playback was cancelled.')
                previous, self._response = self._response, response
            if previous is not None:
                abort_response(previous)
            self._validator = validator
            self.size = total if total is not None else self.size
            self.position = offset
            self._part_end = part_end
            self._buffer = b''
            self._chunks = iter(response.iter_content(64*1024))
            self._eof = False
        except BaseException:
            abort_response(response)
            raise

    def read(self, size):
        if size <= 0 or self.cancelled.is_set() or self._eof:
            return b''
        try:
            while not self._buffer:
                self._buffer = next(self._chunks)
            size = min(max(0,size),64*1024)
            data, self._buffer = self._buffer[:size], self._buffer[size:]
            self.position += len(data)
            if self.position > MAX_STREAM_BYTES or (self.size is not None and self.position > self.size):
                raise ValueError('The audio stream exceeded its declared size.')
            if self._part_end is not None and self.position > self._part_end:
                raise ValueError('The audio response exceeded its byte range.')
            return data
        except StopIteration:
            if self.cancelled.is_set():
                return b''
            if (self.size is not None and self._part_end is not None
                    and self.position == self._part_end and self.position < self.size):
                try:
                    # Some CDNs cap each valid range response. Continue at the
                    # verified boundary without treating it as the episode EOF.
                    self._open(self.position)
                    return self.read(size)
                except Exception as exc:
                    self._on_error(redact(exc))
                    raise OSError(redact(exc)) from None
            self._eof = True
            if self.size is not None and self.position != self.size:
                message = 'The audio stream ended before its declared length.'
                self._on_error(message)
                raise OSError(message) from None
            return b''
        except Exception as exc:
            if self.cancelled.is_set():
                return b''
            self._on_error(redact(exc))
            raise OSError(redact(exc)) from None

    def seek(self, offset):
        if self.cancelled.is_set() or offset < 0 or offset > MAX_STREAM_BYTES:
            return -1
        if offset == self.position:
            return offset
        if self.size is not None and offset > self.size:
            return -1
        if self.size is not None and offset == self.size:
            self.position = offset
            self._buffer = b''
            self._eof = True
            return offset
        try:
            self._open(offset)
            return offset
        except Exception:
            # libmpv treats a negative offset as an unsupported/failed seek.
            # Keep the previous response usable rather than mixing wrong bytes.
            return -1

    def cancel(self):
        self.cancelled.set()
        with self._lock:
            response = self._response
        if response is not None:
            abort_response(response)

    def close(self):
        self.cancel()
        chunks, self._chunks = self._chunks, iter(())
        try:
            close = getattr(chunks, 'close', None)
            if close is not None:
                close()
        except ValueError:
            pass  # A cancelled read is still unwinding on its native thread.
        self._buffer = b''
        self.session.close()
