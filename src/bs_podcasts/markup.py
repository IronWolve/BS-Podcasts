"""Feed and transcript markup: plain text for labels and snippets, the
sanitiser for show notes, and the readers that find feed links and transcript
text. Qt-free; the UI and the feed readers both import from here."""

from html import escape as html_escape
from html.parser import HTMLParser
import html
import re

from .urlguard import UnsafeUrl, ensure_web_url


# An unclosed comment or instruction runs to the end of the text, so each
# opener is scanned once instead of once per later opener.
_TAG = re.compile(r"</?[A-Za-z][A-Za-z0-9:-]*(?:\s+[A-Za-z_:][\w:.-]*\s*=\s*(?:\"[^\"]*\"|'[^']*'|[^\s<>]+))*\s*/?>|<!--.*?(?:-->|\Z)|<![A-Za-z][^<>]*>|<\?.*?(?:\?>|\Z)", re.S)
_WS = re.compile(r"\s+")

# Code blocks drop with their contents. An unclosed one runs to the end of the
# text, the way a browser reads it, so each opener is still scanned once.
_CODE_ELEMENT = re.compile(r"<(script|style|noscript)(?=[\s/>])[^<>]*>.*?(?:</\1\s*>|\Z)", re.I | re.S)
_CDATA_MARKER = re.compile(r"<!\[CDATA\[|\]\]>")


def _strip_markup(text: str) -> str:
    text = _CODE_ELEMENT.sub(" ", text)
    text = _CDATA_MARKER.sub(" ", text)
    return _TAG.sub(" ", text)


def plain_snippet(text: str, limit: int = 240) -> str:
    """One-line plain-text preview of possibly-HTML show notes."""
    if not text:
        return ""
    text = text[:16384]
    # Strip, unescape, strip again: feeds double-escape markup often enough
    # that unescaping reveals fresh tags ("&lt;b&gt;bold&lt;/b&gt;"), which a
    # single pre-unescape strip left visible as literal angle brackets.
    stripped = html.unescape(_strip_markup(text))
    stripped = _strip_markup(stripped)
    collapsed = _WS.sub(" ", stripped).strip()
    return collapsed[:limit]

class SafeFeedHtml(HTMLParser):
    """Small allow-list sanitizer for untrusted feed descriptions."""

    ALLOWED = {
        "p", "br", "b", "strong", "i", "em", "u", "s", "ul", "ol", "li",
        "blockquote", "code", "pre", "h1", "h2", "h3", "h4", "h5", "h6", "a",
    }
    VOID = {"br"}
    BLOCKED = {"script", "style", "iframe", "embed", "svg"}

    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.parts = []
        self._blocked = 0
        self._anchors = []
        self._block_stack = []

    def handle_starttag(self, tag, attrs):
        tag = tag.lower()
        if tag == "img" or tag == "embed":
            return
        if self._block_stack and self._block_stack[-1] == "svg" and tag in {"p", "div", "ul", "ol", "h1", "h2", "h3"}:
            self._block_stack.pop()
            self._blocked = max(0, self._blocked - 1)
        if tag in self.BLOCKED:
            self._blocked += 1
            self._block_stack.append(tag)
            return
        if self._blocked or tag not in self.ALLOWED:
            return
        if tag == "a":
            href = next((value for name, value in attrs if name.lower() == "href"), "") or ""
            try:
                safe_href = ensure_web_url(href)
            except UnsafeUrl:
                self._anchors.append(False)
                self.parts.append("<span>")
                return
            self._anchors.append(True)
            self.parts.append(f'<a href="{html_escape(safe_href, quote=True)}">')
            return
        self.parts.append(f"<{tag}>")

    def handle_startendtag(self, tag, attrs):
        self.handle_starttag(tag, attrs)
        self.handle_endtag(tag)

    def handle_endtag(self, tag):
        tag = tag.lower()
        if tag == "embed":
            return
        if tag == "object":
            return  # Object tags are discarded; their fallback text is readable.
        if tag in self.BLOCKED:
            if tag in self._block_stack:
                index = len(self._block_stack) - 1 - self._block_stack[::-1].index(tag)
                del self._block_stack[index:]
                self._blocked = len(self._block_stack)
            return
        if self._blocked or tag not in self.ALLOWED or tag in self.VOID:
            return
        if tag == "a":
            self.parts.append("</a>" if self._anchors and self._anchors.pop() else "</span>")
        else:
            self.parts.append(f"</{tag}>")

    def handle_data(self, data):
        if self._blocked:
            return
        # Never linkify inside an existing anchor — that would nest <a> tags.
        inside_anchor = bool(self._anchors) and self._anchors[-1]
        rendered = html_escape(data) if inside_anchor else linkify(data)
        self.parts.append(rendered.replace("\n", "<br>"))


# Bare URLs in show notes: with a scheme, as www., or as host/path (which is
# how feeds usually write "megaphone.fm/adchoices"). A bare "Node.js" or
# "audio.mp3" has no path segment, so it is left alone.
_URL_PATTERN = re.compile(
    r"""(?<![A-Za-z0-9_.-])(?P<url>
        https?://[^\s<>"']+
        | www\.[^\s<>"']+
        | [A-Za-z0-9][A-Za-z0-9\-]*(?:\.[A-Za-z0-9\-]+)+/[^\s<>"']*
    )""",
    re.VERBOSE,
)
_URL_TRAILING = ".,;:!?)]}>'\""


def linkify(text: str) -> str:
    """Escape text and turn bare URLs into HTTP(S) links."""
    result = []
    position = 0
    for match in _URL_PATTERN.finditer(text or ""):
        raw = match.group("url")
        trimmed = raw.rstrip(_URL_TRAILING.replace(")", ""))
        while trimmed.endswith(")") and trimmed.count(")") > trimmed.count("("):
            trimmed = trimmed[:-1]
        if not trimmed:
            continue
        href = trimmed if trimmed.lower().startswith(("http://", "https://")) else f"https://{trimmed}"
        result.append(html_escape(text[position:match.start()]))
        try:
            safe_href = ensure_web_url(href)
        except UnsafeUrl:
            result.append(html_escape(trimmed))
        else:
            result.append(f'<a href="{html_escape(safe_href, quote=True)}">{html_escape(trimmed)}</a>')
        result.append(html_escape(raw[len(trimmed):]))
        position = match.end()
    result.append(html_escape(text[position:]))
    return "".join(result)

_SKIP_HTML = {"script", "style"}


class TranscriptHTML(HTMLParser):
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

def markup_text(body: bytes, encoding: str | None) -> str:
    """Body text for the XML sniff and HTML discovery. Never raises.

    The header charset is only a hint here: the feed body reaches the XML
    parser as raw bytes. An unknown or non-text label (`utf8mb4`, `none`,
    `base64`) falls back to UTF-8 instead of failing the fetch.
    """
    if body.startswith((b"\xff\xfe\x00\x00", b"\x00\x00\xfe\xff")):
        return body.decode("utf-32", "replace")
    if body.startswith((b"\xff\xfe", b"\xfe\xff")):
        return body.decode("utf-16", "replace")
    if body.startswith(b"\xef\xbb\xbf"):
        return body.decode("utf-8-sig", "replace")
    try:
        return body.decode((encoding or "").strip() or "utf-8", "replace")
    except (LookupError, ValueError):
        return body.decode("utf-8", "replace")

class FeedLinkFound(Exception):
    """Ends the parse: the feed link and the base that resolves it are known."""


class FeedLinkParser(HTMLParser):
    def __init__(self):
        super().__init__()
        self.href = ""
        self.base = ""
        self.in_body = False

    def handle_starttag(self, tag, attrs):
        if tag.lower() == "base" and not self.base:
            self.base = dict(attrs).get("href", "") or ""
        elif tag.lower() == "body":
            self.in_body = True
        elif tag.lower() == "link" and not self.href:
            values = {key.lower(): value for key, value in attrs if value is not None}
            relations = {part.lower() for part in values.get("rel", "").split()}
            mime = values.get("type", "").lower()
            if "alternate" in relations and mime in {
                "application/rss+xml",
                "application/atom+xml",
                "application/xml",
                "text/xml",
            }:
                self.href = values.get("href", "").strip()
        # A base element belongs in the head, so once the body starts or one
        # was seen, nothing later in the page changes the answer.
        if self.href and (self.base or self.in_body):
            raise FeedLinkFound
