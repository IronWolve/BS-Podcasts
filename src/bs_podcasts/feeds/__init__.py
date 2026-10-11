from .fetch import FeedFetchError, FeedFetcher, FeedNetworkError, FeedResponse
from .parser import FeedParseError, parse_feed

# Local audio, OPML and refresh are loaded on first use: refresh pulls in the
# artwork cache, which a plain feed import does not need.
_LAZY = {
    "LocalAudioError": ".local", "LocalAudioImporter": ".local",
    "OpmlEntry": ".opml", "OpmlError": ".opml", "export_opml": ".opml", "import_opml": ".opml",
    "RefreshReport": ".refresh", "RefreshService": ".refresh",
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
    "FeedNetworkError",
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
