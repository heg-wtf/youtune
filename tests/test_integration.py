"""Integration tests against real YouTube and the real claude CLI.

Run with ``make test-integration`` (sets YOUTUNE_INTEGRATION=1).
"""

import os
import re
from pathlib import Path

import pytest

from youtune.downloader import YoutubeDownloader, select_caption_track
from youtune.subtitles import Cue, merge_into_sentences, parse_json3
from youtune.translator import ClaudeCodeTranslator, TranslatorSettings

pytestmark = [
    pytest.mark.integration,
    pytest.mark.skipif(
        os.environ.get("YOUTUNE_INTEGRATION") != "1", reason="set YOUTUNE_INTEGRATION=1"
    ),
]

VIDEO_URL = "https://www.youtube.com/watch?v=PaPpyQocMww"
HANGUL = re.compile(r"[\uac00-\ud7a3]")


def test_claude_cli_translates_into_korean() -> None:
    cues = [
        Cue(0, 2000, "Thanks for coming today."),
        Cue(2000, 4000, "We are going to talk about building agents."),
        Cue(4000, 6000, "Let's get started."),
    ]
    translator = ClaudeCodeTranslator(TranslatorSettings(batch_size=2, workers=2))

    result = translator.translate(cues, source_language="en", title="Integration test")

    assert [(cue.start_milliseconds, cue.end_milliseconds) for cue in result] == [
        (0, 2000),
        (2000, 4000),
        (4000, 6000),
    ]
    assert all(HANGUL.search(cue.text) for cue in result)


def test_youtube_captions_merge_into_sentences(tmp_path: Path) -> None:
    downloader = YoutubeDownloader(tmp_path)

    video = downloader.fetch_information(VIDEO_URL)
    track = select_caption_track(video, None)
    sentences = merge_into_sentences(parse_json3(downloader.download_caption(track)))

    assert track.language == "en"
    assert len(sentences) > 100
    assert sentences[-1].end_milliseconds > 3_000_000
    assert downloader.output_base_path(video).name.endswith(f"[{video.identifier}]")
