"""Bounded background jobs with one typed result surface."""

from concurrent.futures import ThreadPoolExecutor, TimeoutError
from dataclasses import dataclass
from enum import StrEnum
from typing import Any, Callable


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
    def __init__(self, max_workers: int = 4):
        self._executor = ThreadPoolExecutor(max_workers=max_workers, thread_name_prefix="bs-job")

    def submit(self, callable_: Callable, *args, **kwargs):
        return self._executor.submit(self._invoke, callable_, args, kwargs)

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
            return JobResult(JobStatus.ERROR, message=str(exc) or exc.__class__.__name__)

    def shutdown(self, wait: bool = False):
        self._executor.shutdown(wait=wait, cancel_futures=True)
