"""Shared guards for untrusted XML: feeds and OPML answer to the same rules."""

from xml.parsers import expat
import codecs
import re


_DECLARATION = re.compile(br"^(?:\xef\xbb\xbf)?<\?xml\s+[^?]*\?>", re.IGNORECASE)
_ENCODING = re.compile(br"(\bencoding\s*=\s*)(['\"])([^'\"]+)(\2)", re.IGNORECASE)
_TRANSCODE_XML = {'gb2312', 'gbk', 'big5', 'shift_jis', 'euc_jp', 'euc_kr'}


def normalize_xml(content: bytes, maximum=20 * 1024 * 1024) -> bytes:
    """Normalize UTF-8 aliases and known multibyte publisher encodings safely."""
    content = strip_leading_space(content)
    declaration = _DECLARATION.match(content)
    if declaration is None:
        return content
    encoding = _ENCODING.search(declaration.group())
    if encoding is None:
        return content
    try:
        codec = codecs.lookup(encoding[3].decode("ascii"))
    except (LookupError, UnicodeError):
        return content  # The real XML parser reports unknown encodings.
    if codec.name in _TRANSCODE_XML:
        try:
            content = content.decode(codec.name).encode('utf-8')
        except UnicodeError as exc:
            raise UnsupportedEncoding('The XML body does not match its declared character encoding.') from exc
        if len(content) > maximum:
            raise UnsupportedEncoding('Normalized XML exceeds the size limit.')
    elif codec.name != "utf-8":
        return content
    begin, end = encoding.span(3)
    return content[:begin] + b"utf-8" + content[end:]


def normalize_html_entities(content: bytes) -> bytes:
    """Accept known HTML entities, leaving comments, CDATA and PIs literal.

    Called only after the original document passes the DTD/entity guard.
    Unknown names still fail XML parsing; external declarations are not loaded.
    """
    from html.entities import html5
    tokens = re.compile(br"<!\[CDATA\[.*?\]\]>|<!--.*?-->|<\?.*?\?>|&([A-Za-z][A-Za-z0-9]+);", re.S)
    def replace(match):
        name = match[1]
        if name is None:
            return match[0]
        if name in (b"amp", b"lt", b"gt", b"quot", b"apos"):
            return match[0]  # XML already knows these; avoid needless expansion.
        value = html5.get(name.decode("ascii") + ";")
        if value is None:
            return match[0]
        return b"".join(f"&#{ord(character)};".encode("ascii") for character in value)
    return tokens.sub(replace, content)


class _Refused(Exception):
    pass


class _RootStarted(Exception):
    pass


class UnsupportedEncoding(ValueError):
    """The declared character encoding is unknown or one the XML parser
    cannot read, or the publisher's body does not match its declaration."""


def strip_leading_space(content: bytes) -> bytes:
    """`content` without blank space before the XML declaration or root.

    Server-side templates often emit a newline or a space first, which the XML
    parser refuses ("XML or text declaration not at start of entity"). Only
    ASCII-compatible content is touched: UTF-16 and UTF-32 start with a byte
    order mark or null bytes, and their whitespace is not one byte wide. A
    UTF-8 byte order mark stays in front.
    """
    start = 3 if content.startswith(b"\xef\xbb\xbf") else 0
    if content[start:start + 1] not in (b" ", b"\t", b"\r", b"\n"):
        return content
    rest = content[start:].lstrip(b" \t\r\n")
    if rest[:1] != b"<" or rest[1:2] == b"\x00":
        return content
    return content[:start] + rest


def contains_dtd(content: bytes) -> bool:
    """True when `content` declares an internal DTD subset or an entity.

    The prolog is read by the XML parser itself, set up like the one that
    parses the document, so encodings, comments, processing instructions and
    CDATA are tokenized exactly as the real parse sees them. A text scan
    could be fooled: a processing instruction may legally contain `<!--` and
    hide a declaration that follows it. Reading stops at the root element;
    a declaration after it is malformed and the real parse refuses it.

    A DOCTYPE that only names an external DTD, like the legacy RSS 0.91
    header, is allowed: external DTDs and entities are never loaded.

    Raises UnsupportedEncoding for a declared encoding the parser cannot
    read: the real parse would fail at the same declaration.
    """

    def doctype(name, system_id, public_id, has_internal_subset):
        if has_internal_subset:
            raise _Refused

    def entity(*args):
        raise _Refused

    def root(name, attributes):
        raise _RootStarted

    parser = expat.ParserCreate(None, "}")
    parser.StartDoctypeDeclHandler = doctype
    parser.EntityDeclHandler = entity
    parser.StartElementHandler = root
    try:
        parser.Parse(content, True)
    except _Refused:
        return True
    except (_RootStarted, expat.ExpatError):
        # Malformed input fails the real parse at the same point.
        pass
    except (LookupError, ValueError) as exc:
        raise UnsupportedEncoding("Unsupported character encoding.") from exc
    return False
