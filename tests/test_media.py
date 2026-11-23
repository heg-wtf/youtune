import subprocess
from fractions import Fraction
from pathlib import Path
from typing import Any

import pytest
from support import make_video, needs_ffmpeg

from youtune import media
from youtune.errors import MediaError, RenderError
from youtune.media import output_frame_rate, parse_probe, probe, run_ffmpeg, timebase_filter


def payload(**video: Any) -> dict[str, Any]:
    stream = {"codec_type": "video", "width": 1920, "height": 1080, "avg_frame_rate": "30/1"}
    stream.update(video)
    return {
        "streams": [stream, {"codec_type": "audio", "codec_name": "aac"}],
        "format": {"duration": "12.5"},
    }


class TestParseProbe:
    def test_reads_size_rate_duration_and_audio(self) -> None:
        information = parse_probe(payload())

        assert (information.width, information.height) == (1920, 1080)
        assert information.frame_rate == Fraction(30)
        assert information.duration == 12.5
        assert information.audio_codec == "aac"
        assert information.has_audio and not information.is_portrait

    def test_rotation_swaps_the_frame(self) -> None:
        information = parse_probe(payload(side_data_list=[{"rotation": -90}]))

        assert (information.width, information.height) == (1080, 1920)
        assert information.is_portrait

    def test_falls_back_to_r_frame_rate(self) -> None:
        information = parse_probe(payload(avg_frame_rate="0/0", r_frame_rate="24000/1001"))

        assert information.frame_rate == Fraction(24000, 1001)

    def test_silent_video(self) -> None:
        information = parse_probe(
            {
                "streams": [
                    {"codec_type": "video", "width": 10, "height": 10, "r_frame_rate": "25"}
                ],
                "format": {"duration": "1"},
            }
        )

        assert information.audio_codec is None and not information.has_audio

    @pytest.mark.parametrize(
        ("document", "message"),
        [
            ({"streams": [], "format": {"duration": "1"}}, "no video stream"),
            ({"streams": [{"codec_type": "video", "width": 0, "height": 5}]}, "frame size"),
            (payload() | {"format": {}}, "no duration"),
            (payload(avg_frame_rate="0/0", r_frame_rate="90000/0"), "frame rate"),
        ],
    )
    def test_rejects_unusable_files(self, document: dict[str, Any], message: str) -> None:
        with pytest.raises(MediaError, match=message):
            parse_probe(document)


@pytest.mark.parametrize(
    ("source", "expected"),
    [
        (Fraction(24000, 1001), Fraction(24000, 1001)),
        (Fraction(30), Fraction(30)),
        (Fraction(60), Fraction(30)),
        (Fraction(60000, 1001), Fraction(30000, 1001)),
        (Fraction(120), Fraction(30)),
    ],
)
def test_output_frame_rate_halves_until_thirty(source: Fraction, expected: Fraction) -> None:
    assert output_frame_rate(source) == expected


def test_timebase_filter_is_one_frame_per_tick() -> None:
    assert timebase_filter(Fraction(24000, 1001)) == "settb=1001/24000,setpts=N"


def test_available_encoders_parses_the_listing(monkeypatch: pytest.MonkeyPatch) -> None:
    listing = (
        "Encoders:\n V..... = Video\n ------\n V....D libx264              H.264\n"
        " V....D h264_videotoolbox    VideoToolbox H.264 Encoder\n A....D aac   AAC\n"
    )
    monkeypatch.setattr(media, "find_tool", lambda name: f"/bin/{name}")

    def runner(command: list[str], **keyword_arguments: Any) -> subprocess.CompletedProcess[str]:
        return subprocess.CompletedProcess(command, 0, stdout=listing, stderr="")

    encoders = media.available_encoders(runner=runner)

    assert {"libx264", "h264_videotoolbox", "aac"} <= encoders


def test_run_ffmpeg_reports_the_error_tail(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(media, "find_tool", lambda name: f"/bin/{name}")

    def runner(command: list[str], **keyword_arguments: Any) -> subprocess.CompletedProcess[str]:
        return subprocess.CompletedProcess(command, 1, stdout="", stderr="x" * 2000 + "boom")

    with pytest.raises(RenderError, match=r"while testing: x+boom$") as error:
        run_ffmpeg(["-i", "in"], description="testing", runner=runner)

    assert len(str(error.value)) < 900


def test_missing_tool_is_a_media_error(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(media.shutil, "which", lambda name: None)

    with pytest.raises(MediaError, match="ffprobe not found"):
        media.find_tool("ffprobe")


@needs_ffmpeg
def test_probe_reads_a_real_file(tmp_path: Path) -> None:
    video = make_video(tmp_path / "clip.mp4", size="320x240", rate="25", seconds=1.0)

    information = probe(video)

    assert (information.width, information.height) == (320, 240)
    assert information.frame_rate == Fraction(25)
    assert information.duration == pytest.approx(1.0, abs=0.1)
    assert information.audio_codec == "aac"
