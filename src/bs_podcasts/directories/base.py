"""Directory provider contract with ordered fallback."""

from typing import Protocol

from ..domain import DirectoryCandidate


class DirectoryError(RuntimeError):
    pass


class DirectoryProvider(Protocol):
    name: str

    def search(self, query: str, limit: int = 30) -> list[DirectoryCandidate]: ...

    def browse(self, category: str = "", limit: int = 30) -> list[DirectoryCandidate]: ...

    def recommend(self, shows, limit: int = 30) -> list[DirectoryCandidate]: ...

    def topic(
        self, category: str, topic: str, limit: int = 30
    ) -> list[DirectoryCandidate]: ...

    def chart(
        self, chart_type: str, category: str = ""
    ) -> list[DirectoryCandidate]: ...


class DirectoryService:
    def __init__(self, providers: list[DirectoryProvider]):
        self.providers = list(providers)

    def search(self, query: str, limit: int = 30) -> list[DirectoryCandidate]:
        return self._first("search", query, limit)

    def browse(self, category: str = "", limit: int = 30) -> list[DirectoryCandidate]:
        return self._first("browse", category, limit)

    def recommend(self, shows, limit: int = 30) -> list[DirectoryCandidate]:
        errors = []
        for provider in self.providers:
            if not hasattr(provider, "recommend"):
                continue
            try:
                results = provider.recommend(shows, limit)
            except Exception as exc:
                errors.append(f"{provider.name}: {exc}")
                continue
            if results:
                return results[:limit]
        if errors:
            raise DirectoryError("; ".join(errors))
        return []

    def topic(
        self, category: str, topic: str, limit: int = 30
    ) -> list[DirectoryCandidate]:
        errors = []
        for provider in self.providers:
            if not hasattr(provider, "topic"):
                continue
            try:
                results = provider.topic(category, topic, limit)
            except Exception as exc:
                errors.append(f"{provider.name}: {exc}")
                continue
            if results:
                return results[:limit]
        if errors:
            raise DirectoryError("; ".join(errors))
        return []

    def chart(self, chart_type: str, category: str = ""):
        errors = []
        for provider in self.providers:
            if not hasattr(provider, "chart"):
                continue
            try:
                results = provider.chart(chart_type, category)
            except Exception as exc:
                errors.append(f"{provider.name}: {exc}")
                continue
            if results:
                return results
        if errors:
            raise DirectoryError("; ".join(errors))
        return []

    def _first(self, method: str, value: str, limit: int):
        errors = []
        for provider in self.providers:
            try:
                results = getattr(provider, method)(value, limit)
            except Exception as exc:
                errors.append(f"{provider.name}: {exc}")
                continue
            if results:
                return results[:limit]
        if errors and len(errors) == len(self.providers):
            raise DirectoryError("; ".join(errors))
        return []
