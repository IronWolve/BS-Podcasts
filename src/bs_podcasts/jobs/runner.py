"""Bounded background jobs with one typed result surface."""

from concurrent.futures import ThreadPoolExecutor, TimeoutError, Future
from threading import BoundedSemaphore
from dataclasses import dataclass
import logging
import time
from enum import StrEnum
from typing import Any, Callable


_log = logging.getLogger('bs_podcasts')


class JobStatus(StrEnum):
    OK = "ok"
    EMPTY = "empty"
    ERROR = "error"
    TIMEOUT = "timeout"


@dataclass(frozen=True)
class JobResult:
    status: JobStatus
    value: Any = None
    message: str = ""


class JobRunner:
    def __init__(self, max_workers: int = 4, max_pending: int | None = None):
        self._executor = ThreadPoolExecutor(max_workers=max_workers, thread_name_prefix="bs-job")
        self._slots = BoundedSemaphore(max_pending) if max_pending is not None else None

    def submit(self, callable_: Callable, *args, **kwargs):
        if self._slots is None:
            return self._executor.submit(self._invoke, callable_, args, kwargs)
        if not self._slots.acquire(blocking=False):
            result = Future()
            result.set_result(JobResult(JobStatus.ERROR, message='Background requests are busy; retry shortly.'))
            return result
        try:
            future = self._executor.submit(self._invoke, callable_, args, kwargs)
        except BaseException:
            self._slots.release()
            raise
        # Cancelling the executor's own future releases queued capacity now;
        # running work cannot be cancelled and retains its slot until done.
        future.add_done_callback(lambda _completed: self._slots.release())
        return future

    @staticmethod
    def _invoke(callable_: Callable, args: tuple, kwargs: dict) -> JobResult:
        try:
            value = callable_(*args, **kwargs)
            if value is None:
                return JobResult(JobStatus.EMPTY)
            return JobResult(JobStatus.OK, value=value)
        except TimeoutError as exc:
            return JobResult(JobStatus.TIMEOUT, message=str(exc) or "Operation timed out.")
        except Exception as exc:
            # The result carries only the message; this is the one place the
            # traceback of a failing background callable can be kept.
            _log.exception('Background job %s failed', getattr(callable_, '__qualname__', type(callable_).__name__))
            return JobResult(JobStatus.ERROR, message=str(exc) or exc.__class__.__name__)

    def shutdown(self, wait: bool = False, cancel_futures: bool = True):
        self._executor.shutdown(wait=wait, cancel_futures=cancel_futures)

    def join(self, grace_seconds: float = 3.0, cancel_futures: bool = True) -> int:
        """Stop accepting work, wait up to `grace_seconds` for running jobs, and
        return how many worker threads are still busy. Callers that get a
        non-zero count should exit hard: the interpreter would otherwise block
        at exit joining those (non-daemon) threads."""
        self._executor.shutdown(wait=False, cancel_futures=cancel_futures)
        deadline = time.monotonic() + grace_seconds
        for thread in list(getattr(self._executor, "_threads", ())):
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                break
            thread.join(remaining)
        return sum(1 for thread in getattr(self._executor, "_threads", ()) if thread.is_alive())
