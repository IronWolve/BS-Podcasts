"""Byte- and count-bounded feed previews; no Qt or network ownership."""
from collections import OrderedDict
from collections.abc import MutableMapping
from dataclasses import fields


def prepare_preview(feed):
    """Compute conservative retained size on the feed-fetch worker."""
    if not hasattr(feed, '_preview_bytes'):
        cost = 1024
        for record in (feed, *feed.episodes):
            cost += 1024
            for field in fields(record):
                value = getattr(record, field.name)
                if isinstance(value, str):
                    cost += 4 * len(value)
        object.__setattr__(feed, '_preview_bytes', cost)
    return feed


class PreviewCache(MutableMapping):
    def __init__(self, max_bytes=16*1024*1024, max_entries=64):
        self.max_bytes, self.max_entries = max_bytes, max_entries
        self.bytes = 0
        self._items = OrderedDict()
        self._protected = ()

    def __len__(self):
        return len(self._items)

    def __iter__(self):
        return iter(self._items)

    def __getitem__(self, key):
        return self._items[key][0]

    def get(self, key, default=None):
        if key not in self._items:
            return default
        self._items.move_to_end(key)
        return self[key]

    def __delitem__(self, key):
        self.bytes -= self._items.pop(key)[1]

    def __setitem__(self, key, value):
        cost = prepare_preview(value)._preview_bytes + 4 * len(key)
        if key in self._items:
            del self[key]
        if cost > self.max_bytes:
            return  # Display is allowed; one oversized feed is not retained.
        self._items[key] = (value, cost)
        self.bytes += cost
        self.trim()

    def protect(self, *keys):
        self._protected = tuple(key for key in keys if key)
        self.trim()

    def trim(self, maximum=None):
        count = self.max_entries if maximum is None else min(maximum, self.max_entries)
        while self._items and (len(self) > count or self.bytes > self.max_bytes):
            victim = next((key for key in self._items if key not in self._protected), None)
            if victim is None:
                primary = self._protected[0] if self._protected else None
                victim = next((key for key in reversed(self._items) if key != primary), next(iter(self._items)))
            del self[victim]
