"""Command line entry point: download a YouTube video and write Korean subtitles."""

import argparse
import logging
import sys
from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Protocol
from urllib.parse import urlparse

from youtune.downloader import (
    DEFAULT_VIDEO_FORMAT,
    KOREAN,
    CaptionTrack,
    VideoInformation,
    YoutubeDownloader,
    select_caption_track,
)
from youtune.errors import CaptionNotFoundError, YoutuneError
from youtune.muxer import embed_subtitles
from youtune.subtitles import Cue, merge_into_sentences, parse_json3, write_srt
from youtune.transcriber import Transcript, WhisperTranscriber
from youtune.translator import ClaudeCodeTranslator, TranslatorSettings

logger = logging.getLogger("youtune")

ALLOWED_SCHEMES = ("http", "https")


class Downloader(Protocol):
    def fetch_information(self, url: str) -> VideoInformation: ...
    def download_caption(self, track: CaptionTrack) -> str: ...
    def download_video(self, video: VideoInformation) -> Path: ...
    def download_audio(self, video: VideoInformation) -> Path: ...
    def output_base_path(self, video: VideoInformation) -> Path: ...


class Translator(Protocol):
    def translate(self, cues: Sequence[Cue], *, source_language: str, title: str) -> list[Cue]: ...


class Transcriber(Protocol):
    def transcribe(self, media_path: Path, *, language: str | None = None) -> Transcript: ...


@dataclass(frozen=True, slots=True)
class Options:
    url: str
    output_directory: Path
    should_download_video: bool
    should_embed: bool
    should_force_transcription: bool
    source_language: str | None


@dataclass(frozen=True, slots=True)
class Components:
    downloader: Downloader
    translator: Translator
    transcriber: Transcriber


@dataclass(frozen=True, slots=True)
class SourceSubtitles:
    cues: list[Cue]
    language: str


@dataclass(frozen=True, slots=True)
class RunResult:
    video_path: Path | None
    korean_subtitle_path: Path
    source_subtitle_path: Path | None


def run(options: Options, components: Components) -> RunResult:
    """Execute the full pipeline: metadata, video, source captions, translation, output."""
    downloader = components.downloader
    video = downloader.fetch_information(options.url)
    logger.info("Video: %s [%s]", video.title, video.identifier)
    base_path = downloader.output_base_path(video)

    video_path = downloader.download_video(video) if options.should_download_video else None
    source = _obtain_source(options, components, video, video_path)
    logger.info("Source subtitles: %d cues (%s)", len(source.cues), source.language)

    source_subtitle_path = None
    korean_cues = source.cues
    if source.language != KOREAN:
        source_subtitle_path = write_srt(source.cues, _subtitle_path(base_path, source.language))
        logger.info("Translating %d cues into Korean with Claude Code", len(source.cues))
        korean_cues = components.translator.translate(
            source.cues, source_language=source.language, title=video.title
        )
    korean_subtitle_path = write_srt(korean_cues, _subtitle_path(base_path, KOREAN))

    if options.should_embed and video_path is not None:
        logger.info("Embedding Korean subtitles into %s", video_path.name)
        embed_subtitles(video_path, korean_subtitle_path)
    return RunResult(video_path, korean_subtitle_path, source_subtitle_path)


def _obtain_source(
    options: Options,
    components: Components,
    video: VideoInformation,
    video_path: Path | None,
) -> SourceSubtitles:
    if not options.should_force_transcription:
        try:
            track = select_caption_track(video, options.source_language)
        except CaptionNotFoundError as error:
            logger.info("No usable captions (%s); falling back to Whisper", error)
        else:
            kind = "automatic" if track.is_automatic else "manual"
            logger.info("Using %s '%s' captions", kind, track.language)
            content = components.downloader.download_caption(track)
            return _non_empty(merge_into_sentences(parse_json3(content)), track.language)
    return _transcribe(options, components, video, video_path)


def _transcribe(
    options: Options,
    components: Components,
    video: VideoInformation,
    video_path: Path | None,
) -> SourceSubtitles:
    media_path = video_path or components.downloader.download_audio(video)
    try:
        transcript = components.transcriber.transcribe(
            media_path, language=options.source_language or video.language
        )
    finally:
        if video_path is None:
            media_path.unlink(missing_ok=True)
    return _non_empty(merge_into_sentences(transcript.cues), transcript.language)


def _non_empty(cues: list[Cue], language: str) -> SourceSubtitles:
    if not cues:
        raise CaptionNotFoundError("no speech or caption text found in the video")
    return SourceSubtitles(cues, language.split("-")[0].lower())


def _subtitle_path(base_path: Path, language: str) -> Path:
    return base_path.with_name(f"{base_path.name}.{language}.srt")


def _url(value: str) -> str:
    parsed = urlparse(value)
    if parsed.scheme not in ALLOWED_SCHEMES or not parsed.netloc:
        raise argparse.ArgumentTypeError(f"expected an http(s) video URL, got '{value}'")
    return value


def _positive_integer(value: str) -> int:
    number = int(value)
    if number < 1:
        raise argparse.ArgumentTypeError(f"must be a positive integer, got {value}")
    return number


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="youtune",
        description="Download a YouTube video and generate Korean subtitles (.ko.srt).",
    )
    parser.add_argument("url", type=_url, help="YouTube video URL")
    parser.add_argument(
        "-o", "--output-dir", type=Path, default=Path(), help="output directory (default: .)"
    )
    parser.add_argument(
        "--no-video", action="store_true", help="only generate subtitles, skip the video download"
    )
    parser.add_argument(
        "--embed", action="store_true", help="embed the Korean subtitles into the video file"
    )
    parser.add_argument(
        "--source-language", help="source language code, e.g. en (default: detect)"
    )
    parser.add_argument(
        "--transcribe", action="store_true", help="ignore YouTube captions and use Whisper"
    )
    parser.add_argument(
        "--whisper-model", default="small", help="faster-whisper model size (default: small)"
    )
    parser.add_argument(
        "--model", default="sonnet", help="Claude model for translation (default: sonnet)"
    )
    parser.add_argument(
        "--batch-size", type=_positive_integer, default=40, help="lines per translation request"
    )
    parser.add_argument(
        "--workers", type=_positive_integer, default=4, help="parallel translation requests"
    )
    parser.add_argument(
        "--format", default=DEFAULT_VIDEO_FORMAT, help="yt-dlp format selector for the video"
    )
    parser.add_argument("-v", "--verbose", action="store_true", help="show debug logs")
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    parser = build_parser()
    arguments = parser.parse_args(argv)
    if arguments.no_video and arguments.embed:
        parser.error("--embed requires the video; drop --no-video")

    logging.basicConfig(
        level=logging.DEBUG if arguments.verbose else logging.INFO,
        format="%(message)s",
        stream=sys.stderr,
    )
    options = Options(
        url=arguments.url,
        output_directory=arguments.output_dir,
        should_download_video=not arguments.no_video,
        should_embed=arguments.embed,
        should_force_transcription=arguments.transcribe,
        source_language=arguments.source_language,
    )
    components = Components(
        downloader=YoutubeDownloader(arguments.output_dir, video_format=arguments.format),
        translator=ClaudeCodeTranslator(
            TranslatorSettings(
                model=arguments.model,
                batch_size=arguments.batch_size,
                workers=arguments.workers,
            )
        ),
        transcriber=WhisperTranscriber(arguments.whisper_model),
    )

    try:
        options.output_directory.mkdir(parents=True, exist_ok=True)
        result = run(options, components)
    except YoutuneError as error:
        logger.error("error: %s", error)
        return 1
    except OSError as error:
        logger.error("error: %s", error)
        return 1
    except KeyboardInterrupt:
        logger.error("interrupted")
        return 130

    if result.video_path is not None:
        print(f"video: {result.video_path}")
    if result.source_subtitle_path is not None:
        print(f"source subtitles: {result.source_subtitle_path}")
    print(f"korean subtitles: {result.korean_subtitle_path}")
    return 0
