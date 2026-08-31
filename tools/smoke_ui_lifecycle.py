"""Focused regression for window lifecycle and off-thread destructive work.

Each check fails against the code as it stood before audit batch 6: a closing
window still applied worker results (the guard was inverted), startup timers
fired on windows a theme rebuild had already closed, the job dispatcher only
re-checked `_closed` for three of its ~17 kinds, and every file-deleting path
except bulk show removal looped on the Qt thread.

Offscreen; no network, no audio.
"""

import ast
import os
import sys
from pathlib import Path

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
os.environ.setdefault("BS_PODCASTS_NO_TRACER", "1")

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

SHELL = ROOT / "src" / "bs_podcasts" / "ui" / "shell.py"
FAILURES = []


def check(label, condition):
    if not condition:
        FAILURES.append(label)


def function_source(tree, source: str, name: str) -> str:
    for node in ast.walk(tree):
        if isinstance(node, ast.FunctionDef) and node.name == name:
            return ast.get_source_segment(source, node) or ""
    return ""


def main() -> int:
    source = SHELL.read_text()
    tree = ast.parse(source)

    # --- the closed-window guard is not inverted -------------------------
    run_read = function_source(tree, source, "_run_read")
    check("_run_read has a _closed branch", "self._closed" in run_read)
    check(
        "_run_read returns on a closed window instead of applying inline",
        "if self._closed:\n            return" in run_read,
    )
    check(
        "_run_read still runs inline when there is no job runner",
        "if self.jobs is None:" in run_read and "apply(work())" in run_read,
    )

    # --- one gate for every job kind -------------------------------------
    finished = function_source(tree, source, "_refresh_finished")
    head = finished.split("if kind ==", 1)[0]
    check("_refresh_finished checks _closed before dispatching any kind", "self._closed" in head)

    # --- startup timers cannot fire on a closed window -------------------
    later = function_source(tree, source, "_later")
    check("a guarded one-shot helper exists", "_closed" in later and "singleShot" in later)
    # Bare singleShot calls are allowed only for same-tick (0 ms) scheduling,
    # which cannot outlive the window.
    bare = [
        line.strip()
        for line in source.splitlines()
        if "QTimer.singleShot(" in line and "singleShot(0," not in line and "def _later" not in line
    ]
    bare = [line for line in bare if "lambda: None if self._closed" not in line]
    check(f"no unguarded delayed timers remain ({bare})", not bare)

    # --- destructive work is dispatched, not looped in the handler -------
    for name in (
        "_apply_retention",
        "_delete_downloads",
        "_cleanup_played",
        "_delete_played_quietly",
    ):
        body = function_source(tree, source, name)
        check(
            f"{name} does not unlink on the Qt thread",
            "self.downloads.delete(" not in body,
        )

    unsubscribe = function_source(tree, source, "_unsubscribe")
    check(
        "_unsubscribe does not stat targets on the Qt thread",
        "self.library.removal_preview(" not in unsubscribe,
    )
    check("_unsubscribe dispatches to a worker", "_run_task" in unsubscribe)
    confirm = function_source(tree, source, "_confirm_unsubscribe")
    check(
        "_confirm_unsubscribe does not remove on the Qt thread",
        "library.remove_subscription" in confirm and "_run_task" in confirm,
    )

    # --- every reveal path re-fits the splitter --------------------------
    select_page = function_source(tree, source, "_select_page")
    check(
        "_select_page reveals the pane through _reveal_context",
        "self._reveal_context()" in select_page and "self.context.show()" not in select_page,
    )

    # --- and the guard actually holds at runtime -------------------------
    from PySide6.QtWidgets import QApplication
    from bs_podcasts.ui.shell import MainWindow
    from bs_podcasts.ui.theme import apply_theme

    app = QApplication.instance() or QApplication([])
    apply_theme("dark")
    window = MainWindow()
    try:
        applied = []
        window._closed = True
        window._run_read(lambda: "late", applied.append, "probe")
        check("a closed window applies nothing from _run_read", applied == [])
        window._closed = False
        window._run_read(lambda: "inline", applied.append, "probe")
        check("an open window with no job runner still runs inline", applied == ["inline"])
    finally:
        window._closed = True
        window.deleteLater()
        app.processEvents()

    for failure in FAILURES:
        print(f"FAIL {failure}")
    if FAILURES:
        print(f"\n{len(FAILURES)} check(s) failed")
        return 1
    print("ui lifecycle: all checks passed")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
