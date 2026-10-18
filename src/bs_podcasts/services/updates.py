"""Small, read-only GitHub release checker. It never installs updates."""

from dataclasses import dataclass
import json
import logging
import re

from ..config import RELEASES_API_URL
from ..net import make_session, describe_network_error
from ..netlimits import NetworkDeadline
from ..urlguard import UnsafeUrl, ensure_web_url


@dataclass(frozen=True)
class UpdateResult:
    installed: str
    available: str  # "" means the project has no published release yet
    newer: bool
    notes: str
    url: str
    error: str = ""

    @property
    def status(self) -> str:
        if self.error:
            return self.error
        if not self.available:
            return "No releases published yet"
        if self.newer:
            return f"Version {self.available} is available"
        if _version_key(self.installed) > _version_key(self.available):
            return "You’re running a newer build than the latest release"
        return "You’re up to date"

    def to_cache(self) -> str:
        return json.dumps({'version': self.available, 'url': self.url}, separators=(',', ':'))


def cached_update(installed: str, raw: str) -> UpdateResult | None:
    """Restore only a small release record; recompute availability for this build."""
    try:
        if len(raw) > 8192:
            return None
        value = json.loads(raw)
        available, url = value['version'], value.get('url', '')
        if not isinstance(available, str) or not isinstance(url, str):
            return None
        newer = bool(available) and _version_key(available) > _version_key(installed)
        if url:
            url = ensure_web_url(url, "Release page")
        return UpdateResult(installed, available, newer, "", url)
    except (ValueError, TypeError, KeyError, UnsafeUrl):
        return None


def _version_key(value: str):
    """Numeric two/three-part versions; prerelease identifiers follow release ordering."""
    text = str(value).strip()
    if text[:1] in "vV":
        text = text[1:]
    identifier = r"[0-9A-Za-z-]+(?:\.[0-9A-Za-z-]+)*"
    match = re.fullmatch(r"(\d+)\.(\d+)(?:\.(\d+))?(?:-(" + identifier + r"))?(?:\+" + identifier + r")?", text) if len(text) <= 128 else None
    if match is None:
        raise ValueError("The version number must have two or three numeric parts.")
    major, minor, patch, suffix = match.groups()
    prerelease = tuple((0, int(part)) if part.isdigit() else (1, part) for part in (suffix or '').split('.'))
    return (int(major), int(minor), int(patch or 0), 0 if suffix else 1, prerelease)


def _release_status_sentence(status: int) -> str:
    """One plain sentence for a failed release request; the status code is logged, not shown."""
    if status in (403, 429):
        return "The release service is busy. Try again later."
    if status >= 500:
        return "The release service is unavailable right now. Try again later."
    return "The release service returned an unexpected answer."


def check_for_update(installed: str, session=None) -> UpdateResult:
    from requests.exceptions import RequestException
    if not RELEASES_API_URL:
        raise RuntimeError("No release service is configured for this distribution.")
    owned = session is None
    client = session if session is not None else make_session(retries=0, max_response_bytes=1024 * 1024, total_timeout=20)
    response = None
    try:
        response = client.get(RELEASES_API_URL, timeout=(5, 12))
        if response.status_code == 404:
            return UpdateResult(installed, "", False, "", "")
        if response.status_code >= 400:
            # The URL and the raw status line stay in the log; the user gets one sentence.
            logging.getLogger('bs_podcasts').warning('Release check failed with HTTP %s', response.status_code)
            return UpdateResult(installed, "", False, "", "", _release_status_sentence(response.status_code))
        payload = response.json()
    except (NetworkDeadline, RequestException) as exc:
        message = describe_network_error(exc, "the release service")
        logging.getLogger('bs_podcasts').warning('Release check unavailable: %s', message)
        return UpdateResult(installed, "", False, "", "", message)
    finally:
        if response is not None:
            response.close()
        if owned:
            client.close()
    if not isinstance(payload, dict):
        raise RuntimeError("The release service returned an invalid response.")
    available = str(payload.get("tag_name") or payload.get("name") or "").strip()
    if available[:1] in {"v", "V"}:
        available = available[1:]
    try:
        newer = _version_key(available) > _version_key(installed)
    except ValueError:
        raise RuntimeError("The release service returned an unreadable version number. Try again later.") from None
    notes = str(payload.get("body") or "").strip()
    url = str(payload.get("html_url") or "")
    if url:
        try:
            url = ensure_web_url(url, "Release page")
        except UnsafeUrl:
            url = ""
    return UpdateResult(installed, available, newer, notes, url)
