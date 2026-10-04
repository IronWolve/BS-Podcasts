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
_context = threading.local()


@contextmanager
def request_scope(cancel_event=None, foreground=False, deadline=None):
    previous = (getattr(_context, 'cancel', None), getattr(_context, 'foreground', False),
                getattr(_context, 'deadline', None))
    _context.cancel, _context.foreground, _context.deadline = cancel_event, foreground, deadline
    try:
        yield
    finally:
        _context.cancel, _context.foreground, _context.deadline = previous


def current_cancel():
    return getattr(_context, 'cancel', None)


def is_foreground():
    return getattr(_context, 'foreground', False)


class NetworkDeadline(TimeoutError):
    pass


class Deadline:
    def __init__(self, seconds, user_cancel=None, foreground=None):
        self.started = time.monotonic()
        self.expires = self.started + seconds
        self.cancelled = threading.Event()
        self.user_cancel = user_cancel if user_cancel is not None else current_cancel()
        self.foreground = is_foreground() if foreground is None else foreground
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
_socket_init_lock = threading.Lock()
_socket_init_installed = False


def watch_request_socket(sock):
    """Include a pooled connection in the current header cancellation scope."""
    bag = getattr(_socket_bags, 'bag', None)
    if sock is not None and bag is not None:
        bag.add(sock)


def _install_socket_watch():
    """Record sockets created on a worker that has a bag. getaddrinfo has none."""
    global _socket_init_installed
    with _socket_init_lock:
        if _socket_init_installed:
            return
        original = socket.socket.__init__

        def watched(self, *args, **kwargs):
            original(self, *args, **kwargs)
            bag = getattr(_socket_bags, "bag", None)
            if bag is not None:
                bag.add(self)

        socket.socket.__init__ = watched
        _socket_init_installed = True


def bounded_call(work, deadline, gate=None, dispose=None):
    gate = gate if gate is not None else (PLAYBACK_HEADERS if deadline.foreground else HEADERS)
    if not gate.acquire(blocking=False):
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
                _install_socket_watch()
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
    if not gate.acquire(blocking=False):
        abort_response(response)
        raise NetworkDeadline("Network readers are busy; retry shortly.")
    results = queue.Queue(4)
    stopped = threading.Event()

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
            for chunk in original(min(chunk_size or 16384, 16384), decode_unicode=decode_unicode):
                if stopped.is_set():
                    break
                deliver((0, chunk))
            if not stopped.is_set():
                deliver((1, None))
        except Exception as exc:
            try:
                deliver((2, exc.with_traceback(None)))
            except NetworkDeadline:
                pass
        finally:
            gate.release()

    threading.Thread(target=read, name="bs-http-body", daemon=True).start()
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
                raise NetworkDeadline(f"Response exceeds the {maximum}-byte limit.")
            yield value
    finally:
        stopped.set()
        if not finished:
            # A blocked body never traps a GUI/job-pool thread or Python exit.
            threading.Thread(target=abort_response, args=(response,), name="bs-http-close", daemon=True).start()
