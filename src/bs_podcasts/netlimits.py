"""Bounded daemon network work; caller deadlines also cover stuck OS reads."""
import queue
import socket
import threading
import time

HEADERS = threading.BoundedSemaphore(12)
DNS = threading.BoundedSemaphore(16)
BODIES = threading.BoundedSemaphore(12)


class NetworkDeadline(TimeoutError):
    pass


class Deadline:
    def __init__(self, seconds, user_cancel=None):
        self.started = time.monotonic()
        self.expires = self.started + seconds
        self.cancelled = threading.Event()
        self.user_cancel = user_cancel

    def remaining(self):
        if self.cancelled.is_set() or (self.user_cancel is not None and self.user_cancel.is_set()):
            raise NetworkDeadline("Network operation cancelled.")
        remaining = self.expires - time.monotonic()
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


def bounded_call(work, deadline, gate=HEADERS, dispose=None):
    if not gate.acquire(blocking=False):
        raise NetworkDeadline("Network workers are busy; retry shortly.")
    results = queue.Queue(1)
    handoff = threading.Lock()

    def run():
        value = None
        try:
            try:
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
            gate.release()

    threading.Thread(target=run, name="bs-http-headers", daemon=True).start()
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
            try:
                success, result = results.get_nowait()
            except queue.Empty:
                success, result = False, None
        if success and dispose is not None:
            dispose(result)
        raise


def bounded_chunks(response, original, deadline, maximum, chunk_size, decode_unicode=False):
    if not BODIES.acquire(blocking=False):
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
            BODIES.release()

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
