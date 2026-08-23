"""One representative 50-show / 1,000-episode model construction check."""

from pathlib import Path
from tempfile import TemporaryDirectory
import os
import time


WORKSPACE = Path(__file__).resolve().parents[2]
LOCAL_TMP = WORKSPACE / "tmp"
os.environ.setdefault("TMPDIR", str(LOCAL_TMP))

from bs_podcasts.data import Database
from bs_podcasts.data.repositories import LibraryRepository
from bs_podcasts.domain import FeedData, FeedEpisodeData
from bs_podcasts.ui.models import Episode as UiEpisode, EpisodeModel, Podcast as UiPodcast, PodcastModel


def require(condition: bool, message: str):
    if not condition:
        raise RuntimeError(message)


def main() -> int:
    LOCAL_TMP.mkdir(parents=True, exist_ok=True)
    started = time.monotonic()
    with TemporaryDirectory(prefix="large-smoke-", dir=LOCAL_TMP) as temporary:
        repository = LibraryRepository(Database(Path(temporary) / "library.db"))
        for show_number in range(50):
            show = repository.add_show(
                f"https://samples.invalid/show-{show_number}.xml",
                f"Sample Show {show_number:02d}",
            )
            repository.import_feed(
                show.id,
                FeedData(
                    title=f"Sample Show {show_number:02d}",
                    episodes=tuple(
                        FeedEpisodeData(
                            external_id=f"{show_number}-{episode_number}",
                            title=f"Episode {episode_number:02d}",
                            media_url=f"https://media.invalid/{show_number}/{episode_number}.mp3",
                            duration_seconds=1800,
                        )
                        for episode_number in range(20)
                    ),
                ),
            )
        shows = repository.list_shows()
        episodes = repository.list_episodes(limit=2000)
        podcast_model = PodcastModel(
            [
                UiPodcast(
                    show.title,
                    show.author,
                    show.episode_count,
                    show.new_count,
                    "#7CA8FF",
                    show_id=show.id,
                )
                for show in shows
            ]
        )
        episode_model = EpisodeModel(
            [
                UiEpisode(
                    episode.title,
                    episode.show_title,
                    "Today",
                    "30 min",
                    0.0,
                    "New",
                    "#58D6C2",
                    episode_id=episode.id,
                    show_id=episode.show_id,
                )
                for episode in episodes
            ]
        )
        require(podcast_model.rowCount() == 50, "podcast model count differs")
        require(episode_model.rowCount() == 1000, "episode model count differs")
    elapsed = time.monotonic() - started
    print(f"BS Podcasts larger-library smoke check passed in {elapsed:.2f}s.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
