"""Immutable records shared by persistence and services."""

from dataclasses import dataclass
from enum import StrEnum


class Health(StrEnum):
    UNKNOWN = "unknown"
    LOADING = "loading"
    OK = "ok"
    PARTIAL = "partial"
    ERROR = "error"
    SUSPENDED = "suspended"


@dataclass(frozen=True)
class Show:
    id: int
    feed_url: str
    title: str
    canonical_url: str = ""
    author: str = ""
    description: str = ""
    website_url: str = ""
    artwork_url: str = ""
    artwork_path: str = ""
    source: str = "rss"
    health: Health = Health.UNKNOWN
    fail_count: int = 0
    suspended: bool = False
    etag: str = ""
    last_modified: str = ""
    last_refresh: float | None = None
    episode_count: int = 0
    new_count: int = 0


@dataclass(frozen=True)
class Episode:
    id: int
    show_id: int
    external_id: str
    title: str
    show_title: str = ""
    description: str = ""
    media_url: str = ""
    mime_type: str = "audio/*"
    published_at: str = ""
    duration_seconds: int = 0
    position_seconds: float = 0.0
    played: bool = False
    is_new: bool = True
    downloaded_path: str = ""


@dataclass(frozen=True)
class FeedEpisodeData:
    external_id: str
    title: str
    description: str = ""
    media_url: str = ""
    mime_type: str = "audio/*"
    published_at: str = ""
    duration_seconds: int = 0


@dataclass(frozen=True)
class FeedData:
    title: str
    author: str = ""
    description: str = ""
    website_url: str = ""
    artwork_url: str = ""
    episodes: tuple[FeedEpisodeData, ...] = ()


@dataclass(frozen=True)
class DirectoryCandidate:
    title: str
    author: str
    feed_url: str
    artwork_url: str = ""
    genre: str = ""
