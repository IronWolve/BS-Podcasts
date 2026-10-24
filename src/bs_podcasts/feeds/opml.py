"""Small, safe OPML subscription import/export."""

from dataclasses import dataclass
from datetime import datetime, timezone
import xml.etree.ElementTree as ET
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

from .safety import UnsupportedEncoding, contains_dtd, normalize_xml, normalize_html_entities
from ..privacy import feed_label


MAX_OPML_BYTES = 2 * 1024 * 1024
# The byte cap alone still admitted ~10,000 minimal outlines, each of which
# becomes a subscription plus a queued refresh. Real collections run to a few
# hundred; past this the file is refused loudly rather than truncated
# silently (design.md forbids silent caps).
MAX_OPML_ENTRIES = 1000


class OpmlError(ValueError):
    pass


@dataclass(frozen=True)
class OpmlEntry:
    title: str
    feed_url: str
    website_url: str = ""


def import_opml(content: bytes) -> list[OpmlEntry]:
    if not content or len(content) > MAX_OPML_BYTES:
        raise OpmlError("OPML is empty or exceeds the size limit.")
    # The whole prolog, read by the XML parser itself: a long comment or a
    # processing instruction holding comment markup was enough to smuggle an
    # entity declaration past text scans and reach internal-entity expansion.
    try:
        content = normalize_xml(content, MAX_OPML_BYTES)
        refused = contains_dtd(content)
    except UnsupportedEncoding as exc:
        raise OpmlError(str(exc)) from exc
    if refused:
        raise OpmlError("DTD and entity declarations are not allowed.")
    content = normalize_html_entities(content)
    if len(content) > MAX_OPML_BYTES:
        raise OpmlError("Normalized OPML exceeds the size limit.")
    try:
        root = ET.fromstring(content)
    except ET.ParseError as exc:
        raise OpmlError(f"Invalid OPML: {exc}") from exc

    entries = []
    seen = set()
    for outline in root.iter():
        if outline.tag.rsplit("}", 1)[-1].lower() != "outline":
            continue
        feed_url = (outline.attrib.get("xmlUrl") or outline.attrib.get("xmlurl") or "").strip()
        if not feed_url or feed_url in seen:
            continue
        seen.add(feed_url)
        if len(entries) >= MAX_OPML_ENTRIES:
            raise OpmlError(
                f"OPML has more than {MAX_OPML_ENTRIES} feeds; split the file to import it."
            )
        entries.append(
            OpmlEntry(
                title=(outline.attrib.get("title") or outline.attrib.get("text") or "").strip(),
                feed_url=feed_url,
                website_url=(outline.attrib.get("htmlUrl") or "").strip(),
            )
        )
    return entries


def shareable_url(url: str) -> str:
    """A web address with its credentials removed: userinfo dropped, every
    query value emptied (parameter names kept), fragment dropped.

    Private feeds often carry their access token in the userinfo or in a query
    value. An export is shared with other people, so it keeps the address
    shape and not the token. A token embedded in the path survives: it cannot
    be told apart from a public feed id. The app's own export keeps addresses
    exactly as stored and warns that the file is private."""
    try:
        parts = urlsplit(url)
    except ValueError:
        return ""
    netloc = parts.netloc.rpartition("@")[2]
    query = urlencode([(name, "") for name, _value in parse_qsl(parts.query, keep_blank_values=True)])
    return urlunsplit((parts.scheme, netloc, parts.path, query, ""))


def export_opml(shows, *, keep_credentials: bool = False) -> bytes:
    """Subscriptions as OPML. Feed and website addresses are shareable unless
    `keep_credentials` asks for the addresses exactly as stored (a backup of
    private feeds, to be restored into the same app)."""
    def address(value: str) -> str:
        return value if keep_credentials else shareable_url(value)

    root = ET.Element("opml", {"version": "2.0"})
    head = ET.SubElement(root, "head")
    ET.SubElement(head, "title").text = "BS Podcasts subscriptions"
    ET.SubElement(head, "dateCreated").text = datetime.now(timezone.utc).isoformat()
    body = ET.SubElement(root, "body")
    for show in shows:
        if getattr(show, "source", "rss") == "local":
            continue
        attributes = {
            "type": "rss",
            "text": show.title or feed_label(show.feed_url),
            "title": show.title or feed_label(show.feed_url),
            "xmlUrl": address(show.feed_url),
        }
        if show.website_url:
            attributes["htmlUrl"] = address(show.website_url)
        ET.SubElement(body, "outline", attributes)
    ET.indent(root, space="  ")
    return ET.tostring(root, encoding="utf-8", xml_declaration=True)
