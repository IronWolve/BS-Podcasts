"""PyInstaller entry point for the Windows desktop build."""

import sys


def _windows_prelaunch():
    """Close the double-click race before any heavy import runs.

    The first instance holds a named mutex for its whole lifetime. A second
    launch sees the mutex and waits (briefly) for the first instance's
    single-instance pipe to come up, so the Qt-level probe in app.main()
    reliably finds it and raises the existing window instead of racing it
    while the first instance is still starting.
    """
    if sys.platform != "win32":
        return
    import ctypes
    import getpass
    import time

    kernel32 = ctypes.windll.kernel32
    name = f"bs-podcasts-{getpass.getuser()}"
    # The handle is intentionally kept open (and never closed) so the mutex
    # lives exactly as long as the process.
    kernel32.CreateMutexW(None, False, "Local\\" + name)
    if kernel32.GetLastError() != 183:  # ERROR_ALREADY_EXISTS
        return
    pipe = "\\\\.\\pipe\\" + name  # QLocalServer's pipe path for this name
    deadline = time.monotonic() + 6.0
    while time.monotonic() < deadline:
        if kernel32.WaitNamedPipeW(ctypes.c_wchar_p(pipe), 200):
            return
        if kernel32.GetLastError() == 2:  # pipe not created yet
            time.sleep(0.2)
        else:
            return
    # Timed out: the other instance may be dead; the normal guard decides.


if "--probe-local-audio" in sys.argv:
    try:
        from bs_podcasts.feeds.local_probe import main as probe_main
        raise SystemExit(probe_main())
    except Exception:
        raise SystemExit(1)

if "--self-check" in sys.argv:
    # Do not acquire the GUI-instance mutex or import the application for an
    # owned-copy diagnostic. No window, user profile or network is opened.
    try:
        from bs_podcasts.selfcheck import main as selfcheck_main
        raise SystemExit(selfcheck_main())
    except Exception:
        raise SystemExit(1)  # A windowed bootloader must not show a traceback dialog.

_windows_prelaunch()

from bs_podcasts.__main__ import main


if __name__ == "__main__":
    raise SystemExit(main())
