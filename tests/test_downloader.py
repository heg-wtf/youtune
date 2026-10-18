from pathlib import Path
from typing import Any, Self

import pytest
from yt_dlp.utils import DownloadError as YtDlpDownloadError

from youtune.downloader import (
    CaptionTrack,
    VideoInformation,
    YoutubeDownloader,
    select_caption_track,
)
from youtune.errors import CaptionNotFoundError, DownloadError


def formats(name: str) -> list[dict[str, str]]:
    return [
        {"ext": "vtt", "url": f"https://captions/{name}.vtt"},
        {"ext": "json3", "url": f"https://captions/{name}.json3"},
    ]


def video(
    *,
    language: str | None = "en",
    manual: dict[str, Any] | None = None,
    automatic: dict[str, Any] | None = None,
) -> VideoInformation:
    return VideoInformation(
        url="https://www.youtube.com/watch?v=abc",
        identifier="abc",
        title="Title",
        language=language,
        manual_captions=manual or {},
        automatic_captions=automatic or {},
        raw={"id": "abc", "title": "Title"},
    )


class TestSelectCaptionTrack:
    def test_prefers_manual_korean(self) -> None:
        information = video(
            manual={"ko-KR": formats("ko"), "en": formats("en")},
            automatic={"en-orig": formats("en-orig")},
        )

        track = select_caption_track(information, None)

        assert track == CaptionTrack("ko", False, "https://captions/ko.json3")

    def test_prefers_manual_source_over_automatic(self) -> None:
        information = video(
            manual={"en-US": formats("en-US")}, automatic={"en-orig": formats("en-orig")}
        )

        track = select_caption_track(information, None)

        assert track == CaptionTrack("en", False, "https://captions/en-US.json3")

    def test_prefers_original_automatic_over_plain_automatic(self) -> None:
        information = video(
            automatic={"en": formats("en"), "en-orig": formats("en-orig"), "ko": formats("ko")}
        )

        track = select_caption_track(information, None)

        assert track == CaptionTrack("en", True, "https://captions/en-orig.json3")

    def test_ignores_automatic_korean_translation(self) -> None:
        information = video(automatic={"en-orig": formats("en-orig"), "ko": formats("ko")})

        assert select_caption_track(information, None).language == "en"

    def test_falls_back_to_plain_automatic(self) -> None:
        information = video(automatic={"en": formats("en")})

        assert select_caption_track(information, None).url == "https://captions/en.json3"

    def test_infers_language_from_original_track(self) -> None:
        information = video(language=None, automatic={"ja-orig": formats("ja-orig")})

        assert select_caption_track(information, None) == CaptionTrack(
            "ja", True, "https://captions/ja-orig.json3"
        )

    def test_explicit_source_language_wins(self) -> None:
        information = video(manual={"en": formats("en"), "fr": formats("fr")})

        assert select_caption_track(information, "fr").language == "fr"

    def test_korean_video_uses_its_own_automatic_captions(self) -> None:
        information = video(language="ko", automatic={"ko-orig": formats("ko-orig")})

        assert select_caption_track(information, None).language == "ko"

    def test_skips_tracks_without_json3(self) -> None:
        information = video(manual={"en": [{"ext": "vtt", "url": "https://captions/en.vtt"}]})

        with pytest.raises(CaptionNotFoundError):
            select_caption_track(information, None)

    def test_unknown_language_raises(self) -> None:
        with pytest.raises(CaptionNotFoundError, match="--source-language"):
            select_caption_track(video(language=None), None)

    def test_no_tracks_raises(self) -> None:
        with pytest.raises(CaptionNotFoundError, match="no 'en' captions"):
            select_caption_track(video(), None)


class FakeResponse:
    def __init__(self, payload: bytes) -> None:
        self.payload = payload

    def __enter__(self) -> Self:
        return self

    def __exit__(self, *exception_information: object) -> None:
        return None

    def read(self) -> bytes:
        return self.payload


class FakeClient:
    def __init__(
        self,
        options: dict[str, Any],
        *,
        information: dict[str, Any] | None = None,
        payload: bytes = b"",
        error: Exception | None = None,
    ) -> None:
        self.options = options
        self.information = information or {}
        self.payload = payload
        self.error = error
        self.calls: list[tuple[str, bool]] = []

    def __enter__(self) -> Self:
        return self

    def __exit__(self, *exception_information: object) -> None:
        return None

    def extract_info(self, url: str, download: bool = False) -> dict[str, Any]:
        self.calls.append((url, download))
        if self.error:
            raise self.error
        return self.information

    def urlopen(self, request: str) -> FakeResponse:
        if self.error:
            raise self.error
        return FakeResponse(self.payload)

    def prepare_filename(self, information: dict[str, Any]) -> str:
        directory = Path(self.options["outtmpl"]["default"]).parent
        return str(
            directory / f"{information['title']} [{information['id']}].{information['ext']}"
        )


class ClientRecorder:
    def __init__(self, **client_arguments: Any) -> None:
        self.client_arguments = client_arguments
        self.clients: list[FakeClient] = []

    def __call__(self, options: dict[str, Any]) -> FakeClient:
        client = FakeClient(options, **self.client_arguments)
        self.clients.append(client)
        return client


class TestYoutubeDownloader:
    def test_fetch_information_maps_fields(self, tmp_path: Path) -> None:
        recorder = ClientRecorder(
            information={
                "id": "abc",
                "title": "My Talk",
                "language": "en",
                "webpage_url": "https://www.youtube.com/watch?v=abc",
                "subtitles": {"en": formats("en")},
                "automatic_captions": None,
            }
        )
        downloader = YoutubeDownloader(tmp_path, client_factory=recorder)

        information = downloader.fetch_information("https://youtu.be/abc")

        assert information.identifier == "abc"
        assert information.title == "My Talk"
        assert information.url == "https://www.youtube.com/watch?v=abc"
        assert information.manual_captions == {"en": formats("en")}
        assert information.automatic_captions == {}
        assert recorder.clients[0].calls == [("https://youtu.be/abc", False)]
        assert recorder.clients[0].options["noplaylist"] is True

    def test_fetch_information_rejects_playlists(self, tmp_path: Path) -> None:
        recorder = ClientRecorder(information={"_type": "playlist", "id": "list"})
        downloader = YoutubeDownloader(tmp_path, client_factory=recorder)

        with pytest.raises(DownloadError, match="playlists"):
            downloader.fetch_information("https://www.youtube.com/playlist?list=x")

    def test_yt_dlp_errors_become_download_errors(self, tmp_path: Path) -> None:
        recorder = ClientRecorder(error=YtDlpDownloadError("Video unavailable"))
        downloader = YoutubeDownloader(tmp_path, client_factory=recorder)

        with pytest.raises(DownloadError, match="Video unavailable"):
            downloader.fetch_information("https://www.youtube.com/watch?v=gone")

    def test_download_caption_returns_decoded_text(self, tmp_path: Path) -> None:
        # Multi-byte UTF-8 payload ("Hangul" written in Korean).
        payload = '{"events": []} \ud55c\uae00'
        recorder = ClientRecorder(payload=payload.encode())
        downloader = YoutubeDownloader(tmp_path, client_factory=recorder)

        content = downloader.download_caption(CaptionTrack("en", True, "https://captions/x"))

        assert content == payload

    def test_download_caption_network_error(self, tmp_path: Path) -> None:
        recorder = ClientRecorder(error=OSError("connection reset"))
        downloader = YoutubeDownloader(tmp_path, client_factory=recorder)

        with pytest.raises(DownloadError, match="en caption track"):
            downloader.download_caption(CaptionTrack("en", True, "https://captions/x"))

    def test_download_video_returns_final_path(self, tmp_path: Path) -> None:
        final_path = tmp_path / "My Talk [abc].mp4"
        recorder = ClientRecorder(information={"requested_downloads": [{"filepath": final_path}]})
        downloader = YoutubeDownloader(tmp_path, video_format="best", client_factory=recorder)

        path = downloader.download_video(video())

        assert path == final_path
        client = recorder.clients[0]
        assert client.calls == [("https://www.youtube.com/watch?v=abc", True)]
        assert client.options["format"] == "best"
        assert client.options["merge_output_format"] == "mp4"
        assert client.options["noprogress"] is False

    def test_download_audio_uses_audio_format(self, tmp_path: Path) -> None:
        recorder = ClientRecorder(
            information={"requested_downloads": [{"filepath": str(tmp_path / "a.m4a")}]}
        )
        downloader = YoutubeDownloader(tmp_path, client_factory=recorder)

        assert downloader.download_audio(video()) == tmp_path / "a.m4a"
        assert recorder.clients[0].options["format"].startswith("ba")

    def test_download_without_reported_file_raises(self, tmp_path: Path) -> None:
        downloader = YoutubeDownloader(tmp_path, client_factory=ClientRecorder(information={}))

        with pytest.raises(DownloadError, match="did not report"):
            downloader.download_video(video())

    def test_output_base_path_strips_extension_only(self, tmp_path: Path) -> None:
        downloader = YoutubeDownloader(tmp_path, client_factory=ClientRecorder())
        information = video()
        information.raw.update(title="v1.2 release")

        assert downloader.output_base_path(information) == tmp_path / "v1.2 release [abc]"
