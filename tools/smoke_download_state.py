"""Focused regression for download completion, resume identity, and recovery.

Each check fails against the code as it stood before audit batch 2: a finished
job was reported as a finished download, a cancelled transfer left a row that
claimed to be downloading with no worker behind it, a resumed file could be
appended across a changed resource, and a body with no Content-Length could be
truncated and still marked complete.

Headless, no network: the HTTP session is a stub.
"""

import sys
from pathlib import Path
from tempfile import TemporaryDirectory

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from bs_podcasts.data import Database
from bs_podcasts.data.repositories import DownloadRepository, LibraryRepository
from bs_podcasts.domain import DownloadState, FeedData, FeedEpisodeData
from bs_podcasts.downloads.service import DownloadError, DownloadService

FAILURES = []


def check(label, condition):
    if not condition:
        FAILURES.append(label)


class StubResponse:
    def __init__(self, body: bytes, status=200, headers=None):
        self.body = body
        self.status_code = status
        self.headers = headers or {}

    def raise_for_status(self):
        if self.status_code >= 400:
            raise DownloadError(f"HTTP {self.status_code}")

    def iter_content(self, size):
        for start in range(0, len(self.body), size):
            yield self.body[start : start + size]

    def close(self):
        pass


class StubSession:
    """Serves a scripted sequence of responses and records the requests."""

    def __init__(self, responses):
        self.responses = list(responses)
        self.requests = []

    def get(self, url, headers=None, timeout=None, stream=True):
        self.requests.append(dict(headers or {}))
        return self.responses.pop(0)


def build(temporary, media_url="https://media.invalid/ep.mp3", enclosure_bytes=0):
    database = Database(Path(temporary) / "library.db")
    library = LibraryRepository(database)
    show = library.add_show("https://feed.invalid/rss.xml", "Show")
    library.import_feed(
        show.id,
        FeedData(
            title="Show",
            episodes=(
                FeedEpisodeData(
                    "e1", "Episode", media_url=media_url, enclosure_bytes=enclosure_bytes
                ),
            ),
        ),
    )
    episode = library.list_episodes(show.id)[0]
    return library, DownloadRepository(database), episode


def scratch():
    root = ROOT.parent / "tmp"
    root.mkdir(parents=True, exist_ok=True)
    return TemporaryDirectory(prefix="dlstate-", dir=root, ignore_cleanup_errors=True)


def test_duplicate_call_is_not_a_completion():
    """A call absorbed as a duplicate must not look like a finished download."""
    with scratch() as temporary:
        library, repository, episode = build(temporary)
        service = DownloadService(library, repository, Path(temporary) / "dl", session=StubSession([]))
        # Stand in for a live transfer: the guard entry exists, so a second
        # call returns immediately without downloading anything.
        from threading import Event

        service._cancellations[episode.id] = Event()
        record = service.download(episode.id)
        check(
            "duplicate call does not report COMPLETE",
            getattr(record, "state", None) != DownloadState.COMPLETE,
        )


def test_truncated_body_without_content_length():
    """enclosure_bytes gives truncation detection when the header is absent."""
    with scratch() as temporary:
        library, repository, episode = build(temporary, enclosure_bytes=1000)
        session = StubSession([StubResponse(b"x" * 400)] * 4)  # short body, no length
        service = DownloadService(library, repository, Path(temporary) / "dl", session=session)
        try:
            service.download(episode.id)
        except DownloadError:
            pass
        record = repository.get(episode.id)
        check(
            "short body is not marked complete",
            record is not None and record.state != DownloadState.COMPLETE,
        )
        check(
            "episode has no downloaded_path after a truncated transfer",
            not library.get_episode(episode.id).downloaded_path,
        )


def test_resume_across_a_changed_resource_restarts():
    """A 206 whose total disagrees with the partial must not be appended."""
    with scratch() as temporary:
        library, repository, episode = build(temporary, enclosure_bytes=10)
        directory = Path(temporary) / "dl"
        directory.mkdir(parents=True, exist_ok=True)
        service = DownloadService(library, repository, directory, session=StubSession([]))

        target = service._target(episode.id, episode.media_url)
        partial = target.with_suffix(target.suffix + ".part")
        partial.write_bytes(b"OLD")  # 3 bytes of a previous, different file
        repository.prepare(episode.id, episode.media_url, target, partial)

        # Server offers a 206 declaring a total that does not match, then a
        # clean 200 for the restart the guard should force.
        service.session = StubSession(
            [
                StubResponse(
                    b"XXXXXXX",
                    status=206,
                    headers={"Content-Range": "bytes 3-9/99", "Content-Length": "7"},
                ),
                StubResponse(b"NEWCONTENT", headers={"Content-Length": "10"}),
            ]
        )
        record = service.download(episode.id)
        check(
            "changed resource restarts and completes",
            getattr(record, "state", None) == DownloadState.COMPLETE,
        )
        check(
            "restarted file has no stale prefix",
            target.is_file() and target.read_bytes() == b"NEWCONTENT",
        )


def test_prepare_keeps_existing_paths():
    """Re-preparing must not repoint a record away from its live partial."""
    with scratch() as temporary:
        _library, repository, episode = build(temporary)
        first = Path(temporary) / "a.mp3"
        repository.prepare(episode.id, "https://a.invalid/x", first, first.with_suffix(".part"))
        repository.prepare(episode.id, "https://b.invalid/y", Path(temporary) / "b.mp3", Path(temporary) / "b.part")
        record = repository.get(episode.id)
        check("prepare keeps the original target path", record.target_path == str(first))
        check("prepare updates the source url", record.source_url == "https://b.invalid/y")


def test_reconcile_parks_interrupted_rows():
    """A row left mid-transfer by a previous run becomes resumable."""
    with scratch() as temporary:
        library, repository, episode = build(temporary)
        target = Path(temporary) / "x.mp3"
        repository.prepare(episode.id, episode.media_url, target, target.with_suffix(".part"))
        repository.progress(episode.id, DownloadState.DOWNLOADING, 10, 100)
        service = DownloadService(library, repository, Path(temporary) / "dl", session=StubSession([]))
        parked = service.reconcile_interrupted()
        record = repository.get(episode.id)
        check("one interrupted row was parked", parked == 1)
        check("interrupted row is now paused", record.state == DownloadState.PAUSED)
        check("progress is preserved for the resume", record.bytes_done == 10)


def main() -> int:
    test_duplicate_call_is_not_a_completion()
    test_truncated_body_without_content_length()
    test_resume_across_a_changed_resource_restarts()
    test_prepare_keeps_existing_paths()
    test_reconcile_parks_interrupted_rows()

    for failure in FAILURES:
        print(f"FAIL {failure}")
    if FAILURES:
        print(f"\n{len(FAILURES)} check(s) failed")
        return 1
    print("download state: all checks passed")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
