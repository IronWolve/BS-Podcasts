# The metadata probe imports only the engine module, so the service and the
# external engine load when one of their names is first asked for.
_LAZY = {
    "EngineCapabilities": ".engine", "EngineEvent": ".engine", "LazyMpvEngine": ".engine",
    "MpvEngine": ".engine", "PlaybackUnavailable": ".engine",
    "ExternalPlayerEngine": ".external",
    "PlaybackService": ".service", "PlaybackSnapshot": ".service", "PlaybackState": ".service",
}


def __getattr__(name):
    module = _LAZY.get(name)
    if module is None:
        raise AttributeError(name)
    from importlib import import_module

    return getattr(import_module(module, __name__), name)


__all__ = [
    "EngineCapabilities",
    "EngineEvent",
    "ExternalPlayerEngine",
    "LazyMpvEngine",
    "MpvEngine",
    "PlaybackService",
    "PlaybackSnapshot",
    "PlaybackState",
    "PlaybackUnavailable",
]
