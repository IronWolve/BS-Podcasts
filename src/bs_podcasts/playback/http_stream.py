"""Seekable native-player input using the application's guarded HTTP transport."""
import logging
import re
import time
from threading import Event, Lock

from ..net import make_session
from ..netlimits import abort_response
from ..privacy import redact
from ..urlguard import ensure_web_url

MAX_STREAM_BYTES = 32 * 1024**3
# Waits before each reconnect after a server drops a playing stream. The
# native player keeps playing its buffer meanwhile, so a short outage is not
# heard. A stream that delivers RESUME_PROGRESS bytes after reconnecting
# earns a fresh set of attempts; one that keeps failing gives up.
RESUME_DELAYS = (0.5, 1.0, 2.0, 4.0, 8.0)
RESUME_PROGRESS = 256 * 1024
# A host that answers 429 or 5xx with Retry-After is waited out when the pause
# is this short (an open must still finish inside the player's 25 s load
# limit); a longer one is reported with its length.
COOLDOWN_WAIT_CAP = 15.0

logger = logging.getLogger("bs_podcasts")


class _Cancelled(Exception):
    pass


def _describe(exc) -> str:
    if exc is None:
        return "connection closed early"
    return f"{type(exc).__name__}: {redact(exc)[:160]}"


def _permanent(exc) -> bool:
    """Failures a reconnect cannot fix: a changed, unsafe or refused response."""
    if isinstance(exc, ValueError):
        # Integrity and URL checks: wrong range, changed file, unsafe redirect.
        return True
    from requests.exceptions import HTTPError, RetryError
    if isinstance(exc, RetryError):
        # A short host pause is waited out; one that outlasts the wait is not
        # something a reconnect a few seconds later can fix.
        wait = getattr(exc, 'retry_after', None)
        return wait is None or wait > COOLDOWN_WAIT_CAP
    if isinstance(exc, HTTPError):
        status = getattr(getattr(exc, "response", None), "status_code", None) or 0
        return status < 500 and status not in {408, 425, 429}
    return False


class HttpStream:
    def __init__(self, url, session=None, defer_open=False, on_error=None, on_recovered=None):
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
        self._resume_failures = 0
        self._resumed_at = 0
        self._failed = False
        self._on_error = on_error or (lambda message: None)
        # A reported failure is withdrawn once a later open succeeds (a seek
        # after the error), so a normal end is not reported as that failure.
        self._on_recovered = on_recovered or (lambda: None)
        if not defer_open:
            self.open()

    def open(self):
        try:
            self._open(0)
        except Exception as exc:
            self.close()
            self._on_error(redact(exc))
            raise ValueError(redact(exc)) from None

    def _get(self, headers, remaining):
        """Send the request. A host that asked for a short pause (Retry-After)
        is waited out once, so a reconnect, a seek or a reopen is not refused
        for a cooldown that ends within seconds; a long one is raised with its
        length."""
        from requests.exceptions import RetryError
        for attempt in range(2):
            try:
                return self.session.get(self.url, headers=headers, stream=True, timeout=(8,30),
                                        cancel_event=self.cancelled, total_timeout=remaining)
            except RetryError as exc:
                wait = getattr(exc, 'retry_after', None)
                if attempt or wait is None or wait > COOLDOWN_WAIT_CAP or self.cancelled.wait(wait):
                    raise

    def _open(self, offset):
        remaining = self._expires - time.monotonic()
        if remaining <= 0:
            raise ValueError('The audio stream exceeded its time limit.')
        headers = {'Accept-Encoding': 'identity'}
        if offset:
            headers['Range'] = f'bytes={offset}-'
        if self._validator:
            headers['If-Range'] = self._validator
        response = self._get(headers, remaining)
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
            previous_chunks, self._chunks = self._chunks, iter(response.iter_content(64*1024))
            self._eof = False
            # Free the old body reader now: playback has only two reader
            # slots, and a reconnect must not wait for garbage collection.
            close = getattr(previous_chunks, 'close', None)
            if close is not None:
                try:
                    close()
                except ValueError:
                    pass
            if self._failed:
                self._failed = False
                self._on_recovered()
        except BaseException:
            abort_response(response)
            raise

    def read(self, size):
        if size <= 0 or self.cancelled.is_set() or self._eof:
            return b''
        while not self._buffer:
            try:
                self._buffer = next(self._chunks)
                continue
            except StopIteration:
                if self.cancelled.is_set():
                    return b''
                if self.size is None or self.position >= self.size:
                    # Unknown length ends where the server stops; a known
                    # length is complete. Overruns are refused below.
                    self._eof = True
                    return b''
                failure = None
            except Exception as exc:
                if self.cancelled.is_set():
                    return b''
                failure = exc
            try:
                self._resume(failure)
            except _Cancelled:
                return b''
        size = min(max(0,size),64*1024)
        data, self._buffer = self._buffer[:size], self._buffer[size:]
        self.position += len(data)
        if self.position > MAX_STREAM_BYTES or (self.size is not None and self.position > self.size):
            self._fail('The audio stream exceeded its declared size.')
        if self._part_end is not None and self.position > self._part_end:
            self._fail('The audio response exceeded its byte range.')
        if self._resume_failures and self.position - self._resumed_at >= RESUME_PROGRESS:
            self._resume_failures = 0
        return data

    def _resume(self, failure):
        """Reopen at the current byte after a range ends or the connection drops.

        The same Range/If-Range checks as a seek apply, so a changed file is
        refused rather than spliced. Raises OSError once retrying is futile."""
        if failure is None and self._part_end is not None and self.position == self._part_end:
            # Some CDNs cap each valid range response. Continue at the verified
            # boundary without treating it as the episode EOF or a failure.
            try:
                self._open(self.position)
                return
            except Exception as exc:
                if self.cancelled.is_set():
                    raise _Cancelled from None
                failure = exc
        total = self.size if self.size is not None else "unknown"
        while True:
            if failure is not None and _permanent(failure):
                self._give_up(failure)
            if self._resume_failures >= len(RESUME_DELAYS):
                self._give_up(failure)
            delay = RESUME_DELAYS[self._resume_failures]
            self._resume_failures += 1
            logger.warning("Audio stream dropped at byte %s of %s (%s); reconnecting in %.1fs, attempt %d of %d",
                           self.position, total, _describe(failure), delay,
                           self._resume_failures, len(RESUME_DELAYS))
            if self.cancelled.wait(delay):
                raise _Cancelled
            try:
                self._open(self.position)
            except Exception as exc:
                if self.cancelled.is_set():
                    raise _Cancelled from None
                failure = exc
                continue
            self._resumed_at = self.position
            logger.info("Audio stream reconnected at byte %s of %s after %d attempt(s)",
                        self.position, total, self._resume_failures)
            return

    def _give_up(self, failure):
        logger.error("Audio stream could not be reconnected at byte %s of %s after %d attempt(s): %s",
                     self.position, self.size if self.size is not None else "unknown",
                     self._resume_failures, _describe(failure))
        if failure is None:
            self._fail('The audio stream ended before its declared length.')
        if _permanent(failure):
            self._fail(redact(failure))
        self._fail('The audio connection dropped and could not be reconnected.')

    def _fail(self, message):
        self._eof = True
        self._failed = True
        self._on_error(message)
        raise OSError(message) from None

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
        except Exception as exc:
            # libmpv treats a negative offset as an unsupported/failed seek.
            # Keep the previous response usable rather than mixing wrong bytes.
            if not self.cancelled.is_set():
                logger.warning("Audio stream seek to byte %s failed: %s", offset, _describe(exc))
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
