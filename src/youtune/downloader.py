"""YouTube access through yt-dlp: metadata, caption tracks and media downloads."""

import logging
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Protocol

import yt_dlp
from yt_dlp.utils import YoutubeDLError

from youtune.errors import CaptionNotFoundError, DownloadError

logger = logging.getLogger(__name__)

KOREAN = "ko"
CAPTION_FORMAT = "json3"
ORIGINAL_SUFFIX = "-orig"
# H.264 first: VP9/AV1 in mp4 does not play in QuickTime and many TVs.
DEFAULT_VIDEO_FORMAT = "bv*[vcodec^=avc1]+ba[ext=m4a]/bv*[ext=mp4]+ba[ext=m4a]/b[ext=mp4]/bv*+ba/b"
AUDIO_FORMAT = "ba[ext=m4a]/ba/b"
OUTPUT_TEMPLATE = "%(title)s [%(id)s].%(ext)s"

type InformationDictionary = dict[str, Any]
type CaptionTable = Mapping[str, list[dict[str, Any]]]


class YoutubeClient(Protocol):
    """The subset of ``yt_dlp.YoutubeDL`` this module relies on."""

    def __enter__(self) -> "YoutubeClient": ...
    def __exit__(self, *exception_information: object) -> None: ...
    def extract_info(self, url: str, download: bool = ...) -> InformationDictionary: ...
    def urlopen(self, request: str) -> Any: ...
    def prepare_filename(self, information: InformationDictionary) -> str: ...


type ClientFactory = Callable[[dict[str, Any]], YoutubeClient]


@dataclass(frozen=True, slots=True)
class VideoInformation:
    """Metadata of a single video as reported by yt-dlp."""

    url: str
    identifier: str
    title: str
    language: str | None
    manual_captions: CaptionTable
    automatic_captions: CaptionTable
    raw: InformationDictionary = field(repr=False)


@dataclass(frozen=True, slots=True)
class CaptionTrack:
    """A downloadable caption track in json3 format."""

    language: str
    is_automatic: bool
    url: str = field(repr=False)


def select_caption_track(video: VideoInformation, source_language: str | None) -> CaptionTrack:
    """Pick the best caption track to build Korean subtitles from.

    Order: manual Korean, then in the source language manual captions,
    original automatic captions (``<lang>-orig``) and plain automatic captions.
    Automatic captions in other languages are machine translations that
    YouTube rate-limits heavily, so they are never selected.

    Raises:
        CaptionNotFoundError: No suitable json3 track exists.
    """
    korean_track = _find_track(video.manual_captions, KOREAN, is_automatic=False)
    if korean_track is not None:
        return korean_track

    language = (
        source_language or video.language or _infer_original_language(video.automatic_captions)
    )
    if language is None:
        raise CaptionNotFoundError("cannot determine the video language; pass --source-language")

    candidates = (
        _find_track(video.manual_captions, language, is_automatic=False),
        _find_track(video.automatic_captions, language, is_automatic=True, is_original=True),
        _find_track(video.automatic_captions, language, is_automatic=True),
    )
    track = next((candidate for candidate in candidates if candidate is not None), None)
    if track is None:
        raise CaptionNotFoundError(f"no '{language}' captions available for {video.identifier}")
    return track


def _base_language(code: str) -> str:
    return code.removesuffix(ORIGINAL_SUFFIX).split("-")[0].lower()


def _find_track(
    table: CaptionTable, language: str, *, is_automatic: bool, is_original: bool = False
) -> CaptionTrack | None:
    for key, formats in table.items():
        if _base_language(key) != _base_language(language):
            continue
        if key.endswith(ORIGINAL_SUFFIX) != is_original:
            continue
        url = _json3_url(formats)
        if url is not None:
            return CaptionTrack(_base_language(key), is_automatic, url)
    return None


def _json3_url(formats: list[dict[str, Any]]) -> str | None:
    return next(
        (
            entry["url"]
            for entry in formats
            if entry.get("ext") == CAPTION_FORMAT and "url" in entry
        ),
        None,
    )


def _infer_original_language(table: CaptionTable) -> str | None:
    return next(
        (_base_language(key) for key in table if key.endswith(ORIGINAL_SUFFIX)),
        None,
    )


class YoutubeDownloader:
    """Thin wrapper around yt-dlp bound to an output directory."""

    def __init__(
        self,
        output_directory: Path,
        *,
        video_format: str = DEFAULT_VIDEO_FORMAT,
        client_factory: ClientFactory = yt_dlp.YoutubeDL,
    ) -> None:
        self.output_directory = output_directory
        self.video_format = video_format
        self._client_factory = client_factory

    def fetch_information(self, url: str) -> VideoInformation:
        """Fetch video metadata without downloading media."""
        raw = self._run(url, lambda client: client.extract_info(url, download=False))
        if raw.get("_type") == "playlist":
            raise DownloadError("playlists are not supported; pass a single video URL")
        return VideoInformation(
            url=raw.get("webpage_url") or url,
            identifier=raw["id"],
            title=raw.get("title") or raw["id"],
            language=raw.get("language"),
            manual_captions=raw.get("subtitles") or {},
            automatic_captions=raw.get("automatic_captions") or {},
            raw=raw,
        )

    def download_caption(self, track: CaptionTrack) -> str:
        """Download a caption track and return its text content."""

        def read(client: YoutubeClient) -> str:
            with client.urlopen(track.url) as response:
                return str(response.read().decode("utf-8"))

        return self._run(f"{track.language} caption track", read)

    def download_video(self, video: VideoInformation) -> Path:
        """Download the video (merged to mp4 when possible) and return its path."""
        return self._download(video, self.video_format, merge_output_format="mp4")

    def download_audio(self, video: VideoInformation) -> Path:
        """Download only the audio stream and return its path."""
        return self._download(video, AUDIO_FORMAT)

    def output_base_path(self, video: VideoInformation) -> Path:
        """Return the sanitized output path without extension, e.g. ``dir/Title [id]``."""
        with self._client_factory(self._options()) as client:
            filename = Path(client.prepare_filename({**video.raw, "ext": "mp4"}))
        return filename.with_name(filename.name.removesuffix(".mp4"))

    def _download(self, video: VideoInformation, video_format: str, **extra: str) -> Path:
        options = self._options(format=video_format, noprogress=False, **extra)
        result = self._run(
            video.url,
            lambda client: client.extract_info(video.url, download=True),
            options,
        )
        try:
            return Path(result["requested_downloads"][0]["filepath"])
        except (KeyError, IndexError) as error:
            raise DownloadError(
                f"yt-dlp did not report a downloaded file for {video.url}"
            ) from error

    def _options(self, **overrides: Any) -> dict[str, Any]:
        return {
            "outtmpl": {"default": str(self.output_directory / OUTPUT_TEMPLATE)},
            "noplaylist": True,
            "quiet": True,
            "no_warnings": False,
            **overrides,
        }

    def _run[T](
        self,
        target: str,
        action: Callable[[YoutubeClient], T],
        options: dict[str, Any] | None = None,
    ) -> T:
        try:
            with self._client_factory(options or self._options()) as client:
                return action(client)
        except (YoutubeDLError, OSError) as error:
            raise DownloadError(f"failed to fetch {target}: {error}") from error
