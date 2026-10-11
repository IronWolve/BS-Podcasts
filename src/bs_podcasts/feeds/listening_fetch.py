"""Fetch and parse chapters and transcripts for one episode. No Qt, no DB."""

from dataclasses import dataclass
from html import unescape
from html.parser import HTMLParser
import json
import re

from ..net import USER_AGENT, describe_network_error, make_session
from ..urlguard import UnsafeUrl, ensure_fetchable
from ..domain.times import media_seconds, media_interval


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
    try:
        # Chapter/transcript URLs are feed-supplied; screen the first hop the
        # same way redirects and artwork already are.
        ensure_fetchable(url, "Chapters/transcript URL")
    except UnsafeUrl as exc:
        raise ListeningFetchError(str(exc)) from exc
    import requests

    # A session this call creates is closed here; a caller's session is left open.
    owned = session is None
    client = make_session() if owned else session
    try:
        with client.get(url, timeout=TIMEOUT, stream=True, headers={"User-Agent": USER_AGENT}) as response:
            response.raise_for_status()
            content = bytearray()
            for chunk in response.iter_content(64 * 1024):
                content.extend(chunk)
                if len(content) > MAX_BYTES:
                    raise ListeningFetchError("File exceeds the size limit.")
            return bytes(content), response.headers.get("Content-Type", "").split(";")[0].strip().lower()
    except requests.RequestException as exc:
        # Same sentence-a-person-can-act-on rule as feeds and downloads.
        raise ListeningFetchError(describe_network_error(exc, "the chapters/transcript file")) from exc
    finally:
        if owned:
            client.close()


def fetch_chapters(url: str, session=None) -> list[ChapterData]:
    content, _content_type = _get(url, session)
    return parse_chapters(content)


def parse_chapters(content: bytes) -> list[ChapterData]:
    """Podcast Namespace JSON chapters: {"chapters": [{"startTime": 0, "title": ...}]}."""
    if len(content) > MAX_BYTES:
        raise ListeningFetchError("Chapters exceed the size limit.")
    entries = _json_entries(_decoded_text(content), 'chapters', 'Chapters')
    chapters = []
    for entry in entries or ():
        if not isinstance(entry, dict):
            continue
        start, end = media_interval(entry.get("startTime", 0), entry.get("endTime"))
        if start is None:
            continue
        chapters.append(
            ChapterData(
                start_seconds=start,
                title=_chapter_title(str(entry.get("title") or "").strip()),
                end_seconds=end,
                artwork_url=str(entry.get("img") or ""),
            )
        )
    chapters.sort(key=lambda chapter: chapter.start_seconds)
    return chapters


def fetch_transcript(url: str, declared_type: str = "", session=None) -> list[SegmentData]:
    content, content_type = _get(url, session)
    return parse_transcript(content, declared_type or content_type, url)


_TIMESTAMP = re.compile(r"(?<!\d)(?:(\d{1,6}):)?(\d{1,2}):(\d{2})[.,](\d{1,3})(?!\d)")
_TIMING_LINE = re.compile(r"^\s*(?:\d{1,6}:)?\d{1,2}:\d{2}[.,]\d{1,3}\s+-->\s+(?:\d{1,6}:)?\d{1,2}:\d{2}[.,]\d{1,3}(?:\s.*)?$", re.M)
_TAGS = re.compile(r"<[^<>]+>")
_SKIP_HTML = {"script", "style"}


class _TranscriptHTML(HTMLParser):
    def __init__(self):
        super().__init__(convert_charrefs=True)
        self._skip = []
        self.parts = []

    def handle_starttag(self, tag, attrs):
        name = tag.lower()
        if name in _SKIP_HTML:
            self._skip.append(name)
        elif not self._skip and name in {"br", "p", "div", "li", "tr"}:
            self.parts.append(" ")

    def handle_endtag(self, tag):
        name = tag.lower()
        if self._skip and name == self._skip[-1]:
            self._skip.pop()
        elif not self._skip and name in {"p", "div", "li", "tr"}:
            self.parts.append(" ")

    def handle_data(self, data):
        if not self._skip:
            self.parts.append(data)


def _html_text(value: str) -> str:
    # HTMLParser intentionally treats script/style as raw text. Recover a
    # malformed missing close only at a subsequent block tag, discarding the
    # script/style body rather than displaying it as transcript prose.
    def repair(match):
        name = match[1]
        tail = match[2]
        if re.search(rf"</{name}\s*>$", match[0], re.I):
            return ""
        block = re.search(r"<(?:p|div|section|article|h[1-6]|li|table)\b", tail, re.I)
        return tail[block.start():] if block else ""
    value = re.sub(r"<(script|style)\b[^>]*>(.*?)(?:</\1\s*>|$)", repair, value, flags=re.I | re.S)
    parser = _TranscriptHTML()
    parser.feed(value)
    parser.close()
    return "".join(parser.parts).strip()


def _decoded_text(content: bytes) -> str:
    from .fetch import _markup_text
    return _markup_text(content, "utf-8").replace("\r\n", "\n")


def _chapter_title(text: str) -> str:
    maximum = 500
    return text if len(text) <= maximum else text[:maximum - 1].rstrip() + "…"


def _stamp(text: str) -> float | None:
    match = _TIMESTAMP.search(text)
    if not match:
        return None
    hours, minutes, seconds, millis = match.groups()
    try:
        return media_seconds(int(hours or 0) * 3600 + int(minutes) * 60 + int(seconds) + int(millis.ljust(3, "0")) / 1000)
    except (ValueError, OverflowError):
        return None


def parse_transcript(content: bytes, kind: str = "", url: str = "") -> list[SegmentData]:
    if len(content) > MAX_BYTES:
        raise ListeningFetchError("Transcript exceeds the size limit.")
    text = _decoded_text(content)
    kind = (kind or "").lower()
    lowered_url = url.lower()
    if "json" in kind or lowered_url.endswith(".json"):
        return _parse_json_transcript(text)
    if "vtt" in kind or "srt" in kind or "subrip" in kind or lowered_url.endswith((".vtt", ".srt")) or _TIMING_LINE.search(text[:2000]):
        segments = _parse_cues(text)
        if segments:
            return segments
    if "html" in kind:
        text = _html_text(text)
    return _parse_plain(text)


def _json_entries(text: str, field: str, label: str) -> list:
    try:
        payload = json.loads(text)
    except (ValueError, RecursionError) as exc:
        raise ListeningFetchError(f"{label} are not valid JSON.") from exc
    entries = payload.get(field) if isinstance(payload, dict) else payload
    if entries is None:
        return []
    if not isinstance(entries, list):
        raise ListeningFetchError(f"{label} must contain a list of entries.")
    return entries


def _parse_json_transcript(text: str) -> list[SegmentData]:
    entries = _json_entries(text, 'segments', 'Transcript segments')
    segments = []
    for entry in entries or ():
        if not isinstance(entry, dict):
            continue
        body = str(entry.get("body") or entry.get("text") or "").strip()
        if not body:
            continue
        start, end = media_interval(entry.get("startTime"), entry.get("endTime"))
        segments.append(
            SegmentData(
                text=body,
                start_seconds=start,
                end_seconds=end,
            )
        )
    return _merge_short(segments)


def _parse_cues(text: str) -> list[SegmentData]:
    segments = []
    for block in re.split(r"\n\s*\n", text.strip()):
        # A timing line starts a cue; the lines after it are its words. Lines
        # before the first timing line are a cue identifier or header, and a
        # block with no blank line between cues holds more than one cue.
        cues = []
        for line in (line.strip() for line in block.split("\n")):
            if _TIMING_LINE.fullmatch(line):
                cues.append((line, []))
            elif cues and line and not line.isdigit() and not line.startswith(("WEBVTT", "NOTE", "STYLE")):
                cues[-1][1].append(line)
        for timing, body_lines in cues:
            start_text, _arrow, end_text = timing.partition("-->")
            body = unescape(_TAGS.sub("", " ".join(body_lines))).strip()
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
