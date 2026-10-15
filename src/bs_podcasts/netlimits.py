"""Bounded daemon network work; caller deadlines also cover stuck OS reads."""
import queue
import socket
import threading
import time
from contextlib import contextmanager

HEADERS = threading.BoundedSemaphore(12)
DNS = threading.BoundedSemaphore(16)
BODIES = threading.BoundedSemaphore(12)
PLAYBACK_HEADERS = threading.BoundedSemaphore(2)
PLAYBACK_DNS = threading.BoundedSemaphore(2)
PLAYBACK_BODIES = threading.BoundedSemaphore(2)
_FOREGROUND_GATES = (PLAYBACK_HEADERS, PLAYBACK_DNS, PLAYBACK_BODIES)
# A superseded play is cancelled at once, but its worker can take a moment to
# hand its permit back. A new play waits this long for it instead of failing.
FOREGROUND_ADMISSION_WAIT = 2.0
_context = threading.local()


@contextmanager
def request_scope(cancel_event=None, foreground=False, deadline=None, download_stream=False):
    previous = (getattr(_context, 'cancel', None), getattr(_context, 'foreground', False),
                getattr(_context, 'deadline', None), getattr(_context, 'download_stream', False))
    _context.cancel, _context.foreground, _context.deadline = cancel_event, foreground, deadline
    _context.download_stream = download_stream
    try:
        yield
    finally:
        (_context.cancel, _context.foreground, _context.deadline,
         _context.download_stream) = previous


@contextmanager
def download_scope():
    """Opt this request into larger reads without replacing its other scope."""
    previous = getattr(_context, 'download_stream', False)
    _context.download_stream = True
    try:
        yield
    finally:
        _context.download_stream = previous


def current_cancel():
    return getattr(_context, 'cancel', None)


def is_foreground():
    return getattr(_context, 'foreground', False)


def is_download_stream():
    return getattr(_context, 'download_stream', False)


class NetworkDeadline(Exception):
    """A network deadline or cancellation. Not an OSError: a broad OSError
    handler around a request must not turn it into a network failure."""


class ResponseTooLarge(ValueError):
    """A response exceeds its byte budget, not its network deadline."""


class Deadline:
    def __init__(self, seconds, user_cancel=None, foreground=None):
        self.started = time.monotonic()
        self.expires = self.started + seconds
        self.cancelled = threading.Event()
        self.user_cancel = user_cancel if user_cancel is not None else current_cancel()
        self.foreground = is_foreground() if foreground is None else foreground
        self.download_stream = is_download_stream() and not self.foreground
        self.parent = getattr(_context, 'deadline', None)

    def remaining(self):
        if self.cancelled.is_set() or (self.user_cancel is not None and self.user_cancel.is_set()):
            raise NetworkDeadline("Network operation cancelled.")
        remaining = self.expires - time.monotonic()
        if self.parent is not None:
            remaining = min(remaining, self.parent.remaining())
        if remaining <= 0:
            raise NetworkDeadline("Network operation exceeded its deadline.")
        return remaining


def _admit(gate, deadline) -> bool:
    """Take a permit. Background work fails fast when the gate is full;
    playback waits briefly, within its own deadline and cancellation."""
    if gate.acquire(blocking=False):
        return True
    if not any(gate is foreground for foreground in _FOREGROUND_GATES):
        return False
    until = time.monotonic() + FOREGROUND_ADMISSION_WAIT
    while True:
        left = min(until - time.monotonic(), deadline.remaining())
        if left <= 0:
            return False
        if gate.acquire(timeout=min(.05, left)):
            return True


def abort_response(response):
    try:
        raw = response.raw
        reader = getattr(getattr(raw, "_fp", None), "fp", None)
        sock = getattr(getattr(reader, "raw", None), "_sock", None)
        if sock is not None:
            sock.shutdown(socket.SHUT_RDWR)
    except (AttributeError, OSError):
        pass
    try:
        response.close()
    except Exception:
        pass


def _shutdown_socket(sock):
    try:
        sock.shutdown(socket.SHUT_RDWR)
    except (AttributeError, OSError):
        pass
    try:
        sock.close()
    except (AttributeError, OSError):
        pass


class _SocketBag:
    """Sockets the header worker opened, so a cancelled wait can unblock it."""

    def __init__(self):
        self._lock = threading.Lock()
        self._socks = []
        self._closed = False

    def add(self, sock):
        with self._lock:
            if self._closed:
                _shutdown_socket(sock)
                return
            self._socks.append(sock)

    def close(self):
        with self._lock:
            self._closed = True
            socks = list(self._socks)
        for sock in socks:
            _shutdown_socket(sock)


_socket_bags = threading.local()


def watch_request_socket(sock):
    """Include a socket in the current header cancellation scope.

    Only the connection code registers sockets: the plain socket before it
    connects, a duplicate of it through the TLS handshake (wrapping detaches
    the plain socket object), and a pooled connection before its status line."""
    bag = getattr(_socket_bags, 'bag', None)
    if sock is not None and bag is not None:
        bag.add(sock)


def bounded_call(work, deadline, gate=None, dispose=None):
    gate = gate if gate is not None else (PLAYBACK_HEADERS if deadline.foreground else HEADERS)
    if not _admit(gate, deadline):
        raise NetworkDeadline("Network workers are busy; retry shortly.")
    results = queue.Queue(1)
    handoff = threading.Lock()
    released = False
    sockets = _SocketBag()

    def release():
        nonlocal released
        if not released:
            released = True
            gate.release()

    def run():
        value = None
        try:
            try:
                _socket_bags.bag = sockets
                deadline.remaining()
                value = work()
                result = (True, value)
            except Exception as exc:
                result = (False, exc.with_traceback(None))
            with handoff:
                abandoned = deadline.cancelled.is_set()
                if not abandoned:
                    results.put_nowait(result)
            if abandoned and value is not None and dispose is not None:
                dispose(value)
        finally:
            _socket_bags.bag = None
            with handoff:
                release()

    worker = threading.Thread(target=run, name="bs-http-headers", daemon=True)
    try:
        worker.start()
    except Exception:
        release()
        raise
    try:
        while True:
            remaining = deadline.remaining()
            try:
                success, result = results.get(timeout=min(.05, remaining))
            except queue.Empty:
                continue
            if success:
                return result
            raise result
    except BaseException:
        with handoff:
            deadline.cancelled.set()
            # A DNS permit counts the actual worker: cancelling a caller
            # cannot interrupt getaddrinfo or admit unlimited orphan threads.
            if gate is not DNS and gate is not PLAYBACK_DNS:
                release()
            try:
                success, result = results.get_nowait()
            except queue.Empty:
                success, result = False, None
        # Unblock a connect or a header read. A stuck getaddrinfo cannot be
        # shut down here; its permit stays held until the resolver returns.
        sockets.close()
        if success and dispose is not None:
            dispose(result)
        raise


def bounded_chunks(response, original, deadline, maximum, chunk_size, decode_unicode=False):
    gate = PLAYBACK_BODIES if deadline.foreground else BODIES
    raw = getattr(response, 'raw', None)
    raw_stream = getattr(raw, 'stream', None)
    raw_dict = getattr(raw, '__dict__', None)
    stream_override = raw_dict.get('stream') if isinstance(raw_dict, dict) else None
    had_stream_override = isinstance(raw_dict, dict) and 'stream' in raw_dict
    content_encoding = str(getattr(response, 'headers', {}).get('Content-Encoding', '')).strip().lower()
    # urllib3.read1 yields bytes as they arrive. Keep the legacy read size for
    # transports without it and encoded bodies, where a read1 decode can emit
    # no bytes while its decoder buffers incomplete input.
    use_read1 = (deadline.download_stream and isinstance(raw_dict, dict)
                 and callable(getattr(raw, 'read1', None)) and callable(raw_stream)
                 and content_encoding in {'', 'identity'})
    # Four bounded slots keep the faster writer decoupled without allowing an
    # unbounded body queue. Foreground playback stays at 16 KiB.
    chunk_limit = 64 * 1024 if use_read1 else 16 * 1024
    read_size = min(chunk_size or 16 * 1024, chunk_limit)
    results = queue.Queue(4)
    stopped = threading.Event()
    # Prepare the reader before taking a permit; invalid arguments or a
    # malformed injected response must not strand a network-reader slot.
    try:
        admitted = _admit(gate, deadline)
    except NetworkDeadline:
        abort_response(response)
        raise
    if not admitted:
        abort_response(response)
        raise NetworkDeadline("Network readers are busy; retry shortly.")

    def deliver(result):
        while not stopped.is_set():
            deadline.remaining()
            try:
                results.put(result, timeout=.05)
                return
            except queue.Full:
                continue

    def read():
        try:
            if use_read1:
                # requests.iter_content delegates to raw.stream(), whose
                # read() waits for the entire requested amount. Swap only this
                # response's stream method for the duration of its one reader
                # thread; urllib3 read1 returns currently available bytes,
                # including chunked bodies (http.client.read1 preserves their
                # framing). requests.iter_content still maps transport errors
                # to the usual Requests exceptions for _guarded_chunks.
                def available_stream(amount, decode_content=None):
                    while True:
                        chunk = raw.read1(amount, decode_content=decode_content)
                        if not chunk:
                            return
                        yield chunk
                raw.stream = available_stream
            try:
                for chunk in original(read_size, decode_unicode=decode_unicode):
                    if stopped.is_set():
                        break
                    deliver((0, chunk))
            finally:
                if use_read1:
                    if had_stream_override:
                        raw.stream = stream_override
                    else:
                        del raw.__dict__['stream']
            if not stopped.is_set():
                deliver((1, None))
        except Exception as exc:
            try:
                deliver((2, exc.with_traceback(None)))
            except NetworkDeadline:
                pass
        finally:
            gate.release()

    try:
        threading.Thread(target=read, name="bs-http-body", daemon=True).start()
    except BaseException:
        gate.release()
        abort_response(response)
        raise
    finished = False
    size = 0
    try:
        while True:
            remaining = deadline.remaining()
            try:
                kind, value = results.get(timeout=min(.05, remaining))
            except queue.Empty:
                continue
            if kind == 1:
                finished = True
                return
            if kind == 2:
                raise value
            size += len(value)
            if maximum is not None and size > maximum:
                raise ResponseTooLarge(f"Response exceeds the {maximum}-byte limit.")
            yield value
    finally:
        stopped.set()
        if not finished:
            # A blocked body never traps a GUI/job-pool thread or Python exit.
            threading.Thread(target=abort_response, args=(response,), name="bs-http-close", daemon=True).start()
