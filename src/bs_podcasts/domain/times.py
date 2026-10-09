"""Finite, bounded media times shared by ingestion, storage and display."""
import math

MAX_MEDIA_SECONDS = 2**31


def media_seconds(value):
    try:
        seconds = float(value)
    except (TypeError, ValueError, OverflowError):
        return None
    return seconds if math.isfinite(seconds) and 0 <= seconds <= MAX_MEDIA_SECONDS else None


def media_interval(start, end):
    start, end = media_seconds(start), media_seconds(end)
    if start is not None and end is not None and end < start:
        end = None
    return start, end


def format_media_time(value) -> str:
    """Elapsed media time, floor to seconds; retain hours for long episodes."""
    total = int(media_seconds(value) or 0)
    hours, remainder = divmod(total, 3600)
    minutes, seconds = divmod(remainder, 60)
    return f"{hours}:{minutes:02d}:{seconds:02d}" if hours else f"{minutes}:{seconds:02d}"
