import subprocess
from fractions import Fraction
from pathlib import Path

import pytest
from support import make_video, needs_ffmpeg, needs_font

from youtune.burn import (
    HARDWARE_ENCODER,
    audio_arguments,
    burn_subtitles,
    encoder_arguments,
    subtitle_layout,
)
from youtune.errors import RenderError
from youtune.fonts import FontFace
from youtune.image_track import BLANK_NAME, TimedImage, build_concat_list
from youtune.media import MediaInformation, probe


def information(width: int, height: int, audio: str | None = "aac") -> MediaInformation:
    return MediaInformation(width, height, Fraction(30), 10.0, audio)


class TestConcatList:
    def entries(self, text: str) -> list[tuple[str, float]]:
        lines = text.strip().splitlines()[1:]
        pairs = []
        for index in range(0, len(lines) - 1, 2):
            pairs.append((lines[index][6:-1], float(lines[index + 1].split()[1])))
        return pairs

    def test_fills_gaps_with_blanks_and_repeats_the_last_file(self) -> None:
        text = build_concat_list(
            [TimedImage(1.0, 2.5, "a.png"), TimedImage(3.0, 5.0, "b.png")], total_duration=6.0
        )

        assert text.startswith("ffconcat version 1.0\n")
        assert self.entries(text) == [
            (BLANK_NAME, 1.0),
            ("a.png", 1.5),
            (BLANK_NAME, 0.5),
            ("b.png", 2.0),
            (BLANK_NAME, 1.0),
        ]
        assert text.strip().splitlines()[-1] == f"file '{BLANK_NAME}'"

    def test_orders_by_start_and_trims_overlaps(self) -> None:
        text = build_concat_list(
            [TimedImage(2.0, 4.0, "b.png"), TimedImage(0.0, 3.0, "a.png")], total_duration=4.0
        )

        assert self.entries(text)[:2] == [("a.png", 3.0), ("b.png", 1.0)]

    def test_drops_an_image_left_with_no_time(self) -> None:
        text = build_concat_list(
            [TimedImage(0.0, 3.0, "a.png"), TimedImage(1.0, 2.0, "hidden.png")],
            total_duration=3.0,
        )

        assert "hidden.png" not in text

    def test_trailing_blank_is_never_empty(self) -> None:
        text = build_concat_list([TimedImage(0.0, 3.0, "a.png")], total_duration=2.0)

        assert self.entries(text)[-1] == (BLANK_NAME, 0.1)


class TestLayout:
    def test_landscape_sits_near_the_bottom(self) -> None:
        layout = subtitle_layout(information(1920, 1080))

        assert layout.font_size == 59
        assert layout.width == 1920
        assert layout.top + layout.box_height == 1080 - round(1080 * 0.06)

    def test_portrait_keeps_clear_of_app_buttons(self) -> None:
        layout = subtitle_layout(information(1080, 1920))

        assert layout.top + layout.box_height == 1920 - round(1920 * 0.16)
        assert layout.text_width == 972


class TestArguments:
    def test_hardware_encoder_when_available(self) -> None:
        assert encoder_arguments(frozenset({HARDWARE_ENCODER}), prefer_hardware=True)[1] == (
            HARDWARE_ENCODER
        )

    def test_software_when_asked_or_missing(self) -> None:
        assert "libx264" in encoder_arguments(frozenset({HARDWARE_ENCODER}), prefer_hardware=False)
        assert "libx264" in encoder_arguments(frozenset(), prefer_hardware=True)

    def test_audio_copied_when_mp4_can_carry_it(self) -> None:
        assert audio_arguments(information(10, 10, "aac"))[-1] == "copy"
        assert audio_arguments(information(10, 10, "opus"))[3] == "aac"
        assert audio_arguments(information(10, 10, None)) == []


@needs_ffmpeg
@needs_font
def test_burns_cues_only_while_they_are_shown(tmp_path: Path, font: FontFace) -> None:
    video = make_video(tmp_path / "clip.mp4", size="640x360", seconds=4.0)
    subtitles = tmp_path / "clip.ko.srt"
    subtitles.write_text(
        "1\n00:00:01,000 --> 00:00:02,500\n한국어 자막 번인 테스트\n", encoding="utf-8"
    )
    layout = subtitle_layout(probe(video))

    output = burn_subtitles(video, subtitles, tmp_path / "out" / "clip.ko.mp4", face=font)

    def white_pixels(second: float) -> int:
        frame = subprocess.run(
            [
                "ffmpeg",
                "-loglevel",
                "error",
                "-ss",
                str(second),
                "-i",
                str(output),
                "-frames:v",
                "1",
                "-vf",
                f"crop=640:{layout.box_height}:0:{layout.top},format=gray",
                "-f",
                "rawvideo",
                "-",
            ],
            capture_output=True,
            check=True,
        ).stdout
        return sum(1 for value in frame if value > 240)

    original = probe(video)
    burned = probe(output)
    assert (burned.width, burned.height) == (original.width, original.height)
    assert burned.duration == pytest.approx(original.duration, abs=0.1)
    assert burned.audio_codec == "aac"
    assert white_pixels(1.5) > white_pixels(0.5) + 200
    assert white_pixels(3.0) < white_pixels(1.5)
    assert not (tmp_path / "out" / "clip.ko.partial.mp4").exists()


def test_empty_subtitles_are_refused(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    subtitles = tmp_path / "empty.srt"
    subtitles.write_text("", encoding="utf-8")
    monkeypatch.setattr("youtune.burn.probe", lambda path, runner: information(640, 360))

    with pytest.raises(RenderError, match="no cues"):
        burn_subtitles(tmp_path / "clip.mp4", subtitles, tmp_path / "out.mp4")
