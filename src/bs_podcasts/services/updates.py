"""Small, read-only GitHub release checker. It never installs updates."""

from dataclasses import dataclass
import re

from ..config import RELEASES_API_URL
from ..net import make_session


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
    response = (session or make_session()).get(RELEASES_API_URL, timeout=(5, 12))
    if response.status_code == 404:
        # GitHub's latest-release API returns 404 both for "no release
        # published yet" and "repository not public". Either way there is
        # nothing to update to — that is an answer, not an error, and must
        # not surface as a raw "404 Client Error" or point users at a 404
        # release page.
        return UpdateResult(installed, "", False, "", "")
    response.raise_for_status()
    payload = response.json()
    available = str(payload.get("tag_name") or payload.get("name") or "").lstrip("v")
    if not available:
        raise RuntimeError("The latest release has no version number.")
    notes = str(payload.get("body") or "").strip()
    url = str(payload.get("html_url") or "")
    return UpdateResult(installed, available, _version_key(available) > _version_key(installed), notes, url)
