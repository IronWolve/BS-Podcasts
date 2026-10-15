"""Seekable native-player input using the application's guarded HTTP transport."""
import logging
import re
import time
import warnings
from collections import deque
from threading import Event, Lock, Thread

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
# Waits before each retry of a seek whose range request failed in transit.
SEEK_RETRY_DELAYS = (0.25, 0.5, 1.0)
# A rolling 16 KB must arrive within this long, about 270 B/s, the floor the
# download path uses. A connection that trickles slower never trips the read
# timeout, so it is cut and reopened at the current byte.
STALL_SECONDS = 60.0
STALL_MINIMUM_BYTES = 16 * 1024
STALL_IDLE_SECONDS = 1.0

logger = logging.getLogger("bs_podcasts")


class _StreamFailure(OSError):
    """A designed end of a stream. on_error has already received its message."""


# python-mpv turns any exception raised by a stream callback into a RuntimeWarning
# whose text is the full traceback, local paths included, printed to stderr. Every
# _StreamFailure is reported by on_error (and the reader's log line) first, so only
# that warning is silenced, matched by the exception class in its traceback.
warnings.filterwarnings(
    "ignore", category=RuntimeWarning,
    message=r"Unhandled exception on python-mpv event loop: [\s\S]*"
            + re.escape(f"{__name__}._StreamFailure") + ": ")


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
        self._delimited = False
        self._expires = time.monotonic() + 24*3600
        self._resume_failures = 0
        self._resumed_at = 0
        self._failed = False
        # Wait identities prevent the watchdog from aborting a read that just
        # completed. Byte samples support a rolling minimum-rate check.
        self._watch_lock = Lock()
        self._waiting = None
        self._stalled = None
        self._watch_active_since = None
        self._watch_last_activity = None
        self._watch_bytes = deque()
        self._watching = False
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
            with self._watch_lock:
                self._waiting = None
                self._stalled = None
                self._watch_active_since = None
                self._watch_last_activity = None
                self._watch_bytes.clear()
            if previous is not None:
                abort_response(previous)
            # A reply that names no validator leaves the earlier one in place.
            self._validator = validator or self._validator
            self.size = total if total is not None else self.size
            self.position = offset
            self._part_end = part_end
            # Neither a length nor chunk framing: the body ends where the
            # connection does, so a cut-off one cannot be told from a whole one.
            self._delimited = total is None and 'chunked' not in response.headers.get('Transfer-Encoding', '').lower()
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
            if not self._watching:
                self._watching = True
                Thread(target=self._watch, name='bs-stream-watch', daemon=True).start()
        except BaseException:
            abort_response(response)
            raise

    def read(self, size):
        if size <= 0 or self.cancelled.is_set() or self._eof:
            return b''
        while not self._buffer:
            response = self._response
            now = time.monotonic()
            with self._watch_lock:
                if (self._watch_last_activity is None
                        or now - self._watch_last_activity > STALL_IDLE_SECONDS):
                    self._watch_active_since = now
                    self._watch_bytes.clear()
                self._watch_last_activity = now
                completed = Event()
                waiting = (self._watch_active_since, response, object(), completed)
                self._waiting = waiting
            try:
                chunk = next(self._chunks)
                completed.set()
                self._buffer = chunk
                if chunk:
                    arrived = time.monotonic()
                    with self._watch_lock:
                        cutoff = arrived - STALL_SECONDS
                        while self._watch_bytes and self._watch_bytes[0][0] < cutoff:
                            self._watch_bytes.popleft()
                        if self._watch_bytes and self._watch_bytes[-1][0] >= arrived - 1.0:
                            start, amount = self._watch_bytes[-1]
                            self._watch_bytes[-1] = (start, amount + len(chunk))
                        else:
                            self._watch_bytes.append((arrived, len(chunk)))
                        self._watch_last_activity = arrived
                continue
            except StopIteration:
                completed.set()
                if self.cancelled.is_set():
                    return b''
                if not self._was_cut(response) and (self.size is None or self.position >= self.size):
                    # Unknown length ends where the server stops; a known
                    # length is complete. Overruns are refused below.
                    if self._more_follows():
                        continue
                    self._eof = True
                    if self._failed:
                        # A failed seek left this response playing to its
                        # end, which is a normal end, not that failure.
                        self._failed = False
                        self._on_recovered()
                    return b''
                failure = None
            except Exception as exc:
                completed.set()
                if self.cancelled.is_set():
                    return b''
                failure = exc
            finally:
                with self._watch_lock:
                    if self._waiting is waiting:
                        self._waiting = None
            if self._was_cut(response):
                # The watchdog cut this connection off; its end is not an EOF.
                with self._watch_lock:
                    if self._stalled is response:
                        self._stalled = None
                failure = TimeoutError(f'no audio data for {STALL_SECONDS:.0f} s')
            try:
                self._resume(failure)
            except _Cancelled:
                return b''
        size = min(max(0,size),64*1024)
        data, self._buffer = self._buffer[:size], self._buffer[size:]
        self.position += len(data)
        if self.position > MAX_STREAM_BYTES or (self.size is not None and self.position > self.size):
            self._overrun('The audio stream exceeded its declared size.')
        if self._part_end is not None and self.position > self._part_end:
            self._overrun('The audio response exceeded its byte range.')
        if self._resume_failures and self.position - self._resumed_at >= RESUME_PROGRESS:
            self._resume_failures = 0
        return data

    def _was_cut(self, response):
        with self._watch_lock:
            return response is not None and self._stalled is response

    def _watch(self):
        """Cut a stream below its minimum byte rate while the player reads.

        The rolling window catches slow trickles as well as a fully quiet
        socket. Idle time outside read() is discarded, such as when mpv has
        enough buffered audio or is paused.
        """
        while not self.cancelled.wait(min(1.0, STALL_SECONDS / 4)):
            now = time.monotonic()
            with self._watch_lock:
                waiting = self._waiting
                if waiting is None:
                    if (self._watch_last_activity is None
                            or now - self._watch_last_activity > STALL_IDLE_SECONDS):
                        self._watch_active_since = None
                        self._watch_last_activity = None
                        self._watch_bytes.clear()
                    continue
                cutoff = now - STALL_SECONDS
                while self._watch_bytes and self._watch_bytes[0][0] < cutoff:
                    self._watch_bytes.popleft()
                received = sum(size for _at, size in self._watch_bytes)
                if (now - waiting[0] >= STALL_SECONDS
                        and received < STALL_MINIMUM_BYTES
                        and not waiting[3].is_set()
                        and self._waiting is waiting):
                    # Keep the identity check, state transition, and socket
                    # abort together. A completed read clears/replaces this
                    # exact tuple before the watchdog can cut it.
                    self._stalled = waiting[1]
                    self._waiting = None
                    logger.warning("Audio stream stalled at byte %s: received %s bytes in %.0f s",
                                   self.position, received, STALL_SECONDS)
                    abort_response(waiting[1])

    def _more_follows(self):
        """Ask whether bytes remain after a body that has no length.

        A body with neither a length nor chunk framing ends where the
        connection does, so a cut-off one looks complete. A range request at
        the current byte tells them apart: bytes (206) mean it was cut short
        and playback goes on from them. "Nothing satisfiable" (416), a server
        that ignores ranges, or no answer leave the end as it looked."""
        if not self._delimited or self.position <= 0:
            return False
        try:
            self._open(self.position)
        except Exception:
            return False
        logger.warning("Audio stream ended at byte %s without a length, but the server has more; continuing", self.position)
        return True

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

    def _overrun(self, message):
        """A stream that delivers more than it declared. Nothing else logs this end."""
        logger.error("Audio stream stopped at byte %s of %s: %s", self.position,
                     self.size if self.size is not None else "unknown", message)
        self._fail(message)

    def _fail(self, message):
        self._eof = True
        self._failed = True
        self._on_error(message)
        raise _StreamFailure(message) from None

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
        failure = None
        for attempt in range(len(SEEK_RETRY_DELAYS) + 1):
            if attempt and self.cancelled.wait(SEEK_RETRY_DELAYS[attempt - 1]):
                return -1
            try:
                self._open(offset)
                return offset
            except Exception as exc:
                failure = exc
                # A server that cannot seek (or a changed file) will not do
                # better on a retry; a dropped or refused request may.
                if self.cancelled.is_set() or _permanent(exc):
                    break
        # libmpv treats a negative offset as an unsupported/failed seek.
        # Keep the previous response usable rather than mixing wrong bytes.
        if not self.cancelled.is_set():
            logger.warning("Audio stream seek to byte %s failed: %s", offset, _describe(failure))
            if not _permanent(failure):
                # The server can seek; it did not answer. Say so, so this is
                # not mistaken for a server without range support.
                self._failed = True
                self._on_error('The audio server did not answer the seek request. Check the connection and try again.')
        return -1

    def cancel(self):
        self.cancelled.set()
        with self._lock:
            response = self._response
        if response is not None:
            abort_response(response)

    def close(self):
        self.cancel()
        with self._lock:
            self._response = None
        chunks, self._chunks = self._chunks, iter(())
        try:
            close = getattr(chunks, 'close', None)
            if close is not None:
                close()
        except ValueError:
            pass  # A cancelled read is still unwinding on its native thread.
        self._buffer = b''
        self.session.close()
