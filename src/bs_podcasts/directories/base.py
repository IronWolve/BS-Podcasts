"""Directory provider contract with ordered fallback."""

from typing import Protocol

from ..domain import DirectoryCandidate


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
        return self._first("search", query, limit)

    def browse(self, category: str = "", limit: int = 30) -> list[DirectoryCandidate]:
        return self._first("browse", category, limit)

    def recommend(self, shows, limit: int = 30) -> list[DirectoryCandidate]:
        errors = []
        succeeded = False
        for provider in self.providers:
            if not hasattr(provider, "recommend"):
                continue
            try:
                results = provider.recommend(shows, limit)
            except Exception as exc:
                errors.append((provider.name, exc))
                continue
            succeeded = True
            if results:
                return results[:limit]
        # An empty answer from a working provider is an answer; raise only
        # when every provider that was asked actually failed.
        if errors and not succeeded:
            raise _all_failed(errors)
        return []

    def topic(
        self, category: str, topic: str, limit: int = 30
    ) -> list[DirectoryCandidate]:
        errors = []
        succeeded = False
        for provider in self.providers:
            if not hasattr(provider, "topic"):
                continue
            try:
                results = provider.topic(category, topic, limit)
            except Exception as exc:
                errors.append((provider.name, exc))
                continue
            succeeded = True
            if results:
                return results[:limit]
        # An empty answer from a working provider is an answer; raise only
        # when every provider that was asked actually failed.
        if errors and not succeeded:
            raise _all_failed(errors)
        return []

    def chart(self, chart_type: str, category: str = "", limit: int = 30):
        errors = []
        succeeded = False
        for provider in self.providers:
            if not hasattr(provider, "chart"):
                continue
            try:
                results = provider.chart(chart_type, category, limit)
            except Exception as exc:
                errors.append((provider.name, exc))
                continue
            succeeded = True
            if results:
                return results[:limit]
        # An empty answer from a working provider is an answer; raise only
        # when every provider that was asked actually failed.
        if errors and not succeeded:
            raise _all_failed(errors)
        return []

    def _first(self, method: str, value: str, limit: int):
        errors = []
        for provider in self.providers:
            try:
                results = getattr(provider, method)(value, limit)
            except Exception as exc:
                errors.append((provider.name, exc))
                continue
            if results:
                return results[:limit]
        if errors and len(errors) == len(self.providers):
            raise _all_failed(errors)
        return []
