"""Shared guards for untrusted XML: feeds and OPML answer to the same rules."""

from xml.parsers import expat


class _Refused(Exception):
    pass


class _RootStarted(Exception):
    pass


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
    return False
