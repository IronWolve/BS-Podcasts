"""Focused check for chapter/transcript parsing (no network)."""

from bs_podcasts.feeds.listening_fetch import parse_chapters, parse_transcript


def require(condition, message):
    if not condition:
        raise RuntimeError(message)


def main() -> int:
    chapters = parse_chapters(b'{"version":"1.2.0","chapters":[{"startTime":0,"title":"Intro"},{"startTime":95.5,"title":"Topic","endTime":300}]}')
    require([c.title for c in chapters] == ["Intro", "Topic"], "JSON chapters")
    require(chapters[1].end_seconds == 300, "chapter end")
    srt = b"1\n00:00:01,000 --> 00:00:03,500\nHello there.\n\n2\n00:00:03,500 --> 00:00:06,000\nSecond cue continues\n\n3\n00:01:10,000 --> 00:01:12,000\nLater line.\n"
    segments = parse_transcript(srt, "application/srt")
    require(segments and segments[0].start_seconds == 1.0, "SRT start")
    require(any("Second cue" in s.text for s in segments), "SRT merge kept text")
    vtt = b"WEBVTT\n\n00:00.000 --> 00:02.000\n<v Host>Welcome back.\n\n00:02.000 --> 00:04.000\nToday we talk.\n"
    segments = parse_transcript(vtt, "text/vtt")
    require(segments and "<v" not in segments[0].text, "VTT tags stripped")
    js = b'{"segments":[{"startTime":0.5,"endTime":2,"body":"One."},{"startTime":2,"endTime":4,"body":"Two? Yes."}]}'
    segments = parse_transcript(js, "application/json")
    require(segments[0].start_seconds == 0.5, "JSON transcript start")
    plain = parse_transcript(b"Paragraph one.\n\nParagraph two.", "text/plain")
    require(len(plain) == 2, "plain paragraphs")
    print("BS Podcasts listening fetch parse check passed.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
