"""Shared guards for untrusted XML: feeds and OPML answer to the same rules."""

_DTD_TOKENS = ("<!DOCTYPE", "<!ENTITY")
# expat auto-detects UTF-16/32 from a BOM and parses such a document happily,
# so the declaration has to be looked for in those encodings too.
_ENCODINGS = ("ascii", "utf-16-le", "utf-16-be", "utf-32-le", "utf-32-be")


def contains_dtd(content: bytes) -> bool:
    """True when a DTD or entity declaration appears anywhere in `content`.

    Closes two bypasses of the older check. One: a declaration pushed past a
    fixed-size prefix scan by leading comments or whitespace — this reads the
    whole bounded buffer. Two: a declaration written in a non-ASCII-compatible
    encoding, where a raw ASCII substring search never matches the
    interleaved-null form. `bytes.upper()` only folds ASCII bytes, which is
    exactly what the interleaved form still contains.
    """
    upper = content.upper()
    for token in _DTD_TOKENS:
        for encoding in _ENCODINGS:
            try:
                needle = token.encode(encoding)
            except (LookupError, UnicodeEncodeError):
                continue
            if needle and needle in upper:
                return True
    return False
