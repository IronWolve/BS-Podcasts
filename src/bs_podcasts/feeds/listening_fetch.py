"""Fetch and parse chapters and transcripts for one episode. No Qt, no DB."""

from dataclasses import dataclass
from html import unescape
from io import StringIO
import json
import logging
import re

from ..net import USER_AGENT, describe_network_error
from ..markup import TranscriptHTML, markup_text
from ..urlguard import UnsafeUrl, ensure_fetchable
from ..domain.times import media_seconds, media_interval


MAX_BYTES = 4 * 1024 * 1024
MAX_CHAPTERS = 5000
MAX_SEGMENTS = 50000
TIMEOUT = 15
_log = logging.getLogger(__name__)


def _bounded_entries(entries, maximum, label):
    """Bound row objects and database/UI work independently of the byte budget."""
    for index, entry in enumerate(entries):
        if index >= maximum:
            _log.warning("%s truncated at %s entries", label, maximum)
            break
        yield entry


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


def _get(url: str, session) -> tuple[bytes, str]:
    try:
        # Chapter/transcript URLs are feed-supplied; screen the first hop the
        # same way redirects and artwork already are.
        ensure_fetchable(url, "Chapters/transcript URL")
    except UnsafeUrl as exc:
        raise ListeningFetchError(str(exc)) from exc
    import requests

    try:
        with session.get(url, timeout=TIMEOUT, stream=True, headers={"User-Agent": USER_AGENT}) as response:
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


def fetch_chapters(url: str, session) -> list[ChapterData]:
    content, _content_type = _get(url, session)
    return parse_chapters(content)


def parse_chapters(content: bytes) -> list[ChapterData]:
    """Podcast Namespace JSON chapters: {"chapters": [{"startTime": 0, "title": ...}]}."""
    if len(content) > MAX_BYTES:
        raise ListeningFetchError("Chapters exceed the size limit.")
    entries = _json_entries(_decoded_text(content), 'chapters', 'Chapters')
    chapters = []
    for entry in _bounded_entries(entries or (), MAX_CHAPTERS, "Chapters"):
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


def fetch_transcript(url: str, declared_type: str, session) -> list[SegmentData]:
    content, content_type = _get(url, session)
    return parse_transcript(content, declared_type or content_type, url)


_TIMESTAMP = re.compile(r"(?<!\d)(?:(\d{1,6}):)?(\d{1,2}):(\d{2})[.,](\d{1,3})(?!\d)")
_TIMING_LINE = re.compile(r"^\s*(?:\d{1,6}:)?\d{1,2}:\d{2}[.,]\d{1,3}\s+-->\s+(?:\d{1,6}:)?\d{1,2}:\d{2}[.,]\d{1,3}(?:\s.*)?$", re.M)
_TAGS = re.compile(r"<[^<>]+>")
# A WebVTT NOTE or STYLE block: the keyword, then whitespace or the line end.
_NOTE_OR_STYLE_BLOCK = re.compile(r"(?:NOTE|STYLE)(?:\s|$)")
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
    parser = TranscriptHTML()
    parser.feed(value)
    parser.close()
    return "".join(parser.parts).strip()


def _decoded_text(content: bytes) -> str:
    return markup_text(content, "utf-8").replace("\r\n", "\n")


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
    for entry in _bounded_entries(entries or (), MAX_SEGMENTS, "Transcript segments"):
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
    for block in _iter_blocks(text):
        # A NOTE or STYLE block is not a cue, whatever lines it holds.
        first_text = 0
        while first_text < len(block) and block[first_text].isspace():
            first_text += 1
        if _NOTE_OR_STYLE_BLOCK.match(block, first_text):
            continue
        # A timing line starts a cue; the lines after it are its words. Lines
        # before the first timing line are a cue identifier or header, and a
        # block with no blank line between cues holds more than one cue.
        timing = None
        body = None
        start = 0
        while start <= len(block):
            end = block.find("\n", start)
            if end < 0:
                end = len(block)
            line = block[start:end].strip()
            if _TIMING_LINE.fullmatch(line):
                if timing is not None:
                    segment = _cue_segment(timing, body)
                    if segment is not None and len(segments) >= MAX_SEGMENTS:
                        _log.warning("Transcript cues truncated at %s entries", MAX_SEGMENTS)
                        return _merge_short(segments)
                    if segment is not None:
                        segments.append(segment)
                timing = line
                body = None
            elif timing is not None and line and not line.isdigit():
                if body is None:
                    body = StringIO()
                elif body.tell():
                    body.write(" ")
                body.write(line)
            if end == len(block):
                break
            start = end + 1
        if timing is not None:
            segment = _cue_segment(timing, body)
            if segment is not None and len(segments) >= MAX_SEGMENTS:
                _log.warning("Transcript cues truncated at %s entries", MAX_SEGMENTS)
                return _merge_short(segments)
            if segment is not None:
                segments.append(segment)
    return _merge_short(segments)


def _cue_segment(timing, body):
    if body is None:
        return None
    text = unescape(_TAGS.sub("", body.getvalue())).strip()
    if not text:
        return None
    start_text, _arrow, end_text = timing.partition("-->")
    return SegmentData(text=text, start_seconds=_stamp(start_text), end_seconds=_stamp(end_text))


def _iter_blocks(text):
    """Yield blank-line-delimited spans without materializing a split list."""
    start = 0
    for match in re.finditer(r"\n\s*\n", text):
        yield text[start:match.start()]
        start = match.end()
    yield text[start:]


def _parse_plain(text: str) -> list[SegmentData]:
    paragraphs = (part.strip() for part in _iter_blocks(text) if part.strip())
    first = next(paragraphs, None)
    second = next(paragraphs, None)
    if second is not None:
        from itertools import chain
        selected = chain((first, second), paragraphs)
    else:
        first = None
        # A single paragraph is treated as line-oriented text. Generate lines
        # from spans rather than str.split(), which allocates every line before
        # the segment cap can be applied.
        selected = (match.group(0).strip() for match in re.finditer(r"[^\n]+", text))
    return [SegmentData(text=part, start_seconds=_stamp(part[:16]))
            for part in _bounded_entries((part for part in selected if part), MAX_SEGMENTS, "Transcript paragraphs")]


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
