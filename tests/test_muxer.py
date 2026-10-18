import subprocess
from pathlib import Path
from typing import Any

import pytest

from youtune.errors import MuxError
from youtune.muxer import embed_subtitles


class FakeFfmpeg:
    def __init__(self, *, returncode: int = 0, stderr: str = "") -> None:
        self.returncode = returncode
        self.stderr = stderr
        self.commands: list[list[str]] = []

    def __call__(self, command: list[str], **keyword_arguments: Any) -> Any:
        self.commands.append(command)
        Path(command[-1]).write_bytes(b"muxed")
        return subprocess.CompletedProcess(command, self.returncode, stdout="", stderr=self.stderr)


@pytest.fixture(autouse=True)
def ffmpeg_on_path(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("youtune.muxer.shutil.which", lambda name: "/opt/bin/ffmpeg")


@pytest.fixture
def media(tmp_path: Path) -> tuple[Path, Path]:
    video = tmp_path / "-tricky [abc].mp4"
    subtitle = tmp_path / "-tricky [abc].ko.srt"
    video.write_bytes(b"original")
    subtitle.write_text("1\n00:00:00,000 --> 00:00:01,000\nHello\n", encoding="utf-8")
    return video, subtitle


def test_replaces_video_with_muxed_copy(media: tuple[Path, Path]) -> None:
    video, subtitle = media
    ffmpeg = FakeFfmpeg()

    result = embed_subtitles(video, subtitle, runner=ffmpeg)

    assert result == video.resolve()
    assert video.read_bytes() == b"muxed"
    assert not list(video.parent.glob("*.embedding.*"))
    command = ffmpeg.commands[0]
    assert command[0] == "/opt/bin/ffmpeg"
    assert command[command.index("-c:s") + 1] == "mov_text"
    assert "language=kor" in command
    assert all(Path(argument).is_absolute() for argument in (command[5], command[7]))


def test_failure_keeps_original_and_cleans_up(media: tuple[Path, Path]) -> None:
    video, subtitle = media

    with pytest.raises(MuxError, match="Invalid data"):
        embed_subtitles(video, subtitle, runner=FakeFfmpeg(returncode=1, stderr="Invalid data"))

    assert video.read_bytes() == b"original"
    assert not list(video.parent.glob("*.embedding.*"))


def test_mkv_uses_srt_codec(tmp_path: Path) -> None:
    video = tmp_path / "clip.mkv"
    video.write_bytes(b"original")
    ffmpeg = FakeFfmpeg()

    embed_subtitles(video, tmp_path / "clip.ko.srt", runner=ffmpeg)

    command = ffmpeg.commands[0]
    assert command[command.index("-c:s") + 1] == "srt"


def test_unsupported_container_raises(tmp_path: Path) -> None:
    with pytest.raises(MuxError, match=r"'\.webm'"):
        embed_subtitles(tmp_path / "clip.webm", tmp_path / "clip.ko.srt", runner=FakeFfmpeg())


def test_missing_ffmpeg_raises(monkeypatch: pytest.MonkeyPatch, media: tuple[Path, Path]) -> None:
    monkeypatch.setattr("youtune.muxer.shutil.which", lambda name: None)

    with pytest.raises(MuxError, match="ffmpeg not found"):
        embed_subtitles(*media, runner=FakeFfmpeg())
