"""Theme rebuild preserves state even when the podcast read exceeds 350ms."""
import time
from threading import Event
from PySide6.QtCore import QItemSelectionModel
from PySide6.QtTest import QTest
from smoke_gui_state import create_application, build, settle, shell


def main():
    app = create_application(["view-restore"])
    old, repo, show, episodes, jobs = build(app)
    library = old.library
    old._open_show_id(show.id)
    settle(app, old)
    page = old.episode_page
    page.header.search.setText("Episode")
    QTest.qWait(180)
    page.set_filter("Unplayed")
    page._set_sort("oldest", "Oldest first")
    page.view.selectAll()
    page.view.verticalScrollBar().setValue(page.view.verticalScrollBar().maximum())
    state = old._capture_view_state()
    expected = page._selected_keys()
    scroll = page.view.verticalScrollBar().value()
    old._keep_services = True
    old.close()
    entered, release = Event(), Event()
    original = library.episodes
    def held(show_id=None, limit=500, offset=0):
        if show_id == show.id:
            entered.set()
            assert release.wait(3)
        return original(show_id,limit,offset)
    library.episodes = held
    new = shell.MainWindow(library=library, jobs=jobs, view_state=state, commands=old.commands)
    new.show()
    end = time.monotonic()+2
    while not entered.is_set() and time.monotonic()<end:
        app.processEvents()
        time.sleep(.005)
    assert entered.is_set()
    QTest.qWait(420)
    release.set()
    settle(app,new)
    page = new.episode_page
    assert page.header.search.text() == "Episode"
    assert page._filter == "Unplayed" and page._sort == "oldest"
    assert set(page._selected_keys()) == set(expected)
    assert page.view.verticalScrollBar().value() == scroll, (page.view.verticalScrollBar().value(),scroll)
    assert new._back_stack == state["back"] and new._forward_stack == state["forward"]
    assert new._pending_view_restore is None
    new.close()
    jobs.join(3)
    app.processEvents()
    print("A27: PASS delayed read, filter, search, sort, multi-selection, scroll and history")


if __name__ == "__main__":
    main()
