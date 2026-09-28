"""Keep credentials and private URL paths out of diagnostic text."""
import re
from urllib.parse import urlsplit

_URL = re.compile(r'https?://[^\s<>\"\']+', re.IGNORECASE)
_QUERY = re.compile(r'([?&][A-Za-z0-9_.%-]+=)[^\s&\"\']+')
_RELATIVE_URL = re.compile(r'(\burl:\s*)/[^\s\)\]\"\']*', re.IGNORECASE)
_HEADER = re.compile(r'\b(Authorization|Proxy-Authorization|Cookie|Set-Cookie)\s*:\s*[^\r\n]+', re.IGNORECASE)


def feed_label(value):
    try:
        return urlsplit(str(value)).hostname or 'Podcast feed'
    except ValueError:
        return 'Podcast feed'


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
    result = _URL.sub(url, str(text))
    result = _RELATIVE_URL.sub(r'\1[redacted]', result)
    result = _QUERY.sub(r'\1[redacted]', result)
    return _HEADER.sub(r'\1: [redacted]', result)
