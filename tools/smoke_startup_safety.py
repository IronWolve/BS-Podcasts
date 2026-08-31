"""Focused regression for startup claiming and connection reuse.

Before audit batch 9 `_claim_single_instance` cleared the socket path before
trying to bind it, so a second launch could unlink a live first instance's
socket and both would then listen on it; and every query opened a fresh
sqlite connection plus two PRAGMAs.

Headless; no network, no audio.
"""

import ast
import sys
import threading
from pathlib import Path
from tempfile import TemporaryDirectory

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from bs_podcasts.data import Database
from bs_podcasts.data.repositories import LibraryRepository

APP = ROOT / "src" / "bs_podcasts" / "app.py"
FAILURES = []


def check(label, condition):
    if not condition:
        FAILURES.append(label)


def scratch():
    root = ROOT.parent / "tmp"
    root.mkdir(parents=True, exist_ok=True)
    return TemporaryDirectory(prefix="startup-", dir=root, ignore_cleanup_errors=True)


def check_socket_is_never_cleared_before_binding():
    """removeServer must follow a failed listen, never precede the attempt."""
    source = APP.read_text()
    tree = ast.parse(source)
    claim = ""
    for node in ast.walk(tree):
        if isinstance(node, ast.FunctionDef) and node.name == "_claim_single_instance":
            claim = ast.get_source_segment(source, node) or ""
    check("_claim_single_instance was found", bool(claim))
    remove_at = claim.find("QLocalServer.removeServer(")
    listen_at = claim.find("server.listen(")
    check("the socket path is still cleared somewhere", remove_at != -1)
    check(
        "clearing happens only after a bind attempt, not before it",
        listen_at != -1 and listen_at < remove_at,
    )
    guard = claim[:remove_at]
    check(
        "and only once a re-probe found nothing answering",
        "_probe_running_instance(name, timeout_ms=1500)" in guard,
    )


def check_connections_are_reused_per_thread():
    with scratch() as temporary:
        database = Database(Path(temporary) / "library.db")
        with database.connect() as first:
            first_id = id(first)
        with database.connect() as second:
            second_id = id(second)
        check("the same thread reuses one connection", first_id == second_id)

        other = {}

        def worker():
            with database.connect() as connection:
                other["id"] = id(connection)

        thread = threading.Thread(target=worker)
        thread.start()
        thread.join()
        check("a different thread gets its own connection", other.get("id") != first_id)


def check_a_reused_connection_still_rolls_back():
    with scratch() as temporary:
        database = Database(Path(temporary) / "library.db")
        library = LibraryRepository(database)
        show = library.add_show("https://feed.invalid/a.xml", "Show")
        try:
            with database.connect() as connection:
                connection.execute("UPDATE shows SET title='rolled back' WHERE id=?", (show.id,))
                raise RuntimeError("boom")
        except RuntimeError:
            pass
        check(
            "a failed block does not leak its write into the next read",
            library.get_show(show.id).title == "Show",
        )
        # And the handle is still usable afterwards.
        library.set_setting("probe", "ok")
        check("the connection still works after a rollback", library.get_setting("probe") == "ok")


def check_repair_invalidates_cached_handles():
    with scratch() as temporary:
        database = Database(Path(temporary) / "library.db")
        library = LibraryRepository(database)
        library.add_show("https://feed.invalid/a.xml", "Before repair")
        with database.connect():
            pass  # cache a handle on this thread
        database.repair()
        shows = library.list_shows()
        check("data survives the repair", any(s.title == "Before repair" for s in shows))
        library.set_setting("after", "repair")
        check("the reopened handle works", library.get_setting("after") == "repair")


def main() -> int:
    check_socket_is_never_cleared_before_binding()
    check_connections_are_reused_per_thread()
    check_a_reused_connection_still_rolls_back()
    check_repair_invalidates_cached_handles()

    for failure in FAILURES:
        print(f"FAIL {failure}")
    if FAILURES:
        print(f"\n{len(FAILURES)} check(s) failed")
        return 1
    print("startup safety: all checks passed")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
