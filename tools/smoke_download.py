"""One local M4 interrupted-download, resume, and cleanup-preview smoke flow."""

from pathlib import Path
from tempfile import TemporaryDirectory
import os
import subprocess
import wave

import requests


WORKSPACE = Path(__file__).resolve().parents[2]
LOCAL_TMP = WORKSPACE / "tmp"

os.environ.setdefault("TMPDIR", str(LOCAL_TMP))

from bs_podcasts.data import Database
from bs_podcasts.data.repositories import DownloadRepository, LibraryRepository
from bs_podcasts.domain import DownloadState, FeedData, FeedEpisodeData
from bs_podcasts.downloads import DownloadError, DownloadService


def require(condition: bool, message: str):
    if not condition:
        raise RuntimeError(message)


def make_silent_media(path: Path):
    subprocess.run(
        [
            "ffmpeg",
            "-loglevel",
            "error",
            "-f",
            "lavfi",
            "-i",
            "anullsrc=r=48000:cl=stereo",
            "-t",
            "1",
            "-c:a",
            "pcm_s16le",
            "-y",
            str(path),
        ],
        check=True,
    )


class LocalResponse:
    def __init__(self, content: bytes, status_code: int, interrupt: bool = False, content_range: str = ""):
        self.content = content
        self.status_code = status_code
        self.headers = {"Content-Length": str(len(content))}
        if content_range:
            # RFC 7233 requires Content-Range on a 206, and the service now
            # relies on it to verify the resume offset instead of appending on
            # faith. A stub without it models a non-conformant server.
            self.headers["Content-Range"] = content_range
        self.interrupt = interrupt
        self.closed = False

    def raise_for_status(self):
        return None

    def iter_content(self, chunk_size):
        midpoint = max(1, len(self.content) // 2)
        yield self.content[:midpoint]
        if self.interrupt:
            raise requests.ConnectionError("intentional local interruption")
        yield self.content[midpoint:]

    def close(self):
        self.closed = True


class ResumeSession:
    def __init__(self, content: bytes):
        self.content = content
        self.calls = 0
        self.responses = []

    def get(self, url, headers, **kwargs):
        self.calls += 1
        range_header = headers.get("Range", "")
        if self.calls == 1:
            response = LocalResponse(self.content, 200, interrupt=True)
            self.responses.append(response)
            return response
        require(range_header.startswith("bytes="), "retry did not request a byte range")
        start = int(range_header.removeprefix("bytes=").removesuffix("-"))
        total = len(self.content)
        response = LocalResponse(
            self.content[start:],
            206,
            content_range=f"bytes {start}-{total - 1}/{total}",
        )
        self.responses.append(response)
        return response


def main() -> int:
    LOCAL_TMP.mkdir(parents=True, exist_ok=True)
    source = LOCAL_TMP / "m4-source.wav"
    make_silent_media(source)
    media = source.read_bytes()

    with TemporaryDirectory(prefix="m4-smoke-", dir=LOCAL_TMP, ignore_cleanup_errors=True) as temporary:
        root = Path(temporary)
        database = Database(root / "library.db")
        library = LibraryRepository(database)
        downloads = DownloadRepository(database)
        show = library.add_show("https://samples.invalid/download.xml", "Download Sample")
        library.import_feed(
            show.id,
            FeedData(
                title="Download Sample",
                episodes=(
                    FeedEpisodeData(
                        "download-001",
                        "Resumable episode",
                        media_url="https://media.invalid/resumable.wav",
                        duration_seconds=1,
                    ),
                ),
            ),
        )
        episode = library.list_episodes(show.id)[0]
        session = ResumeSession(media)
        service = DownloadService(library, downloads, root / "downloads", session=session)
        service.RETRY_DELAYS = (0.0,)
        completed = service.download(episode.id)
        target = Path(completed.target_path)
        partial = Path(completed.partial_path)
        require(completed.state == DownloadState.COMPLETE, "retry did not complete")
        require(session.calls == 2, f"stream failure was not retried: {session.calls}")
        require(all(response.closed for response in session.responses), "stream response was not closed")
        require(target.read_bytes() == media, "resumed file differs from source")
        require(not partial.exists(), "partial file remained after atomic completion")
        with wave.open(str(target), "rb") as audio:
            require(audio.getnframes() > 0, "completed WAV is unreadable")

        preview = service.cleanup_preview(episode.id)
        require(preview.path == str(target), "cleanup preview reported the wrong path")
        require(preview.bytes_reclaimed == len(media), "cleanup preview reported the wrong size")
        require(target.exists(), "cleanup preview deleted the file")

    print("BS Podcasts M4 download smoke flow passed.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
