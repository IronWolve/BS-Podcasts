"""PyInstaller entry point for the Windows desktop build."""

import sys


def _windows_prelaunch():
    """Close the double-click race before any heavy import runs.

    The first instance holds a named mutex for its whole lifetime. A second
    launch sees the mutex and waits (briefly) for the first instance's
    single-instance pipe to come up, so the Qt-level probe in app.main()
    reliably finds it and raises the existing window instead of racing it
    while the first instance is still starting.

    An instance that is quitting closes its pipe first and exits a moment
    later, so a missing pipe is not worth waiting out: the loop also looks
    again at the mutex, which disappears with that instance's process.
    """
    if sys.platform != "win32":
        return
    import ctypes
    from ctypes import wintypes
    import getpass
    import time

    # use_last_error: a separate GetLastError() call can read a code that
    # another call in between overwrote.
    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel32.CreateMutexW.restype = wintypes.HANDLE
    kernel32.CreateMutexW.argtypes = (wintypes.LPVOID, wintypes.BOOL, wintypes.LPCWSTR)
    kernel32.CloseHandle.argtypes = (wintypes.HANDLE,)
    kernel32.WaitNamedPipeW.restype = wintypes.BOOL
    kernel32.WaitNamedPipeW.argtypes = (wintypes.LPCWSTR, wintypes.DWORD)
    name = f"bs-podcasts-{getpass.getuser()}"

    def create_mutex():
        ctypes.set_last_error(0)
        handle = kernel32.CreateMutexW(None, False, "Local\\" + name)
        return handle, ctypes.get_last_error() == 183  # ERROR_ALREADY_EXISTS

    # The handle is intentionally kept open (and never closed on the way out)
    # so the mutex lives exactly as long as the process.
    handle, existed = create_mutex()
    if not existed:
        return
    pipe = "\\\\.\\pipe\\" + name  # QLocalServer's pipe path for this name
    deadline = time.monotonic() + 6.0
    while time.monotonic() < deadline:
        ctypes.set_last_error(0)
        if kernel32.WaitNamedPipeW(pipe, 200):
            return
        if ctypes.get_last_error() != 2:  # not "pipe not created yet"
            return
        # Our own handle would keep a quitting instance's mutex alive after
        # that instance has gone, so let go of it before looking again.
        kernel32.CloseHandle(handle)
        handle, existed = create_mutex()
        if not existed:
            return  # the other instance has ended; this launch is the first
        time.sleep(0.2)
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
