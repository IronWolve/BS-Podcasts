"""Fetch and parse chapters and transcripts for one episode. No Qt, no DB."""

from dataclasses import dataclass
import json
import re

import requests

from ..net import USER_AGENT, make_session


MAX_BYTES = 4 * 1024 * 1024
TIMEOUT = 15


class ListeningFetchError(RuntimeError):
    pass


@dataclass(frozen=True)
class ChapterData:
    start_seconds: float
    title: str
    end_seconds: float | None = None
    artwork_url: str = ""


@dataclass(frozen=True)
class SegmentData:
    text: str
    start_seconds: float | None = None
    end_seconds: float | None = None


def _get(url: str, session=None) -> tuple[bytes, str]:
    client = session or make_session()
    with client.get(url, timeout=TIMEOUT, stream=True, headers={"User-Agent": USER_AGENT}) as response:
        response.raise_for_status()
        content = bytearray()
        for chunk in response.iter_content(64 * 1024):
            content.extend(chunk)
            if len(content) > MAX_BYTES:
                raise ListeningFetchError("File exceeds the size limit.")
        return bytes(content), response.headers.get("Content-Type", "").split(";")[0].strip().lower()


def fetch_chapters(url: str, session=None) -> list[ChapterData]:
    content, _content_type = _get(url, session)
    return parse_chapters(content)


def parse_chapters(content: bytes) -> list[ChapterData]:
    """Podcast Namespace JSON chapters: {"chapters": [{"startTime": 0, "title": ...}]}."""
    try:
        payload = json.loads(content.decode("utf-8", "replace"))
    except ValueError as exc:
        raise ListeningFetchError(f"Chapters are not valid JSON: {exc}") from exc
    entries = payload.get("chapters") if isinstance(payload, dict) else payload
    chapters = []
    for entry in entries or ():
        if not isinstance(entry, dict):
            continue
        try:
            start = float(entry.get("startTime", 0))
        except (TypeError, ValueError):
            continue
        end = entry.get("endTime")
        chapters.append(
            ChapterData(
                start_seconds=max(0.0, start),
                title=str(entry.get("title") or "").strip(),
                end_seconds=float(end) if isinstance(end, (int, float)) else None,
                artwork_url=str(entry.get("img") or ""),
            )
        )
    chapters.sort(key=lambda chapter: chapter.start_seconds)
    return chapters


def fetch_transcript(url: str, declared_type: str = "", session=None) -> list[SegmentData]:
    content, content_type = _get(url, session)
    return parse_transcript(content, declared_type or content_type, url)


_TIMESTAMP = re.compile(r"(?:(\d+):)?(\d{1,2}):(\d{2})[.,](\d{1,3})")
_TAGS = re.compile(r"<[^>]+>")


def _stamp(text: str) -> float | None:
    match = _TIMESTAMP.search(text)
    if not match:
        return None
    hours, minutes, seconds, millis = match.groups()
    return int(hours or 0) * 3600 + int(minutes) * 60 + int(seconds) + int(millis.ljust(3, "0")) / 1000


def parse_transcript(content: bytes, kind: str = "", url: str = "") -> list[SegmentData]:
    text = content.decode("utf-8", "replace").replace("\r\n", "\n")
    kind = (kind or "").lower()
    lowered_url = url.lower()
    if "json" in kind or lowered_url.endswith(".json"):
        return _parse_json_transcript(text)
    if "vtt" in kind or "srt" in kind or "subrip" in kind or lowered_url.endswith((".vtt", ".srt")) or "-->" in text[:2000]:
        return _parse_cues(text)
    if "html" in kind:
        text = _TAGS.sub(" ", text)
    return _parse_plain(text)


def _parse_json_transcript(text: str) -> list[SegmentData]:
    try:
        payload = json.loads(text)
    except ValueError as exc:
        raise ListeningFetchError(f"Transcript is not valid JSON: {exc}") from exc
    entries = payload.get("segments") if isinstance(payload, dict) else payload
    segments = []
    for entry in entries or ():
        if not isinstance(entry, dict):
            continue
        body = str(entry.get("body") or entry.get("text") or "").strip()
        if not body:
            continue
        start = entry.get("startTime")
        end = entry.get("endTime")
        segments.append(
            SegmentData(
                text=body,
                start_seconds=float(start) if isinstance(start, (int, float)) else None,
                end_seconds=float(end) if isinstance(end, (int, float)) else None,
            )
        )
    return _merge_short(segments)


def _parse_cues(text: str) -> list[SegmentData]:
    segments = []
    for block in re.split(r"\n\s*\n", text.strip()):
        lines = [line.strip() for line in block.split("\n") if line.strip()]
        if not lines:
            continue
        timing = next((line for line in lines if "-->" in line), None)
        if timing is None:
            continue
        start_text, _arrow, end_text = timing.partition("-->")
        body_lines = [line for line in lines if line is not timing and not line.isdigit() and not line.startswith(("WEBVTT", "NOTE", "STYLE"))]
        body = _TAGS.sub("", " ".join(body_lines)).strip()
        if body:
            segments.append(SegmentData(text=body, start_seconds=_stamp(start_text), end_seconds=_stamp(end_text)))
    return _merge_short(segments)


def _parse_plain(text: str) -> list[SegmentData]:
    paragraphs = [para.strip() for para in re.split(r"\n\s*\n", text) if para.strip()]
    if len(paragraphs) <= 1:
        paragraphs = [line.strip() for line in text.split("\n") if line.strip()]
    return [SegmentData(text=para, start_seconds=_stamp(para[:16])) for para in paragraphs]


def _merge_short(segments: list[SegmentData], target_chars: int = 180) -> list[SegmentData]:
    """Caption cues are a few words each; merge them into readable paragraphs."""
    merged = []
    buffer = None
    for segment in segments:
        if buffer is None:
            buffer = segment
            continue
        if len(buffer.text) < target_chars and not buffer.text.endswith((".", "?", "!")):
            buffer = SegmentData(text=f"{buffer.text} {segment.text}", start_seconds=buffer.start_seconds, end_seconds=segment.end_seconds)
        else:
            merged.append(buffer)
            buffer = segment
    if buffer is not None:
        merged.append(buffer)
    return merged
