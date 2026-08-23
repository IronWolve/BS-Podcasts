"""Directory provider contract with ordered fallback."""

from typing import Protocol

from ..domain import DirectoryCandidate


class DirectoryError(RuntimeError):
    pass


class DirectoryProvider(Protocol):
    name: str

    def search(self, query: str, limit: int = 30) -> list[DirectoryCandidate]: ...

    def browse(self, category: str = "", limit: int = 30) -> list[DirectoryCandidate]: ...


class DirectoryService:
    def __init__(self, providers: list[DirectoryProvider]):
        self.providers = list(providers)

    def search(self, query: str, limit: int = 30) -> list[DirectoryCandidate]:
        return self._first("search", query, limit)

    def browse(self, category: str = "", limit: int = 30) -> list[DirectoryCandidate]:
        return self._first("browse", category, limit)

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
