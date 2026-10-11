"""Directory provider contract with ordered fallback."""

import logging
from typing import Protocol

from ..domain import DirectoryCandidate
from ..net import describe_network_error
from ..privacy import redact

_log = logging.getLogger(__name__)
UNEXPECTED_ANSWER = "The directory returned an unexpected page. Check the connection or try again later."


class DirectoryError(RuntimeError):
    """A directory request failed.

    `network` is True when the directory could not be reached at all, as
    opposed to answering badly; the window shows its offline notice for it."""

    def __init__(self, *args, network: bool = False):
        super().__init__(*args)
        self.network = network


def is_unreachable(exc) -> bool:
    """True when `exc` is a failed connection, raw or wrapped by a provider.

    A TLS failure is a connection error to the requests library, but the
    directory was reached and answered badly, so it is not an outage."""
    from requests import exceptions as errors

    while exc is not None:
        if getattr(exc, "network", False):
            return True
        if isinstance(exc, errors.ConnectionError) and not isinstance(exc, errors.SSLError):
            return True
        exc = getattr(exc, "__cause__", None)
    return False


def _all_failed(errors) -> DirectoryError:
    """One error for providers that all failed: unreachable only if each was."""
    return DirectoryError(
        "; ".join(f"{name}: {exc}" for name, exc in errors),
        network=all(is_unreachable(exc) for _name, exc in errors),
    )


def failure_text(exc, what: str) -> str:
    """The banner text for a failed directory request.

    A failed request is named by describe_network_error. An answer that did
    not parse (a JSON or HTML parser message, a missing key) becomes one fixed
    sentence, and its detail goes to the log instead of the banner."""
    import requests

    if isinstance(exc, requests.RequestException) and not isinstance(exc, requests.exceptions.JSONDecodeError):
        return describe_network_error(exc, what)
    _log.warning("Directory answer did not parse: %s", redact(str(exc)))
    return UNEXPECTED_ANSWER


class CandidateList(list):
    """Directory candidates that say whether the answer is complete.

    `partial` is True when a request or the deadline dropped results, so the
    list may be shorter than the directory's answer and must not be kept as
    that answer. A slice keeps the flag."""

    partial = False

    def __init__(self, items=(), partial=False):
        super().__init__(items)
        self.partial = partial

    def __getitem__(self, index):
        found = super().__getitem__(index)
        return CandidateList(found, self.partial) if isinstance(index, slice) else found


class DirectoryProvider(Protocol):
    name: str

    def search(self, query: str, limit: int = 30) -> list[DirectoryCandidate]: ...

    def browse(self, category: str = "", limit: int = 30) -> list[DirectoryCandidate]: ...

    def recommend(self, shows, limit: int = 30) -> list[DirectoryCandidate]: ...

    def topic(
        self, category: str, topic: str, limit: int = 30
    ) -> list[DirectoryCandidate]: ...

    def chart(
        self, chart_type: str, category: str = "", limit: int = 30
    ) -> list[DirectoryCandidate]: ...


class DirectoryService:
    def invalidate(self):
        for provider in self.providers:
            invalidate = getattr(provider, "invalidate", None)
            if invalidate is not None:
                invalidate()

    def __init__(self, providers: list[DirectoryProvider]):
        self.providers = list(providers)

    def search(self, query: str, limit: int = 30) -> list[DirectoryCandidate]:
        return self._fan_out("search", (query,), limit)

    def browse(self, category: str = "", limit: int = 30) -> list[DirectoryCandidate]:
        return self._fan_out("browse", (category,), limit)

    def recommend(self, shows, limit: int = 30) -> list[DirectoryCandidate]:
        return self._fan_out("recommend", (shows,), limit)

    def topic(
        self, category: str, topic: str, limit: int = 30
    ) -> list[DirectoryCandidate]:
        return self._fan_out("topic", (category, topic), limit)

    def chart(self, chart_type: str, category: str = "", limit: int = 30):
        return self._fan_out("chart", (chart_type, category), limit)

    def _fan_out(self, method: str, args: tuple, limit: int):
        """The first non-empty answer from the providers, asked in order.

        A provider that raises, or that lacks `method`, is logged with its
        traceback and counts as a failure; the next provider is asked. An
        empty answer is an answer. An error is raised only when no provider
        answered and at least one failed."""
        errors = []
        succeeded = False
        for provider in self.providers:
            try:
                results = getattr(provider, method)(*args, limit)
            except Exception as exc:
                _log.warning("Directory provider %s could not answer %s", provider.name, method, exc_info=True)
                errors.append((provider.name, exc))
                continue
            succeeded = True
            if results:
                return results[:limit]
        if errors and not succeeded:
            raise _all_failed(errors)
        return []
