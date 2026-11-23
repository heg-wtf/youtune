import json
from fractions import Fraction
from pathlib import Path
from typing import Any

import pytest
from support import make_video, needs_ffmpeg, needs_font

from youtune import cli, commands
from youtune.cli import Components, Options, RunResult
from youtune.downloader import VideoInformation, YoutubeDownloader
from youtune.media import MediaInformation
from youtune.subtitles import parse_srt


def video_information() -> VideoInformation:
    return VideoInformation(
        url="https://www.youtube.com/watch?v=abc",
        identifier="abc",
        title="Talk",
        language="en",
        manual_captions={},
        automatic_captions={},
        raw={"id": "abc", "channel": "Chan", "duration": 61},
    )


class TestDispatch:
    def test_a_subcommand_goes_to_its_handler(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        seen: list[str] = []
        monkeypatch.setitem(commands.HANDLERS, "frames", lambda arguments: seen.append("frames"))
        video = tmp_path / "v.mp4"
        video.write_bytes(b"x")

        exit_code = cli.main(["frames", str(video), "--at", "1", "-o", str(tmp_path)])

        assert exit_code == 0 and seen == ["frames"]

    def test_subcommand_errors_exit_one(
        self, tmp_path: Path, caplog: pytest.LogCaptureFixture
    ) -> None:
        manifest = tmp_path / "manifest.json"
        manifest.write_text("{}", encoding="utf-8")

        assert cli.main(["report", str(manifest)]) == 1
        assert "invalid report manifest" in caplog.text

    def test_missing_input_file_is_a_usage_error(self, tmp_path: Path) -> None:
        with pytest.raises(SystemExit) as exit_information:
            cli.main(["shorts", str(tmp_path / "none.mp4"), "--spec", "x", "-o", str(tmp_path)])

        assert exit_information.value.code == 2

    def test_the_url_form_still_works(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        def fake_run(options: Options, components: Components) -> RunResult:
            assert options.should_burn
            return RunResult(None, tmp_path / "v.ko.srt", None)

        monkeypatch.setattr(cli, "run", fake_run)

        assert cli.main(["https://youtu.be/abc", "-o", str(tmp_path), "--burn"]) == 0

    def test_burn_needs_the_video(self) -> None:
        with pytest.raises(SystemExit) as exit_information:
            cli.main(["https://youtu.be/abc", "--no-video", "--burn"])

        assert exit_information.value.code == 2


def test_json_output_describes_the_run(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    def fake_run(options: Options, components: Components) -> RunResult:
        return RunResult(
            tmp_path / "v.mp4",
            tmp_path / "v.ko.srt",
            tmp_path / "v.en.srt",
            video=video_information(),
            source_language="en",
            caption_kind="automatic",
            burned_video_path=tmp_path / "v.ko.mp4",
        )

    monkeypatch.setattr(cli, "run", fake_run)

    assert cli.main(["https://youtu.be/abc", "-o", str(tmp_path), "--json"]) == 0

    payload = json.loads(capsys.readouterr().out)
    assert payload["channel"] == "Chan"
    assert payload["duration_seconds"] == 61.0
    assert payload["caption_kind"] == "automatic"
    assert payload["burned_video"] == str(tmp_path / "v.ko.mp4")


def test_yt_dlp_logs_to_stderr(tmp_path: Path) -> None:
    options: dict[str, Any] = YoutubeDownloader(tmp_path)._options()

    assert options["logtostderr"] is True


@needs_ffmpeg
@needs_font
def test_shorts_command_renders_the_named_short(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    video = make_video(tmp_path / "v.mp4", size="640x360", seconds=8.0)
    spec = tmp_path / "spec.json"
    spec.write_text(
        json.dumps(
            {
                "shorts": [
                    {"name": "one", "segments": [{"start": 0.0, "end": 6.0}]},
                    {"name": "two", "segments": [{"start": 1.0, "end": 7.0}]},
                ]
            }
        ),
        encoding="utf-8",
    )
    subtitles = tmp_path / "v.ko.srt"
    subtitles.write_text("1\n00:00:01,000 --> 00:00:03,000\n자막\n", encoding="utf-8")

    exit_code = cli.main(
        [
            "shorts",
            str(video),
            "--spec",
            str(spec),
            "--subtitles",
            str(subtitles),
            "-o",
            str(tmp_path / "out"),
            "--only",
            "two",
        ]
    )

    assert exit_code == 0
    (rendered,) = json.loads(capsys.readouterr().out)
    assert rendered["name"] == "two"
    assert Path(rendered["video"]).is_file()
    assert (
        parse_srt(Path(rendered["subtitles"]).read_text(encoding="utf-8"))[0].start_milliseconds
        == 0
    )
    assert not (tmp_path / "out" / "one.mp4").exists()


def test_unknown_short_name_is_reported(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    video = tmp_path / "v.mp4"
    video.write_bytes(b"x")
    spec = tmp_path / "spec.json"
    spec.write_text(
        json.dumps({"shorts": [{"name": "one", "segments": [{"start": 0.0, "end": 6.0}]}]}),
        encoding="utf-8",
    )
    monkeypatch.setattr(
        commands, "probe", lambda path: MediaInformation(640, 360, Fraction(30), 10.0, None)
    )

    exit_code = cli.main(
        ["shorts", str(video), "--spec", str(spec), "-o", str(tmp_path), "--only", "zzz"]
    )

    assert exit_code == 1
    assert "no short named zzz" in caplog.text
