"""PyInstaller entry point for the Windows desktop build."""

import sys


def _windows_prelaunch():
    """Close the double-click race before any heavy import runs.

    The first instance holds a named mutex for its whole lifetime. A second
    launch sees the mutex and waits for the first instance's single-instance
    pipe to come up, so the Qt-level probe in app.main() usually finds it and
    raises the existing window instead of racing it while the first instance
    is still starting. The wait is best effort (six seconds at most); a slow
    first start can end it early. LibraryLease is the guard that keeps a second
    instance from opening the library, whichever way this wait ends.

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
    # Only a second launch gets here, and the application is about to be
    # imported anyway: let it name its own endpoint instead of rebuilding the
    # name, so a change to that name cannot leave this wait on a missing pipe.
    from bs_podcasts.app import _instance_name
    pipe = "\\\\.\\pipe\\" + _instance_name()  # QLocalServer's pipe path for this name
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


def _diagnostic_failed(option):
    """Keep the active traceback in the folder the option names, then exit 1. A windowed bootloader
    must not show a traceback dialog, so the record goes to a file: beside the output file for a path
    option, inside the folder itself for --self-check-dir."""
    import traceback
    from pathlib import Path
    argv = sys.argv
    folder = None
    if option in argv and argv.index(option) + 1 < len(argv):
        target = Path(argv[argv.index(option) + 1])
        folder = target if option == "--self-check-dir" else target.parent
    if folder is not None:
        try:
            folder.mkdir(parents=True, exist_ok=True)
            (folder / "launcher-traceback.txt").write_text(traceback.format_exc(), encoding="utf-8")
        except OSError:
            pass  # nowhere else to report it; the exit code still says the run failed
    raise SystemExit(1)


if "--probe-local-audio" in sys.argv:
    try:
        from bs_podcasts.feeds.local_probe import main as probe_main
        raise SystemExit(probe_main())
    except Exception:
        _diagnostic_failed("--probe-output")

if "--self-check" in sys.argv:
    # Do not acquire the GUI-instance mutex or import the application for an
    # owned-copy diagnostic. No window, user profile or network is opened.
    try:
        from bs_podcasts.selfcheck import main as selfcheck_main
        raise SystemExit(selfcheck_main())
    except Exception:
        _diagnostic_failed("--self-check-dir")

_windows_prelaunch()

from bs_podcasts.__main__ import main


if __name__ == "__main__":
    raise SystemExit(main())
