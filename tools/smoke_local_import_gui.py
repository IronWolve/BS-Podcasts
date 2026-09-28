"""Isolated offscreen check: local metadata work never blocks or steals navigation."""
from threading import Event, current_thread
from unittest.mock import patch
from smoke_gui_state import build, create_application, settle, shell


def main():
    app = create_application(['local-import-gui'])
    window, repository, show, episodes, jobs = build(app)
    entered, release = Event(), Event()
    main_thread = current_thread()
    notices = []
    def slow(path):
        assert current_thread() is not main_thread
        entered.set(); assert release.wait(3)
        return show
    try:
        window._notify = lambda *args: notices.append(args)
        with patch.object(window.library, 'import_local_audio', slow):
            window._import_local_audio_path('fixture.wav')
            assert entered.wait(2)
            window.navigation.select(shell.PAGE_SETTINGS)
            app.processEvents()
            assert not release.is_set() and window.pages.currentIndex() == shell.PAGE_SETTINGS
            release.set(); settle(app, window)
            assert window.pages.currentIndex() == shell.PAGE_SETTINGS
            assert any(args[0] == 'Local audio imported' for args in notices)
        with patch.object(window.library, 'import_local_audio', side_effect=ValueError('fixture import error')):
            window._import_local_audio_path('invalid.wav'); settle(app, window)
            assert any(args[0] == 'fixture import error' for args in notices)
        print('Local import GUI: PASS background worker, responsive navigation, late-result suppression and failure feedback')
    finally:
        release.set(); window.close(); window.commands.join(3); jobs.join(3)
        app.processEvents(); repository.database.close()


if __name__ == '__main__':
    main()
