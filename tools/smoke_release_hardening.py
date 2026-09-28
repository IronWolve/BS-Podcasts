"""Small offline regressions for release text, parsers, cache and storage safety."""
from concurrent.futures import ThreadPoolExecutor
import json
import os
from pathlib import Path
import sqlite3
import sys
from tempfile import TemporaryDirectory
from threading import Event
from types import SimpleNamespace
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'src'))
from bs_podcasts.data import Database
from bs_podcasts.data.repositories import DownloadRepository, LibraryRepository
from bs_podcasts.domain import DownloadState, FeedData, FeedEpisodeData
from bs_podcasts.downloads.service import DownloadError, DownloadService
from bs_podcasts.feeds.listening_fetch import ListeningFetchError, _stamp, parse_chapters, parse_transcript
from bs_podcasts.feeds.parser import _date, parse_feed


def parsers():
    for value in ('9999-12-31T23:59:59-23:59', '0001-01-01T00:00:00+23:59', '9999-99-99T99:99:99'):
        assert _date(value) == ''
    assert _date('2026-09-28T12:00:00+02:00') == '2026-09-28T10:00:00'
    for content in (b'1', b'true', b'"text"'):
        for parse in (parse_chapters, lambda value: parse_transcript(value, 'application/json')):
            try:
                parse(content)
            except ListeningFetchError:
                pass
            else:
                raise AssertionError('Malformed collection accepted')
    deep = b'[' * 1100 + b'0' + b']' * 1100
    for parse in (parse_chapters, lambda value: parse_transcript(value, 'application/json')):
        try:
            assert parse(deep) == []
        except ListeningFetchError:
            pass  # Python versions with a lower JSON nesting limit reject it safely.
    for parse, content in ((parse_chapters, b'{"chapters":1}'),
                           (lambda data: parse_transcript(data, 'application/json'), b'{"segments":{}}')):
        try:
            parse(content)
        except ListeningFetchError:
            pass
        else:
            raise AssertionError('Non-list collection accepted')
    assert _stamp('00:02.500') == 2.5
    assert _stamp('12:34:56,789') == 45296.789
    assert _stamp('9' * 16000) is None
    from bs_podcasts.feeds.listening_fetch import _TIMESTAMP
    assert r'\d+' not in _TIMESTAMP.pattern, 'Timestamp scanning must retain bounded digit groups'
    feed = parse_feed(b'<rss><channel><title>Fixture</title><category>News</category><category>news</category>'
                      b'<category text="Technology"/><item><pubDate>9999-12-31T23:59:59-23:59</pubDate>'
                      b'<enclosure url="https://media.invalid/e.mp3" type="audio/mpeg"/></item></channel></rss>')
    assert feed.categories == ('News', 'Technology') and feed.episodes[0].published_at == ''


def storage(folder):
    db = Database(folder / 'library # 100%.db')
    try:
        assert not Database.migration_pending(db.path)
        library = LibraryRepository(db)
        library.add_show('https://feed.invalid/rss', 'Fixture')
        assert db.backup().is_file()
        assert db.repair().is_file() and db.check_integrity() == 'ok'
        assert library.list_shows()[0].title == 'Fixture'
        for operation in (db.backup, db._backup_before_migration):
            closed = []
            source = SimpleNamespace(close=lambda: closed.append(True))
            with patch('bs_podcasts.data.database.sqlite3.connect', side_effect=[source, sqlite3.OperationalError('fixture')]):
                try:
                    operation()
                except sqlite3.OperationalError:
                    pass
                else:
                    raise AssertionError('Backup failure hidden')
            assert closed == [True]
            assert not list(folder.rglob('.library-backup-*.tmp'))
    finally:
        db.close()


def downloads(folder):
    db = Database(folder / 'downloads.db')
    try:
        library, records = LibraryRepository(db), DownloadRepository(db)
        show = library.add_show('https://feed.invalid/downloads', 'Fixture')
        cases = ('valid', 'unknown-too-large', 'known-too-large', 'disk-reserve', 'disk-recheck')
        library.import_feed(show.id, FeedData('Fixture', episodes=tuple(
            FeedEpisodeData(case, case, media_url=f'https://media.invalid/{case}.mp3') for case in cases)))
        for episode in library.list_episodes(show.id):
            mode = episode.title
            class Response:
                status_code = 200
                headers = {'Content-Type': 'audio/mpeg', **({'Content-Length': '9'} if mode == 'known-too-large' else {})}
                def raise_for_status(self): pass
                def iter_content(self, size):
                    yield b'abcde' if mode == 'unknown-too-large' else b'abcd'
                    yield b'efghi' if mode == 'unknown-too-large' else b'efgh'
                def close(self): pass
            service = DownloadService(library, records, folder, session=SimpleNamespace(get=lambda *a, **k: Response()))
            service.MIN_FREE_BYTES = 10
            service.MAX_DOWNLOAD_BYTES = 8
            service.SPACE_CHECK_BYTES = 4
            target = service._target(episode.id, episode.media_url)
            partial = target.with_suffix(target.suffix + '.part')
            records.prepare(episode.id, episode.media_url, target, partial)
            space = [SimpleNamespace(free=n) for n in ([14, 10] if mode == 'disk-reserve' else [100, 13] if mode == 'disk-recheck' else [100, 100])]
            with patch('bs_podcasts.downloads.service.ensure_fetchable', lambda value, *args: value), \
                 patch('bs_podcasts.downloads.service.shutil.disk_usage', side_effect=space):
                try:
                    record = service._download_once(episode.id, Event())
                except DownloadError:
                    assert mode != 'valid' and not target.exists()
                    assert records.get(episode.id).state == DownloadState.ERROR
                    assert not partial.exists() or partial.stat().st_size <= service.MAX_DOWNLOAD_BYTES
                else:
                    assert mode == 'valid' and record.state == DownloadState.COMPLETE
                    assert target.read_bytes() == b'abcdefgh'
    finally:
        db.close()


def gui_and_cache(folder):
    from PySide6.QtCore import Qt
    from PySide6.QtWidgets import QApplication, QVBoxLayout, QWidget
    from bs_podcasts.ui import pixmaps
    from bs_podcasts.ui.widgets import PageHeader, PlainTextLabel, ElidedValueLabel, StateBanner
    app = QApplication.instance() or QApplication([])
    host = QWidget(); layout = QVBoxLayout(host)
    header = PageHeader('<b>Feed title must stay literal</b>', '<i>Publisher text</i>', show_search=False)
    layout.addWidget(header)
    banner = StateBanner(host); layout.addWidget(banner)
    banner.show_state('error', '<img src="file:///fixture">')
    label = PlainTextLabel('<img src="file:///fixture">'); layout.addWidget(label)
    value = ElidedValueLabel('<b>File name</b>'); layout.addWidget(value)
    label.setToolTip('<img src="file:///fixture">')
    assert '<img' not in label.toolTip() and '&lt;img' in label.toolTip()
    for field in (header.title_label, header.subtitle_label, banner.label, label, value):
        assert field.textFormat() == Qt.TextFormat.PlainText
    host.resize(800, 230); host.show(); app.processEvents()
    evidence = os.environ.get('BS_PODCASTS_AUDIT_EVIDENCE')
    if evidence:
        assert host.grab().save(str(Path(evidence) / 'fixed-literal-labels.png'))
    host.close(); app.processEvents()
    target = folder / 'accents.json'
    protected = folder / 'unrelated.txt'; protected.write_text('keep this file')
    if os.name == 'posix':
        target.with_suffix('.json.tmp').symlink_to(protected)
    with patch.object(pixmaps, '_cache_file', return_value=target):
        with ThreadPoolExecutor(max_workers=2) as pool:
            list(pool.map(lambda _: pixmaps.save_accents(), range(4)))
    assert protected.read_text() == 'keep this file'
    assert json.loads(target.read_text())['version'] == 1
    if os.name == 'posix':
        assert target.stat().st_mode & 0o777 == 0o600


def main():
    scratch = ROOT.parent / 'tmp'; scratch.mkdir(exist_ok=True)
    parsers()
    with TemporaryDirectory(prefix='release-hardening-', dir=scratch) as name:
        folder = Path(name)
        storage(folder); downloads(folder); gui_and_cache(folder)
    print('R01–R06: PASS literal labels/tooltips, atomic accent cache, bounded metadata parsing, '
          'download size/disk limits, escaped migration paths and backup cleanup')


if __name__ == '__main__':
    main()
