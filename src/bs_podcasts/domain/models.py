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
    latest_episode_title: str = ""
    latest_episode_published_at: str = ""
    playback_speed: float = 1.0
    skip_back: int = 15
    skip_forward: int = 30
    auto_continue: bool = True
    trim_level: str = "off"
    auto_download_override: bool | None = None
    auto_download_limit: int | None = None
    retention_keep: int | None = None
    retention_days: int | None = None
    categories: str = ""


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
    transcript_url: str = ""
    transcript_type: str = ""
    artwork_path: str = ""
    chapters_url: str = ""
    artwork_url: str = ""
    website_url: str = ""
    author: str = ""
    season_number: int | None = None
    episode_number: int | None = None
    episode_type: str = ""
    explicit: bool | None = None
    enclosure_bytes: int = 0
    favorite: bool = False


@dataclass(frozen=True)
class FeedEpisodeData:
    external_id: str
    title: str
    description: str = ""
    media_url: str = ""
    mime_type: str = "audio/*"
    published_at: str = ""
    duration_seconds: int = 0
    transcript_url: str = ""
    transcript_type: str = ""
    chapters_url: str = ""
    artwork_url: str = ""
    website_url: str = ""
    author: str = ""
    season_number: int | None = None
    episode_number: int | None = None
    episode_type: str = ""
    explicit: bool | None = None
    enclosure_bytes: int = 0


@dataclass(frozen=True)
class Chapter:
    id: int
    episode_id: int
    index: int
    start_seconds: float
    end_seconds: float | None
    title: str
    artwork_url: str = ""


@dataclass(frozen=True)
class TranscriptSegment:
    id: int
    episode_id: int
    index: int
    text: str
    start_seconds: float | None = None
    end_seconds: float | None = None


@dataclass(frozen=True)
class Bookmark:
    id: int
    episode_id: int
    position_seconds: float
    title: str
    created_at: float
    episode_title: str = ""
    show_title: str = ""


@dataclass(frozen=True)
class FeedData:
    title: str
    author: str = ""
    description: str = ""
    website_url: str = ""
    artwork_url: str = ""
    categories: tuple[str, ...] = ()
    episodes: tuple[FeedEpisodeData, ...] = ()


@dataclass(frozen=True)
class DirectoryCandidate:
    title: str
    author: str
    feed_url: str
    artwork_url: str = ""
    genre: str = ""
    rank: int = 0
    chart_type: str = ""
    directory_url: str = ""


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
