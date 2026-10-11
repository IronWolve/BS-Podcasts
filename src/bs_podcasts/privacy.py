"""Keep credentials and private URL paths out of diagnostic text."""
from functools import lru_cache
import os
import re
from urllib.parse import urlsplit

_URL = re.compile(r'[A-Za-z][A-Za-z0-9+.-]*://[^\s<>\"\']+', re.IGNORECASE)
_QUERY = re.compile(r'([?&][A-Za-z0-9_.%-]+=)[^\s&\"\']+')
_RELATIVE_URL = re.compile(r'(\burl:\s*)/[^\s\)\]\"\']*', re.IGNORECASE)
_HEADER = re.compile(r'\b(Authorization|Proxy-Authorization|Cookie|Set-Cookie)\s*:\s*[^\r\n]+', re.IGNORECASE)
_BARE_SECRET = re.compile(r'(["\x27]?\b(?:password|passwd|token|secret|api[_-]?key|access[_-]?token|refresh[_-]?token|client[_-]?secret)\b["\x27]?\s*[=:]\s*)(?:\[redacted\]|"[^"\r\n]*"|\x27[^\x27\r\n]*\x27|[^\s,;\]\)}]+)', re.IGNORECASE)
_BEARER = re.compile(r'\bBearer\s+[^\s,;\"\x27]+', re.IGNORECASE)
_USER_FOLDER = re.compile(r'([\\/](?:home|Users)[\\/]+)[^\\/\s:*?\"\x27<>|]+', re.IGNORECASE)


def feed_label(value):
    try:
        return urlsplit(str(value)).hostname or 'Podcast feed'
    except ValueError:
        return 'Podcast feed'


@lru_cache(maxsize=8)
def _home_pattern(home):
    parts = [part for part in re.split(r'[\\/]+', home) if part]
    if home == '~' or not parts or (len(parts) == 1 and parts[0].endswith(':')):
        return None
    lead = r'[\\/]+' if home[0] in '\\/' else ''
    body = r'[\\/]+'.join(re.escape(part) for part in parts)
    return re.compile(r'(?<![\w.~-])' + lead + body + r'(?![\w.-])', re.IGNORECASE)


def _home_paths(text):
    """Show the home folder as a tilde and hide any other account's folder name."""
    home = _home_pattern(os.path.expanduser('~'))
    if home is not None:
        text = home.sub('~', text)
    return _USER_FOLDER.sub(r'\1[user]', text)


def redact(text):
    def url(match):
        try:
            value = urlsplit(match.group(0))
            host = value.hostname or 'redacted'
            if ':' in host:
                host = '[' + host + ']'
            return value.scheme + '://' + host + '/[redacted]'
        except ValueError:
            return '[redacted URL]'
    result = _URL.sub(url, _home_paths(str(text)))
    result = _RELATIVE_URL.sub(r'\1[redacted]', result)
    result = _QUERY.sub(r'\1[redacted]', result)
    result = _BARE_SECRET.sub(r'\1[redacted]', result)
    result = _BEARER.sub('Bearer [redacted]', result)
    return _HEADER.sub(r'\1: [redacted]', result)
