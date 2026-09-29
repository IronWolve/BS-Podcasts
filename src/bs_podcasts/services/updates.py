"""Small, read-only GitHub release checker. It never installs updates."""

from dataclasses import dataclass
import re

from ..config import RELEASES_API_URL
from ..net import make_session
from ..urlguard import ensure_web_url


@dataclass(frozen=True)
class UpdateResult:
    installed: str
    available: str  # "" means the project has no published release yet
    newer: bool
    notes: str
    url: str


def _version_key(value: str):
    return tuple(int(part) for part in re.findall(r"\d+", value)[:4])


def check_for_update(installed: str, session=None) -> UpdateResult:
    if not RELEASES_API_URL:
        raise RuntimeError("No release service is configured for this distribution.")
    owned = session is None
    client = session if session is not None else make_session(max_response_bytes=1024 * 1024, total_timeout=20)
    response = None
    try:
        response = client.get(RELEASES_API_URL, timeout=(5, 12))
        if response.status_code == 404:
            return UpdateResult(installed, "", False, "", "")
        response.raise_for_status()
        payload = response.json()
    finally:
        if response is not None:
            response.close()
        if owned:
            client.close()
    if not isinstance(payload, dict):
        raise RuntimeError("The release service returned an invalid response.")
    available = str(payload.get("tag_name") or payload.get("name") or "").lstrip("v")
    if not re.fullmatch(r"\d+\.\d+\.\d+(?:[-+][0-9A-Za-z.-]+)?", available):
        raise RuntimeError("The latest release has no valid version number.")
    notes = str(payload.get("body") or "").strip()
    url = str(payload.get("html_url") or "")
    if url:
        url = ensure_web_url(url, "Release page")
    return UpdateResult(installed, available, _version_key(available) > _version_key(installed), notes, url)
