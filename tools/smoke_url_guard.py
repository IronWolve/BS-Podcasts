"""Focused regression for the central fetch/open/play boundary.

Each check below fails against the code as it stood before `urlguard` existed:
a feed could name a `file://` enclosure and have it opened by the OS, point
artwork or chapters at a metadata endpoint, or hide a DTD from the parser
behind a long comment or a UTF-16 BOM.

Headless, no network, no audio: every check is a pure function call or a
stubbed engine.
"""

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from bs_podcasts.feeds.opml import OpmlError, import_opml
from bs_podcasts.feeds.parser import FeedParseError, parse_feed
from bs_podcasts.feeds.safety import contains_dtd
from bs_podcasts.playback.engine import PlaybackUnavailable
from bs_podcasts.playback.external import ExternalPlayerEngine
from bs_podcasts.urlguard import (
    UnsafeUrl,
    ensure_fetchable,
    ensure_media_source,
    ensure_web_url,
    is_web_url,
)

FAILURES = []


def check(label: str, condition: bool):
    if not condition:
        FAILURES.append(label)


def rejects(label: str, call, *args, **kwargs):
    try:
        call(*args, **kwargs)
    except (UnsafeUrl, OpmlError, FeedParseError, PlaybackUnavailable):
        return
    except Exception as exc:  # a different failure is still not a pass
        FAILURES.append(f"{label}: raised {type(exc).__name__} instead of a guard error")
        return
    FAILURES.append(f"{label}: accepted")


def accepts(label: str, call, *args, **kwargs):
    try:
        call(*args, **kwargs)
    except Exception as exc:
        FAILURES.append(f"{label}: rejected ({type(exc).__name__}: {exc})")


class _StubEngine:
    """Records what it was asked to load; never touches media."""

    class capabilities:
        internal = True
        seek = True
        speed = True
        volume = True
        ab_repeat = False
        silence_trim = False

    def __init__(self):
        self.loaded = []
        self.dead = False

    def set_event_handler(self, handler):
        pass

    def load(self, source, start_position=0.0, autoplay=True):
        self.loaded.append(source)

    def set_speed(self, speed):
        pass

    def set_volume(self, volume):
        pass

    def shutdown(self):
        self.dead = True


def check_playback_provenance():
    """A feed may not name a local file; a local import may.

    This is the contract the whole guard exists for, so it is asserted end to
    end through PlaybackService rather than against the helper alone.
    """
    from tempfile import TemporaryDirectory

    from bs_podcasts.data.database import Database
    from bs_podcasts.data.repositories import LibraryRepository
    from bs_podcasts.domain import FeedData, FeedEpisodeData
    from bs_podcasts.playback.service import PlaybackService

    scratch = ROOT.parent / "tmp"
    scratch.mkdir(parents=True, exist_ok=True)
    with TemporaryDirectory(prefix="urlguard-", dir=scratch, ignore_cleanup_errors=True) as temporary:
        repository = LibraryRepository(Database(Path(temporary) / "library.db"))
        hostile = "file:///etc/passwd"

        feed_show = repository.add_show("https://feed.invalid/rss.xml", "Feed Show")
        repository.import_feed(
            feed_show.id,
            FeedData(title="Feed Show", episodes=(FeedEpisodeData("a", "A", media_url=hostile),)),
        )
        local_show = repository.add_show("/music/album", "Local Album", source="local")
        repository.import_feed(
            local_show.id,
            FeedData(title="Local Album", episodes=(FeedEpisodeData("b", "B", media_url=hostile),)),
        )

        engine = _StubEngine()
        playback = PlaybackService(repository, engine)
        try:
            feed_episode = repository.list_episodes(feed_show.id)[0]
            local_episode = repository.list_episodes(local_show.id)[0]

            rejects(
                "PlaybackService refuses a file:// enclosure from a feed",
                playback.load_episode,
                feed_episode.id,
                autoplay=False,
            )
            check(
                "nothing reached the engine from the feed show",
                engine.loaded == [],
            )
            accepts(
                "PlaybackService plays a locally-imported file",
                playback.load_episode,
                local_episode.id,
                autoplay=False,
            )
            check(
                "the local import did reach the engine",
                engine.loaded == [hostile],
            )
        finally:
            playback.shutdown()


def main() -> int:
    # --- scheme allowlist: the enclosure that turned Play into a file opener
    rejects("file:// media URL", ensure_web_url, "file:///home/user/.ssh/id_rsa")
    rejects("file:// UNC media URL", ensure_web_url, "file://attacker/share/payload.exe")
    rejects("javascript: URL", ensure_web_url, "javascript:alert(1)")
    rejects("data: URL", ensure_web_url, "data:text/html;base64,AAAA")
    rejects("scheme-only string", ensure_web_url, "notaurl")
    accepts("ordinary https URL", ensure_web_url, "https://example.com/ep.mp3")
    check("is_web_url(file://) is False", not is_web_url("file:///etc/passwd"))
    check("is_web_url(https) is True", is_web_url("https://example.com/x"))

    # --- media sources: app-produced local paths stay playable
    accepts("local posix path", ensure_media_source, "/home/user/Music/ep.mp3")
    accepts("windows drive path", ensure_media_source, r"C:\Users\me\ep.mp3")
    accepts("https media source", ensure_media_source, "https://example.com/ep.mp3")
    rejects("file:// media source without provenance", ensure_media_source, "file:///etc/passwd")
    # A local-audio import stores the user's own file as a file:// URL, so
    # that form is allowed when the caller vouches for it — but a UNC
    # authority is remote and stays refused either way.
    accepts(
        "local file:// with provenance",
        ensure_media_source,
        "file:///home/user/Music/ep.mp3",
        allow_file_url=True,
    )
    rejects(
        "UNC file:// even with provenance",
        ensure_media_source,
        "file://attacker/share/payload.exe",
        allow_file_url=True,
    )

    # --- SSRF: cloud metadata and loopback are link-local/loopback literals
    rejects("cloud metadata IP", ensure_fetchable, "http://169.254.169.254/latest/meta-data/")
    rejects("loopback IPv4", ensure_fetchable, "http://127.0.0.1:9000/admin")
    rejects("loopback IPv6", ensure_fetchable, "http://[::1]/admin")
    rejects("unspecified address", ensure_fetchable, "http://0.0.0.0/")
    # Private LAN stays reachable on purpose: a self-hosted podcast server is
    # a legitimate subscription. Documented in urlguard.ensure_fetchable.
    accepts("private LAN host", ensure_fetchable, "http://192.168.1.50:8000/feed.xml")

    # --- the external player is the primitive that can execute
    engine = ExternalPlayerEngine()
    engine.command = None  # never launch anything from a smoke check

    def load(source):
        engine.load(source)

    rejects("external engine UNC source", load, "file://host/share/payload.exe")

    # A web URL must get past the guard and fail later, on the missing command
    # — proving the refusal above was the guard, not the stubbed launcher.
    try:
        load("https://example.com/ep.mp3")
        FAILURES.append("external engine https source: no launcher error")
    except PlaybackUnavailable as exc:
        check(
            "external engine passes https to the launcher",
            "external audio player" in str(exc),
        )
    except UnsafeUrl:
        FAILURES.append("external engine https source: rejected by the guard")

    # --- XML: prefix-scan and encoding bypasses
    padding = b"<!-- " + b"x" * 5000 + b" -->"
    smuggled = (
        b'<?xml version="1.0"?>' + padding + b'<!DOCTYPE opml [<!ENTITY a "AA">]>'
        b'<opml><body><outline title="t" xmlUrl="http://a/f"/></body></opml>'
    )
    check("DTD past 4 KiB is detected", contains_dtd(smuggled))
    rejects("OPML with a smuggled DTD", import_opml, smuggled)

    utf16 = '<?xml version="1.0" encoding="UTF-16"?><!DOCTYPE rss><rss/>'.encode("utf-16")
    check("UTF-16 DTD is detected", contains_dtd(utf16))
    rejects("feed with a UTF-16 DTD", parse_feed, utf16)

    # The guard must not fire on ordinary content that merely mentions the word
    clean = (
        b'<?xml version="1.0"?><rss version="2.0"><channel><title>Doctype talk</title>'
        b"<item><title>Entity Framework</title></item></channel></rss>"
    )
    check("clean feed is not flagged as a DTD", not contains_dtd(clean))
    accepts("clean feed parses", parse_feed, clean)

    check_playback_provenance()

    for failure in FAILURES:
        print(f"FAIL {failure}")
    if FAILURES:
        print(f"\n{len(FAILURES)} check(s) failed")
        return 1
    print("url guard: all checks passed")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
