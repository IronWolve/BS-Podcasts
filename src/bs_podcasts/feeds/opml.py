"""Small, safe OPML subscription import/export."""

from dataclasses import dataclass
from datetime import datetime, timezone
import xml.etree.ElementTree as ET


MAX_OPML_BYTES = 2 * 1024 * 1024


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
    upper = content[:4096].upper()
    if b"<!DOCTYPE" in upper or b"<!ENTITY" in upper:
        raise OpmlError("DTD and entity declarations are not allowed.")
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
        entries.append(
            OpmlEntry(
                title=(outline.attrib.get("title") or outline.attrib.get("text") or "").strip(),
                feed_url=feed_url,
                website_url=(outline.attrib.get("htmlUrl") or "").strip(),
            )
        )
    return entries


def export_opml(shows) -> bytes:
    root = ET.Element("opml", {"version": "2.0"})
    head = ET.SubElement(root, "head")
    ET.SubElement(head, "title").text = "BS Podcasts subscriptions"
    ET.SubElement(head, "dateCreated").text = datetime.now(timezone.utc).isoformat()
    body = ET.SubElement(root, "body")
    for show in shows:
        attributes = {
            "type": "rss",
            "text": show.title or show.feed_url,
            "title": show.title or show.feed_url,
            "xmlUrl": show.feed_url,
        }
        if show.website_url:
            attributes["htmlUrl"] = show.website_url
        ET.SubElement(body, "outline", attributes)
    ET.indent(root, space="  ")
    return ET.tostring(root, encoding="utf-8", xml_declaration=True)
