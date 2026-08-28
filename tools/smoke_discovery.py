"""One local M2 feed, discovery, OPML, fallback, and search smoke flow."""

from pathlib import Path
from tempfile import TemporaryDirectory
import os


WORKSPACE = Path(__file__).resolve().parents[2]
SAMPLES = WORKSPACE / "repo/tests/samples"
LOCAL_TMP = WORKSPACE / "tmp"

os.environ.setdefault("TMPDIR", str(LOCAL_TMP))

from bs_podcasts.data import Database
from bs_podcasts.data.repositories import LibraryRepository
from bs_podcasts.directories import DirectoryService
from bs_podcasts.domain import DirectoryCandidate
from bs_podcasts.feeds import FeedFetcher, FeedParseError, parse_feed
from bs_podcasts.feeds.opml import import_opml
from bs_podcasts.services import LibraryService


def require(condition: bool, message: str):
    if not condition:
        raise RuntimeError(message)


class LocalResponse:
    def __init__(self, url: str, content: bytes, content_type: str):
        self.url = url
        self.content = content
        self.headers = {"Content-Type": content_type}
        self.status_code = 200
        self.encoding = "utf-8"
        self.closed = False

    def raise_for_status(self):
        return None

    def iter_content(self, chunk_size):
        yield self.content

    def close(self):
        self.closed = True


class LocalSession:
    max_redirects = 5

    def __init__(self, html: bytes, atom: bytes):
        self.html = html
        self.atom = atom
        self.responses = []

    def get(self, url, **kwargs):
        if url.endswith("bench.atom"):
            response = LocalResponse(url, self.atom, "application/atom+xml")
        else:
            response = LocalResponse(url, self.html, "text/html; charset=utf-8")
        self.responses.append(response)
        return response


class BrokenProvider:
    name = "broken"

    def search(self, query, limit=30):
        raise RuntimeError("provider unavailable")

    def browse(self, category="", limit=30):
        raise RuntimeError("provider unavailable")


class LocalProvider:
    name = "local"

    def search(self, query, limit=30):
        return [DirectoryCandidate("Bench Stories", "Local", "https://samples.invalid/bench.atom")]

    def browse(self, category="", limit=30):
        return self.search(category, limit)


def main() -> int:
    LOCAL_TMP.mkdir(parents=True, exist_ok=True)
    rss_content = (SAMPLES / "m1-feed.xml").read_bytes()
    atom_content = (SAMPLES / "m2-feed.atom").read_bytes()
    html_content = (SAMPLES / "m2-discovery.html").read_bytes()
    malformed = (SAMPLES / "m1-malformed.xml").read_bytes()

    require(len(parse_feed(rss_content).episodes) == 2, "RSS sample failed")
    categorized = parse_feed(
        b'''<rss xmlns:itunes="http://www.itunes.com/dtds/podcast-1.0.dtd" version="2.0"><channel>
        <title>Categorized</title><link>https://example.test/show</link>
        <itunes:category text="Society &amp; Culture"><itunes:category text="Philosophy"/></itunes:category>
        <itunes:category text="News"><itunes:category text="Daily News"/></itunes:category>
        <item><title>One</title><guid>one</guid><enclosure url="https://example.test/one.mp3" type="audio/mpeg"/></item>
        </channel></rss>'''
    )
    require(categorized.categories == ("Philosophy", "Daily News"), categorized.categories)
    atom = parse_feed(atom_content)
    require(len(atom.episodes) == 2, "Atom sample failed")
    try:
        parse_feed(malformed)
    except FeedParseError:
        pass
    else:
        raise RuntimeError("malformed feed was accepted")

    session = LocalSession(html_content, atom_content)
    fetcher = FeedFetcher(session=session)
    discovered = fetcher.fetch("https://samples.invalid/landing")
    require(discovered.final_url.endswith("bench.atom"), "HTML autodiscovery failed")
    require(len(parse_feed(discovered.content).episodes) == 2, "discovered feed failed")
    require(all(response.closed for response in session.responses), "feed responses were not closed")

    directory = DirectoryService([BrokenProvider(), LocalProvider()])
    candidates = directory.search("bench")
    require(len(candidates) == 1, "directory fallback did not return a result")

    with TemporaryDirectory(prefix="m2-smoke-", dir=LOCAL_TMP, ignore_cleanup_errors=True) as temporary:
        repository = LibraryRepository(Database(Path(temporary) / "library.db"))
        library = LibraryService(repository)
        show = library.add_subscription("https://samples.invalid/workshop.xml", "Workshop Radio")
        repository.import_feed(show.id, parse_feed(rss_content))
        repository.import_feed(show.id, categorized)
        require(repository.get_show(show.id).categories == "Philosophy, Daily News", "feed categories were not stored")
        before = len(library.shows())
        shows, episodes = library.search("foundation")
        require(not shows and len(episodes) == 1, "persisted search failed")

        exported = library.export_opml()
        entries = import_opml(exported)
        require(
            [entry.feed_url for entry in entries]
            == ["https://samples.invalid/workshop.xml"],
            "OPML round-trip changed subscription URLs",
        )
        require(len(library.shows()) == before, "directory fallback touched the library")

    print("BS Podcasts M2 discovery smoke flow passed.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
