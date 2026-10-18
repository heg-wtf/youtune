import json
from collections.abc import Sequence
from pathlib import Path
from typing import Any

import pytest

from youtune import cli
from youtune.cli import Components, Options, run
from youtune.downloader import CaptionTrack, VideoInformation
from youtune.errors import CaptionNotFoundError, DownloadError
from youtune.subtitles import Cue
from youtune.transcriber import Transcript

ENGLISH_CAPTIONS = json.dumps(
    {
        "events": [
            {"tStartMs": 0, "dDurationMs": 2000, "segs": [{"utf8": "Hello there."}]},
            {"tStartMs": 2000, "dDurationMs": 2000, "segs": [{"utf8": "Second sentence."}]},
        ]
    }
)
KOREAN_CAPTIONS = json.dumps(
    {
        "events": [
            {"tStartMs": 0, "dDurationMs": 2000, "segs": [{"utf8": "Native Korean caption."}]}
        ]
    }
)


def caption_formats(name: str) -> list[dict[str, str]]:
    return [{"ext": "json3", "url": f"https://captions/{name}"}]


def information(**captions: Any) -> VideoInformation:
    return VideoInformation(
        url="https://www.youtube.com/watch?v=abc",
        identifier="abc",
        title="Talk",
        language="en",
        manual_captions=captions.get("manual", {}),
        automatic_captions=captions.get("automatic", {}),
        raw={"id": "abc", "title": "Talk"},
    )


class FakeDownloader:
    def __init__(self, directory: Path, video: VideoInformation, captions: dict[str, str]) -> None:
        self.directory = directory
        self.video = video
        self.captions = captions
        self.downloaded: list[str] = []

    def fetch_information(self, url: str) -> VideoInformation:
        return self.video

    def download_caption(self, track: CaptionTrack) -> str:
        return self.captions[track.url.rsplit("/", 1)[1]]

    def download_video(self, video: VideoInformation) -> Path:
        self.downloaded.append("video")
        path = self.directory / "Talk [abc].mp4"
        path.write_bytes(b"video")
        return path

    def download_audio(self, video: VideoInformation) -> Path:
        self.downloaded.append("audio")
        path = self.directory / "Talk [abc].m4a"
        path.write_bytes(b"audio")
        return path

    def output_base_path(self, video: VideoInformation) -> Path:
        return self.directory / "Talk [abc]"


class FakeTranslator:
    def __init__(self) -> None:
        self.calls: list[tuple[int, str, str]] = []

    def translate(self, cues: Sequence[Cue], *, source_language: str, title: str) -> list[Cue]:
        self.calls.append((len(cues), source_language, title))
        return [
            Cue(cue.start_milliseconds, cue.end_milliseconds, f"translated {cue.text}")
            for cue in cues
        ]


class FakeTranscriber:
    def __init__(self, cues: list[Cue], language: str = "en") -> None:
        self.cues = cues
        self.language = language
        self.media: list[Path] = []

    def transcribe(self, media_path: Path, *, language: str | None = None) -> Transcript:
        assert media_path.exists()
        self.media.append(media_path)
        return Transcript(self.cues, self.language)


def options(directory: Path, **overrides: Any) -> Options:
    defaults: dict[str, Any] = {
        "url": "https://www.youtube.com/watch?v=abc",
        "output_directory": directory,
        "should_download_video": True,
        "should_embed": False,
        "should_force_transcription": False,
        "source_language": None,
    }
    return Options(**{**defaults, **overrides})


def components(
    downloader: FakeDownloader,
    translator: FakeTranslator | None = None,
    transcriber: FakeTranscriber | None = None,
) -> Components:
    return Components(
        downloader, translator or FakeTranslator(), transcriber or FakeTranscriber([])
    )


def test_translates_automatic_english_captions(tmp_path: Path) -> None:
    downloader = FakeDownloader(
        tmp_path,
        information(automatic={"en-orig": caption_formats("en")}),
        {"en": ENGLISH_CAPTIONS},
    )
    translator = FakeTranslator()

    result = run(options(tmp_path), components(downloader, translator))

    assert result.video_path == tmp_path / "Talk [abc].mp4"
    assert result.source_subtitle_path == tmp_path / "Talk [abc].en.srt"
    assert result.korean_subtitle_path == tmp_path / "Talk [abc].ko.srt"
    assert translator.calls == [(2, "en", "Talk")]
    korean = result.korean_subtitle_path.read_text(encoding="utf-8")
    assert "translated Hello there." in korean
    assert "Second sentence." in result.source_subtitle_path.read_text(encoding="utf-8")


def test_manual_korean_captions_skip_translation(tmp_path: Path) -> None:
    downloader = FakeDownloader(
        tmp_path, information(manual={"ko": caption_formats("ko")}), {"ko": KOREAN_CAPTIONS}
    )
    translator = FakeTranslator()

    result = run(
        options(tmp_path, should_download_video=False), components(downloader, translator)
    )

    assert translator.calls == []
    assert result.video_path is None
    assert result.source_subtitle_path is None
    assert "Native Korean caption." in result.korean_subtitle_path.read_text(encoding="utf-8")
    assert downloader.downloaded == []


def test_without_captions_transcribes_downloaded_video(tmp_path: Path) -> None:
    downloader = FakeDownloader(tmp_path, information(), {})
    transcriber = FakeTranscriber([Cue(0, 1000, "Spoken words.")])

    result = run(options(tmp_path), components(downloader, transcriber=transcriber))

    assert transcriber.media == [tmp_path / "Talk [abc].mp4"]
    assert (tmp_path / "Talk [abc].mp4").exists()
    assert "translated Spoken words." in result.korean_subtitle_path.read_text(encoding="utf-8")


def test_no_video_transcription_downloads_and_removes_audio(tmp_path: Path) -> None:
    downloader = FakeDownloader(tmp_path, information(), {})
    transcriber = FakeTranscriber([Cue(0, 1000, "Words.")])

    run(
        options(tmp_path, should_download_video=False),
        components(downloader, transcriber=transcriber),
    )

    assert downloader.downloaded == ["audio"]
    assert not (tmp_path / "Talk [abc].m4a").exists()


def test_force_transcription_ignores_captions(tmp_path: Path) -> None:
    downloader = FakeDownloader(
        tmp_path, information(manual={"en": caption_formats("en")}), {"en": ENGLISH_CAPTIONS}
    )
    transcriber = FakeTranscriber([Cue(0, 1000, "Korean speech.")], language="ko")
    translator = FakeTranslator()

    result = run(
        options(tmp_path, should_force_transcription=True),
        components(downloader, translator, transcriber),
    )

    assert translator.calls == []
    assert "Korean speech." in result.korean_subtitle_path.read_text(encoding="utf-8")


def test_empty_source_raises(tmp_path: Path) -> None:
    downloader = FakeDownloader(tmp_path, information(), {})

    with pytest.raises(CaptionNotFoundError, match="no speech"):
        run(options(tmp_path), components(downloader, transcriber=FakeTranscriber([])))


def test_embed_muxes_korean_subtitles(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    embedded: list[tuple[Path, Path]] = []
    monkeypatch.setattr(
        cli, "embed_subtitles", lambda video, subtitle: embedded.append((video, subtitle))
    )
    downloader = FakeDownloader(
        tmp_path, information(manual={"ko": caption_formats("ko")}), {"ko": KOREAN_CAPTIONS}
    )

    run(options(tmp_path, should_embed=True), components(downloader))

    assert embedded == [(tmp_path / "Talk [abc].mp4", tmp_path / "Talk [abc].ko.srt")]


class TestMain:
    @pytest.mark.parametrize(
        "url", ["file:///etc/passwd", "PaPpyQocMww", "javascript:alert(1)", "https://"]
    )
    def test_rejects_non_http_urls(self, url: str) -> None:
        with pytest.raises(SystemExit) as exit_information:
            cli.main([url])

        assert exit_information.value.code == 2

    def test_rejects_embed_without_video(self) -> None:
        with pytest.raises(SystemExit) as exit_information:
            cli.main(["https://youtu.be/abc", "--no-video", "--embed"])

        assert exit_information.value.code == 2

    @pytest.mark.parametrize("value", ["0", "-3"])
    def test_rejects_non_positive_workers(self, value: str) -> None:
        with pytest.raises(SystemExit):
            cli.main(["https://youtu.be/abc", "--workers", value])

    def test_reports_errors_with_exit_code_one(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
    ) -> None:
        def failing_run(options: Options, components: Components) -> None:
            raise DownloadError("Video unavailable")

        monkeypatch.setattr(cli, "run", failing_run)

        exit_code = cli.main(["https://youtu.be/abc", "-o", str(tmp_path / "out")])

        assert exit_code == 1
        assert "Video unavailable" in caplog.text
        assert (tmp_path / "out").is_dir()

    def test_prints_output_paths(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
    ) -> None:
        captured_options: list[Options] = []

        def fake_run(options: Options, components: Components) -> cli.RunResult:
            captured_options.append(options)
            return cli.RunResult(tmp_path / "v.mp4", tmp_path / "v.ko.srt", tmp_path / "v.en.srt")

        monkeypatch.setattr(cli, "run", fake_run)

        exit_code = cli.main(
            [
                "https://youtu.be/abc",
                "-o",
                str(tmp_path),
                "--source-language",
                "en",
                "--transcribe",
            ]
        )

        assert exit_code == 0
        output = capsys.readouterr().out
        assert f"korean subtitles: {tmp_path / 'v.ko.srt'}" in output
        assert f"video: {tmp_path / 'v.mp4'}" in output
        assert captured_options[0].source_language == "en"
        assert captured_options[0].should_force_transcription is True
