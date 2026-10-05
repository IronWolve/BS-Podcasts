"""Bounded RSS/Atom parsing with deliberately small output contracts."""

from collections import deque
from email.utils import parsedate_to_datetime
from hashlib import sha256
from io import BytesIO
import xml.etree.ElementTree as ET

from ..domain import FeedData, FeedEpisodeData
from ..domain.media import BINARY_AUDIO_TYPES
from .safety import UnsupportedEncoding, contains_dtd, strip_leading_space


MAX_FEED_BYTES = 20 * 1024 * 1024
MAX_EPISODES = 5000
# _cap_newest keeps the newest MAX_EPISODES *of what was materialized*; this
# bounds materialization itself, so a byte-capped feed of hundreds of
# thousands of minimal items cannot balloon memory building dataclasses that
# are about to be thrown away. Generous on purpose: at 4x the keep-cap, any
# feed it truncates is pathological, not a real back catalogue.
MAX_SCANNED_EPISODES = MAX_EPISODES * 4
# A flat flood is rejected while it is parsed, not after the tree exists.
MAX_FEED_ELEMENTS = 1_000_000
# Elements attached to the tree at once: the feed's own fields plus the
# item being read. Unread branches are dropped as they close, so only a
# flood inside a kept field or item reaches this. Real feeds keep a few
# hundred; long inline XHTML show notes stay far below it.
MAX_KEPT_ELEMENTS = 20_000
MIN_ENCLOSURE_BYTES = 16 * 1024
# Titles are shown in every list; one with megabytes of text would cost every
# layout pass. Episode identity is taken from the full title before this cap.
MAX_TITLE_CHARS = 500


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


def _clamp_title(text: str) -> str:
    if len(text) <= MAX_TITLE_CHARS:
        return text
    return text[:MAX_TITLE_CHARS - 1].rstrip() + "\u2026"


# Non-audio enclosures seen while parsing the current feed (per thread).
import threading as _threading

_skipped_local = _threading.local()


class _SkippedList:
    def append(self, mime):
        getattr(_skipped_local, "items", None) is None and setattr(_skipped_local, "items", [])
        _skipped_local.items.append(mime)

    def take(self) -> int:
        items = getattr(_skipped_local, "items", None) or []
        _skipped_local.items = []
        return len(items)


_SKIPPED = _SkippedList()


def _date(value: str) -> str:
    value = value.strip()
    if not value:
        return ""
    try:
        parsed = parsedate_to_datetime(value)
    except (TypeError, ValueError, OverflowError):
        parsed = None
    if (
        parsed is None
        and len(value) >= 10
        and value[4:5] == "-"
        and value[7:8] == "-"
        and value[:4].isdigit()
        and value[5:7].isdigit()
        and value[8:10].isdigit()
    ):
        try:
            from datetime import datetime
            parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
        except ValueError:
            return ""
    if parsed is None:
        return ""
    # Stored text is compared lexically by SQLite; only a normalized UTC
    # representation makes that ordering chronological across feeds that mix
    # timezone offsets.
    if parsed.tzinfo is not None:
        from datetime import timezone
        try:
            parsed = parsed.astimezone(timezone.utc).replace(tzinfo=None)
        except (ValueError, OverflowError):
            return ""
    return parsed.isoformat()


def _duration(value: str) -> int:
    import math
    try:
        value = (value or "").strip()
        if not value:
            return 0
        if ":" not in value:
            seconds = float(value)
        else:
            raw = value.split(":")
            if len(raw) not in {2, 3}:
                return 0
            parts = [int(part) for part in raw[:-1]]
            parts.append(float(raw[-1]))
            seconds = sum(part * 60**index for index, part in enumerate(reversed(parts)))
        return int(seconds) if math.isfinite(seconds) and 0 <= seconds <= 2**31 else 0
    except (TypeError, ValueError, OverflowError):
        return 0


def _integer(value: str) -> int | None:
    try:
        number = int(value.strip())
    except (TypeError, ValueError):
        return None
    # Season/episode numbers; a feed shipping a 40-digit "number" would
    # otherwise travel to SQLite and overflow its 8-byte INTEGER on insert.
    return number if 0 <= number <= 2**31 else None


def _explicit(element) -> bool | None:
    value = _text(element, "explicit").strip().lower()
    if value in {"yes", "true", "explicit", "1"}:
        return True
    if value in {"no", "false", "clean", "0"}:
        return False
    return None


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


def _categories(element) -> tuple[str, ...]:
    """Return useful leaf categories from RSS/iTunes or Atom metadata."""
    values = []
    seen = set()

    def add(value: str):
        value = " ".join((value or "").split())
        if value and value.casefold() not in seen:
            seen.add(value.casefold())
            values.append(value)

    def visit(node, depth: int = 0):
        # Real feeds nest one or two levels; a hostile feed nesting
        # thousands must not raise RecursionError and take the whole parse
        # down with it. Past the cap the node is treated as a leaf.
        nested = _children(node, "category") if depth < 8 else ()
        if nested:
            for child in nested:
                visit(child, depth + 1)
        else:
            add(
                node.attrib.get("text", "")
                or node.attrib.get("term", "")
                or "".join(node.itertext()).strip()
            )

    for category in _children(element, "category"):
        visit(category)
    return tuple(values)


def _rss_link(element) -> str:
    """Text RSS links plus alternate-style links used by some hybrid feeds."""
    for child in _children(element, "link"):
        value = "".join(child.itertext()).strip()
        if value:
            return value
        if child.attrib.get("rel", "alternate").lower() == "alternate":
            href = child.attrib.get("href", "").strip()
            if href:
                return href
    return ""


def _rss_enclosure(item) -> tuple[str, str, int]:
    """Prefer an RSS enclosure. A typeless media:content is the last resort."""
    typed_audio = None
    typeless = None
    for child in item:
        local = _local(child.tag)
        if local not in {"enclosure", "content"}:
            continue
        attributes = {_local(key): value for key, value in child.attrib.items()}
        url = (attributes.get("url") or attributes.get("resource") or "").strip()
        if not url:
            continue
        length = attributes.get("length", "").strip()
        mime = attributes.get("type", "").split(";", 1)[0].strip().lower()
        if mime and not mime.startswith("audio/") and mime not in BINARY_AUDIO_TYPES:
            if mime.startswith("video/"):
                _SKIPPED.append(mime)
            continue
        candidate = (url, mime or "audio/*", _integer(length) or 0)
        if local == "enclosure":
            return candidate
        if mime:
            if typed_audio is None:
                typed_audio = candidate
        elif typeless is None:
            typeless = candidate
    return typed_audio or typeless or ("", "audio/*", 0)


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


def _atom_link(element, relation: str) -> tuple[str, str, int]:
    for link in _children(element, "link"):
        if link.attrib.get("rel", "alternate") != relation:
            continue
        href = link.attrib.get("href", "").strip()
        if not href:
            continue
        mime = link.attrib.get("type", "").split(";", 1)[0].strip().lower()
        if relation == "enclosure" and mime and not mime.startswith("audio/") and mime not in BINARY_AUDIO_TYPES:
            if mime.startswith("video/"):
                _SKIPPED.append(mime)
            continue
        return (
            href,
            mime or ("audio/*" if relation == "enclosure" else ""),
            _integer(link.attrib.get("length", "")) or 0,
        )
    return "", "", 0


def _external_id(title: str, published: str, media_url: str, explicit: str) -> str:
    if explicit.strip():
        return explicit.strip()
    stable = "\n".join((title.strip(), published.strip(), media_url.strip()))
    return sha256(stable.encode("utf-8")).hexdigest()


def _rss_episode(item) -> FeedEpisodeData | None:
    title = _text(item, "title") or "Untitled episode"
    media_url, mime, enclosure_bytes = _rss_enclosure(item)
    if not media_url:
        return None
    published = _date(_text(item, "pubdate", "published", "date"))
    external = _external_id(title, published, media_url, _text(item, "guid", "id") or item.attrib.get("{http://www.w3.org/1999/02/22-rdf-syntax-ns#}about", ""))
    transcript_url, transcript_type = _transcript(item)
    chapters_url = _chapters(item)
    return FeedEpisodeData(
        external_id=external,
        title=_clamp_title(title),
        description=_text(item, "description", "summary", "encoded"),
        media_url=media_url,
        mime_type=mime,
        published_at=published,
        duration_seconds=_duration(_text(item, "duration")),
        transcript_url=transcript_url,
        transcript_type=transcript_type,
        chapters_url=chapters_url,
        artwork_url=_artwork(item),
        website_url=_rss_link(item),
        author=_text(item, "author", "creator"),
        season_number=_integer(_text(item, "season")),
        episode_number=_integer(_text(item, "episode")),
        episode_type=_text(item, "episodetype").lower(),
        explicit=_explicit(item),
        enclosure_bytes=enclosure_bytes,
    )


def _parse_rss(root, episodes) -> FeedData:
    channel = root if _local(root.tag) == "channel" else _first(root, "channel")
    if channel is None:
        raise FeedParseError("RSS feed has no channel.")

    return FeedData(
        title=_clamp_title(_text(channel, "title") or "Untitled podcast"),
        author=_text(channel, "author", "managingeditor"),
        description=_text(channel, "description", "subtitle"),
        website_url=_rss_link(channel),
        artwork_url=_artwork(channel),
        categories=_categories(channel),
        episodes=tuple(_cap_newest(episodes)),
        truncated=len(episodes) > MAX_EPISODES,
        skipped_video=_SKIPPED.take(),
    )



def _cap_newest(episodes: list) -> list:
    """Cap a mega-feed at MAX_EPISODES keeping the NEWEST by date.

    The old document-order slice meant an oldest-first feed could never
    persist its new episodes. Document order is preserved among the kept
    entries so ordinary feeds are untouched."""
    if len(episodes) <= MAX_EPISODES:
        return episodes
    ranked = sorted(
        range(len(episodes)),
        key=lambda index: (episodes[index].published_at or "", -index),
        reverse=True,
    )[:MAX_EPISODES]
    return [episodes[index] for index in sorted(ranked)]

def _atom_episode(entry) -> FeedEpisodeData | None:
    title = _text(entry, "title") or "Untitled episode"
    media_url, mime, enclosure_bytes = _atom_link(entry, "enclosure")
    if not media_url:
        return None
    published = _date(_text(entry, "published", "updated"))
    external = _external_id(title, published, media_url, _text(entry, "id", "guid"))
    transcript_url, transcript_type = _transcript(entry)
    chapters_url = _chapters(entry)
    website_url, _website_mime, _website_size = _atom_link(entry, "alternate")
    author = _first(entry, "author")
    return FeedEpisodeData(
        external_id=external,
        title=_clamp_title(title),
        description=_text(entry, "summary", "content"),
        media_url=media_url,
        mime_type=mime,
        published_at=published,
        duration_seconds=_duration(_text(entry, "duration")),
        transcript_url=transcript_url,
        transcript_type=transcript_type,
        chapters_url=chapters_url,
        artwork_url=_artwork(entry),
        website_url=website_url,
        author=(_text(author, "name") if author is not None else "") or _text(entry, "creator", "author"),
        season_number=_integer(_text(entry, "season")),
        episode_number=_integer(_text(entry, "episode")),
        episode_type=_text(entry, "episodetype").lower(),
        explicit=_explicit(entry),
        enclosure_bytes=enclosure_bytes,
    )


def _parse_atom(root, episodes) -> FeedData:
    website_url, _mime, _size = _atom_link(root, "alternate")
    author = _first(root, "author")
    return FeedData(
        title=_clamp_title(_text(root, "title") or "Untitled podcast"),
        author=_text(author, "name") if author is not None else "",
        description=_text(root, "subtitle"),
        website_url=website_url,
        artwork_url=_text(root, "logo", "icon"),
        categories=_categories(root),
        episodes=tuple(_cap_newest(episodes)),
        truncated=len(episodes) > MAX_EPISODES,
        skipped_video=_SKIPPED.take(),
    )


XML_BASE = "{http://www.w3.org/XML/1998/namespace}base"
_URL_ATTRIBUTES = ("url", "href", "src", "{http://www.w3.org/1999/02/22-rdf-syntax-ns#}resource", "{http://www.w3.org/1999/02/22-rdf-syntax-ns#}about")
_URL_TEXT_TAGS = {"link", "url"}


def _is_relative(value: str) -> bool:
    return bool(value) and "://" not in value.split("?", 1)[0][:12]


def _url_budget():
    budget = 0

    def bounded(value):
        nonlocal budget
        budget += len(value)
        if len(value) > 8192 or budget > 8 * 1024 * 1024:
            raise FeedParseError("Feed URL expansion exceeds the safety limit.")
        return value

    return bounded


def _next_base(element, base, under, bounded):
    from urllib.parse import urljoin
    own = element.attrib.get(XML_BASE, "").strip()
    if not own:
        return base, under
    if len(own) > 8192:
        raise FeedParseError("Feed base URL exceeds the safety limit.")
    return bounded(urljoin(base, own) if base else own), True


def _rewrite_attributes(element, base, under, bounded):
    if not (under and base):
        return
    from urllib.parse import urljoin
    for name in _URL_ATTRIBUTES:
        value = (element.attrib.get(name) or "").strip()
        if _is_relative(value):
            element.set(name, bounded(urljoin(base, value)))


def _rewrite_text(element, base, under, bounded):
    if not (under and base) or _local(element.tag) not in _URL_TEXT_TAGS:
        return
    from urllib.parse import urljoin
    text = (element.text or "").strip()
    if _is_relative(text):
        element.text = bounded(urljoin(base, text))


def _apply_xml_base(root, document_base: str):
    """Absolutize URL-bearing nodes against their nearest xml:base.

    Atom permits xml:base on any element, inherited downward; resolving only
    against the document URL mis-resolved every URL under one. Runs before
    the parse proper — resolve_feed_urls then finds those values already
    absolute and leaves them alone. Iterative on purpose: recursion here
    would reintroduce the nesting-depth hazard the category walker just had
    fixed."""
    # `under` is True once any ancestor (or the element itself) declared
    # xml:base — inheritance is the whole point. Elements not under one are
    # left for resolve_feed_urls, exactly as before.
    stack = [(root, document_base, False, 0)]
    bounded = _url_budget()
    while stack:
        element, base, under, depth = stack.pop()
        if depth > 128:
            raise FeedParseError("Feed nesting exceeds the safety limit.")
        base, under = _next_base(element, base, under, bounded)
        _rewrite_attributes(element, base, under, bounded)
        _rewrite_text(element, base, under, bounded)
        for child in element:
            stack.append((child, base, under, depth + 1))


_STRUCTURAL = {"rss", "rdf", "channel", "feed"}
_METADATA = {
    "title", "author", "managingeditor", "description", "subtitle", "summary",
    "encoded", "link", "image", "category", "logo", "icon", "name", "creator", "url",
}


def _remember_episode(head, tail, seen, episode):
    if episode is None or episode.external_id in seen:
        return
    seen.add(episode.external_id)
    if len(head) < MAX_SCANNED_EPISODES // 2:
        head.append(episode)
    else:
        tail.append(episode)


def _stream_feed(content: bytes, base_url: str):
    """Parse without keeping every element. Items are dropped once read.

    The kept episodes are the head and the tail of the document, so an
    oldest-first feed still has its newest items when the scan cap is hit.

    A frame is [element, base, under, unread, size]. `unread` marks an
    element dropped unread when it closes: a non-field child of
    rss/channel/feed and everything inside one, except an item or entry.
    `size` counts the frame's still-attached subtree for MAX_KEPT_ELEMENTS.
    """
    bounded = _url_budget()
    head = []
    tail = deque(maxlen=MAX_SCANNED_EPISODES - MAX_SCANNED_EPISODES // 2)
    seen = set()
    stack = []
    root = None
    root_kind = ""
    count = 0
    kept = 0
    source = BytesIO(content)

    def episode_tag(local):
        return (local == "item" and root_kind in {"rss", "rdf", "channel"}) or (
            local == "entry" and root_kind == "feed"
        )

    try:
        for event, element in ET.iterparse(source, events=("start", "end")):
            if event == "start":
                count += 1
                kept += 1
                if count > MAX_FEED_ELEMENTS:
                    raise FeedParseError("Feed element count exceeds the safety limit.")
                if kept > MAX_KEPT_ELEMENTS:
                    raise FeedParseError("Feed item or channel field exceeds the element safety limit.")
                if len(stack) > 128:
                    raise FeedParseError("Feed nesting exceeds the safety limit.")
                if root is None:
                    root = element
                    root_kind = _local(element.tag)
                parent_base, parent_under = (base_url, False) if not stack else (stack[-1][1], stack[-1][2])
                base, under = _next_base(element, parent_base, parent_under, bounded)
                _rewrite_attributes(element, base, under, bounded)
                local = _local(element.tag)
                if not stack or episode_tag(local):
                    unread = False
                elif (
                    _local(stack[-1][0].tag) in _STRUCTURAL
                    and local not in _METADATA
                    and local not in _STRUCTURAL
                ):
                    unread = True
                else:
                    unread = stack[-1][3]
                stack.append([element, base, under, unread, 1])
                continue
            current, base, under, unread, size = stack.pop()
            _rewrite_text(current, base, under, bounded)
            local = _local(current.tag)
            parent = stack[-1][0] if stack else None
            if episode_tag(local):
                read = _rss_episode if local == "item" else _atom_episode
                _remember_episode(head, tail, seen, read(current))
                if parent is not None:
                    parent.remove(current)
                current.clear()
                kept -= size
            elif unread:
                parent.remove(current)
                current.clear()
                kept -= size
            elif parent is not None:
                stack[-1][4] += size
    finally:
        source.close()
    if root is None:
        raise FeedParseError("Feed response was empty.")
    return root, head + list(tail)


def resolve_feed_urls(feed: FeedData, base_url: str) -> FeedData:
    """Resolve relative references against the feed's final URL.

    Atom explicitly allows relative URIs and real-world RSS contains them;
    unresolved values later fail playback, downloads, and the HTTP(S)-only
    link guard."""
    from dataclasses import replace as _replace
    from urllib.parse import urljoin

    if not base_url:
        return feed

    def fix(value: str) -> str:
        value = (value or "").strip()
        if not value or "://" in value.split("?", 1)[0][:12]:
            return value
        return urljoin(base_url, value)

    episodes = tuple(
        _replace(
            episode,
            media_url=fix(episode.media_url),
            website_url=fix(episode.website_url),
            transcript_url=fix(episode.transcript_url),
            chapters_url=fix(episode.chapters_url),
            artwork_url=fix(episode.artwork_url),
        )
        for episode in feed.episodes
    )
    return _replace(
        feed,
        website_url=fix(feed.website_url),
        artwork_url=fix(feed.artwork_url),
        episodes=episodes,
    )


def parse_feed(content: bytes, base_url: str = "") -> FeedData:
    if not content:
        raise FeedParseError("Feed response was empty.")
    if len(content) > MAX_FEED_BYTES:
        raise FeedParseError("Feed exceeds the size limit.")
    # A newline or space before the XML declaration is common and harmless.
    content = strip_leading_space(content)
    # The XML parser reads the whole prolog itself: text scans missed
    # declarations behind long comments, in UTF-16, or after a processing
    # instruction that contained comment markup.
    try:
        refused = contains_dtd(content)
    except UnsupportedEncoding as exc:
        raise FeedParseError(str(exc)) from exc
    if refused:
        raise FeedParseError("DTD and entity declarations are not allowed.")
    _SKIPPED.take()
    try:
        root, episodes = _stream_feed(content, base_url)
    except ET.ParseError as exc:
        raise FeedParseError(f"Invalid XML: {exc}") from exc
    kind = _local(root.tag)
    if kind in {"rss", "rdf", "channel"}:
        return resolve_feed_urls(_parse_rss(root, episodes), base_url)
    if kind == "feed":
        return resolve_feed_urls(_parse_atom(root, episodes), base_url)
    raise FeedParseError(f"Unsupported feed root: {kind or 'unknown'}.")
