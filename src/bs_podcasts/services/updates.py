"""Small, read-only GitHub release checker. It never installs updates."""

from dataclasses import dataclass
import re

from ..config import RELEASES_API_URL
from ..net import make_session


@dataclass(frozen=True)
class UpdateResult:
    installed: str
    available: str
    newer: bool
    notes: str
    url: str


def _version_key(value: str):
    return tuple(int(part) for part in re.findall(r"\d+", value)[:4])


def check_for_update(installed: str, session=None) -> UpdateResult:
    response = (session or make_session()).get(RELEASES_API_URL, timeout=(5, 12))
    response.raise_for_status()
    payload = response.json()
    available = str(payload.get("tag_name") or payload.get("name") or "").lstrip("v")
    if not available:
        raise RuntimeError("The latest release has no version number.")
    notes = str(payload.get("body") or "").strip()
    url = str(payload.get("html_url") or "")
    return UpdateResult(installed, available, _version_key(available) > _version_key(installed), notes, url)
