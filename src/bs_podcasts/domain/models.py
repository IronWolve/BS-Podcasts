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
    playback_speed: float = 1.0
    skip_back: int = 15
    skip_forward: int = 30
    auto_continue: bool = True


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
    last_played: float | None = None


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


class DownloadState(StrEnum):
    QUEUED = "queued"
    DOWNLOADING = "downloading"
    PAUSED = "paused"
    COMPLETE = "complete"
    ERROR = "error"


@dataclass(frozen=True)
class DownloadRecord:
    id: int
    episode_id: int
    source_url: str
    target_path: str
    partial_path: str
    state: DownloadState
    bytes_done: int = 0
    bytes_total: int = 0
    error_message: str = ""
    episode_title: str = ""
    show_title: str = ""
