"""Desktop integrations. MPRIS needs QtDBus, which only ships on Linux wheels."""

import logging
import sys

from .tray import TrayController

try:
    from .mpris import MprisController
except ImportError as exc:  # pragma: no cover - non-Linux platforms
    if sys.platform.startswith("linux"):
        logging.getLogger("bs_podcasts").warning("MPRIS media keys are unavailable: %s", exc)

    class MprisController:  # type: ignore[no-redef]
        """No-op stand-in when QtDBus is unavailable."""

        available = False

        def __init__(self, window, playback):
            self.window = window
            self.playback = playback

        def shutdown(self):
            pass


__all__ = ["MprisController", "TrayController"]
