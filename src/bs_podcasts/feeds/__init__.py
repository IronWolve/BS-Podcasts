from .fetch import FeedFetchError, FeedFetcher, FeedResponse
from .parser import FeedParseError, parse_feed
from .refresh import RefreshReport, RefreshService

# Local audio and OPML are only needed when explicitly importing files.
_LAZY = {
    "LocalAudioError": ".local", "LocalAudioImporter": ".local",
    "OpmlEntry": ".opml", "OpmlError": ".opml", "export_opml": ".opml", "import_opml": ".opml",
}


def __getattr__(name):
    module = _LAZY.get(name)
    if module is None:
        raise AttributeError(name)
    from importlib import import_module

    return getattr(import_module(module, __name__), name)


__all__ = [
    "FeedFetchError",
    "FeedFetcher",
    "FeedParseError",
    "FeedResponse",
    "LocalAudioError",
    "LocalAudioImporter",
    "OpmlEntry",
    "OpmlError",
    "RefreshReport",
    "RefreshService",
    "export_opml",
    "import_opml",
    "parse_feed",
]
