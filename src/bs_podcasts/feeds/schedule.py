"""Durable refresh cadence derived from saved publication/attempt timestamps."""

from datetime import datetime, timezone
import math
import time

DAY = 86400


def published_time(value):
    try:
        stamp = datetime.fromisoformat(str(value).replace('Z', '+00:00'))
        if stamp.tzinfo is None:
            stamp = stamp.replace(tzinfo=timezone.utc)
        result = stamp.timestamp()
        return result if math.isfinite(result) else None
    except (ValueError, TypeError, OverflowError, OSError):
        return None


def refresh_seconds(show, minutes, now, smart=True):
    base = max(1, int(minutes or 60)) * 60
    published = published_time(show.latest_episode_published_at)
    if not smart or published is None:
        return base
    age = max(0, now - published)
    quiet = 7 * DAY if age > 180 * DAY else DAY if age > 60 * DAY else 6 * 3600 if age > 14 * DAY else base
    # A failed connection should not postpone an archived feed's next try
    # for a whole week, nor turn every restart into another attempt.
    if show.last_refresh_error:
        quiet = min(quiet, 6 * 3600)
    return max(base, quiet)


def due_shows(shows, minutes, *, now=None, smart=True, priority=(), manual=False):
    if minutes <= 0 and not manual:
        return []
    now = time.time() if now is None else now
    priority = set(priority)
    result = []
    base = max(1, int(minutes or 60)) * 60
    for show in shows:
        if show.source == 'local' or show.suspended:
            continue
        interval = refresh_seconds(show, minutes, now, smart)
        stamps = [value for value in (show.last_refresh, show.last_refresh_attempt)
                  if isinstance(value, (int, float)) and math.isfinite(value)]
        last = max(stamps) if stamps else None
        # Tolerate completion just after a timer tick without halving its rate.
        due = last is None or last > now + 300 or now >= last + interval - min(300, interval // 2)
        # A user-triggered library check fetches active feeds immediately;
        # inactive feeds keep their cadence. Check all is a separate action.
        if not due and not (manual and interval == base):
            continue
        published = published_time(show.latest_episode_published_at)
        rank = 0 if last is None else 1 if show.id in priority else 2 if interval == base else 3
        result.append(((rank, -(published or 0), last or 0, show.id), show))
    return [show for _key, show in sorted(result, key=lambda pair: pair[0])]
