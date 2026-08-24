"""Bounded RSS/Atom parsing with deliberately small output contracts."""

from email.utils import parsedate_to_datetime
from hashlib import sha256
import xml.etree.ElementTree as ET

from ..domain import FeedData, FeedEpisodeData


MAX_FEED_BYTES = 20 * 1024 * 1024
MAX_EPISODES = 5000
MIN_ENCLOSURE_BYTES = 16 * 1024


class FeedParseError(ValueError):
    pass


def _local(tag: str) -> str:
    return tag.rsplit("}", 1)[-1].rsplit(":", 1)[-1].lower()


def _children(element, name: str):
    wanted = name.lower()
    return [child for child in element if _local(child.tag) == wanted]


def _first(element, *names):
    wanted = {name.lower() for name in names}
    for child in element:
        if _local(child.tag) in wanted:
            return child
    return None


def _text(element, *names) -> str:
    child = _first(element, *names)
    if child is None:
        return ""
    return "".join(child.itertext()).strip()


def _date(value: str) -> str:
    value = value.strip()
    if not value:
        return ""
    try:
        return parsedate_to_datetime(value).isoformat()
    except (TypeError, ValueError, OverflowError):
        if "T" in value and len(value) >= 10:
            return value
        return ""


def _duration(value: str) -> int:
    value = value.strip()
    if not value:
        return 0
    try:
        if ":" not in value:
            return max(0, int(float(value)))
        parts = [int(part) for part in value.split(":")]
        if len(parts) == 2:
            return max(0, parts[0] * 60 + parts[1])
        if len(parts) == 3:
            return max(0, parts[0] * 3600 + parts[1] * 60 + parts[2])
    except (TypeError, ValueError):
        return 0
    return 0


def _artwork(element) -> str:
    for child in element:
        if _local(child.tag) == "image":
            href = child.attrib.get("href", "").strip()
            if href:
                return href
            url = _text(child, "url")
            if url:
                return url
    return ""


def _rss_enclosure(item) -> tuple[str, str]:
    for child in item:
        if _local(child.tag) not in {"enclosure", "content"}:
            continue
        url = child.attrib.get("url", "").strip()
        if not url:
            continue
        length = child.attrib.get("length", "").strip()
        if length:
            try:
                if 0 < int(length) < MIN_ENCLOSURE_BYTES:
                    continue
            except ValueError:
                pass
        mime = child.attrib.get("type", "").strip().lower()
        if mime and not mime.startswith("audio/"):
            continue
        return url, mime or "audio/*"
    return "", "audio/*"


def _transcript(element) -> tuple[str, str]:
    for child in element:
        if _local(child.tag) != "transcript":
            continue
        return (
            child.attrib.get("url", "").strip(),
            child.attrib.get("type", "").strip().lower(),
        )
    return "", ""


def _chapters(element) -> str:
    for child in element:
        if _local(child.tag) == "chapters":
            return child.attrib.get("url", "").strip()
    return ""


def _atom_link(element, relation: str) -> tuple[str, str]:
    for link in _children(element, "link"):
        if link.attrib.get("rel", "alternate") != relation:
            continue
        href = link.attrib.get("href", "").strip()
        if not href:
            continue
        mime = link.attrib.get("type", "").strip().lower()
        if relation == "enclosure" and mime and not mime.startswith("audio/"):
            continue
        return href, mime or ("audio/*" if relation == "enclosure" else "")
    return "", ""


def _external_id(title: str, published: str, media_url: str, explicit: str) -> str:
    if explicit.strip():
        return explicit.strip()
    stable = "\n".join((title.strip(), published.strip(), media_url.strip()))
    return sha256(stable.encode("utf-8")).hexdigest()


def _parse_rss(root) -> FeedData:
    channel = root if _local(root.tag) == "channel" else _first(root, "channel")
    if channel is None:
        raise FeedParseError("RSS feed has no channel.")

    episodes = []
    seen = set()
    for item in _children(channel, "item")[:MAX_EPISODES]:
        title = _text(item, "title") or "Untitled episode"
        media_url, mime = _rss_enclosure(item)
        if not media_url:
            continue
        published = _date(_text(item, "pubdate", "published", "date"))
        external = _external_id(title, published, media_url, _text(item, "guid", "id"))
        if external in seen:
            continue
        seen.add(external)
        transcript_url, transcript_type = _transcript(item)
        chapters_url = _chapters(item)
        item_artwork = _artwork(item)
        episodes.append(
            FeedEpisodeData(
                external_id=external,
                title=title,
                description=_text(item, "description", "summary", "encoded"),
                media_url=media_url,
                mime_type=mime,
                published_at=published,
                duration_seconds=_duration(_text(item, "duration")),
                transcript_url=transcript_url,
                transcript_type=transcript_type,
            )
        )

    return FeedData(
        title=_text(channel, "title") or "Untitled podcast",
        author=_text(channel, "author", "managingeditor"),
        description=_text(channel, "description", "subtitle"),
        website_url=_text(channel, "link"),
        artwork_url=_artwork(channel),
        episodes=tuple(episodes),
    )


def _parse_atom(root) -> FeedData:
    episodes = []
    seen = set()
    for entry in _children(root, "entry")[:MAX_EPISODES]:
        title = _text(entry, "title") or "Untitled episode"
        media_url, mime = _atom_link(entry, "enclosure")
        if not media_url:
            continue
        published = _date(_text(entry, "published", "updated"))
        external = _external_id(title, published, media_url, _text(entry, "id", "guid"))
        if external in seen:
            continue
        seen.add(external)
        transcript_url, transcript_type = _transcript(entry)
        chapters_url = _chapters(entry)
        item_artwork = _artwork(entry)
        episodes.append(
            FeedEpisodeData(
                external_id=external,
                title=title,
                description=_text(entry, "summary", "content"),
                media_url=media_url,
                mime_type=mime,
                published_at=published,
                duration_seconds=_duration(_text(entry, "duration")),
                transcript_url=transcript_url,
                transcript_type=transcript_type,
                chapters_url=chapters_url,
                artwork_url=item_artwork,
            )
        )

    website_url, _mime = _atom_link(root, "alternate")
    author = _first(root, "author")
    return FeedData(
        title=_text(root, "title") or "Untitled podcast",
        author=_text(author, "name") if author is not None else "",
        description=_text(root, "subtitle"),
        website_url=website_url,
        artwork_url=_text(root, "logo", "icon"),
        episodes=tuple(episodes),
    )


def parse_feed(content: bytes) -> FeedData:
    if not content:
        raise FeedParseError("Feed response was empty.")
    if len(content) > MAX_FEED_BYTES:
        raise FeedParseError("Feed exceeds the size limit.")
    upper = content[:4096].upper()
    if b"<!DOCTYPE" in upper or b"<!ENTITY" in upper:
        raise FeedParseError("DTD and entity declarations are not allowed.")
    try:
        root = ET.fromstring(content)
    except ET.ParseError as exc:
        raise FeedParseError(f"Invalid XML: {exc}") from exc

    kind = _local(root.tag)
    if kind in {"rss", "rdf", "channel"}:
        return _parse_rss(root)
    if kind == "feed":
        return _parse_atom(root)
    raise FeedParseError(f"Unsupported feed root: {kind or 'unknown'}.")
