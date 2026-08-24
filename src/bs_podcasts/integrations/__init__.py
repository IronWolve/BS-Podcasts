"""Desktop integrations. MPRIS needs QtDBus, which only ships on Linux wheels."""

from .tray import TrayController

try:
    from .mpris import MprisController
except ImportError:  # pragma: no cover - non-Linux platforms

    class MprisController:  # type: ignore[no-redef]
        """No-op stand-in when QtDBus is unavailable."""

        def __init__(self, window, playback):
            self.window = window
            self.playback = playback

        def shutdown(self):
            pass


__all__ = ["MprisController", "TrayController"]
