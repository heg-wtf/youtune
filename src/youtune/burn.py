"""Burn Korean subtitles into a video (hard subtitles every player shows)."""

import subprocess
import tempfile
from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path

from youtune.errors import RenderError
from youtune.fonts import FontFace, find_font
from youtune.image_track import TimedImage, concat_input_arguments, write_image_track
from youtune.media import (
    COPYABLE_AUDIO_CODECS,
    HARDWARE_ENCODER,
    CommandRunner,
    MediaInformation,
    available_encoders,
    probe,
    run_ffmpeg,
)
from youtune.subtitles import Cue, read_srt
from youtune.typography import (
    LINE_HEIGHT_RATIO,
    WHITE,
    fit_text,
    paste_centered,
    render_lines,
    stroke_width_for,
)

FONT_SIZE_RATIO = 0.055  # of the shorter frame side
TEXT_WIDTH_RATIO = 0.9
MAXIMUM_LINES = 2
BOX_LINES = 3  # room for a cue that still needs three lines at the smallest size
LANDSCAPE_BOTTOM_MARGIN_RATIO = 0.06
#: A vertical video is usually watched where an app's buttons cover the bottom.
PORTRAIT_BOTTOM_MARGIN_RATIO = 0.16
HARDWARE_QUALITY = "60"
SOFTWARE_CONSTANT_RATE_FACTOR = "20"


@dataclass(frozen=True, slots=True)
class SubtitleLayout:
    """Where the subtitle strip sits and how its text is sized."""

    width: int
    box_height: int
    top: int
    font_size: int
    text_width: int


def subtitle_layout(information: MediaInformation) -> SubtitleLayout:
    font_size = max(16, round(min(information.width, information.height) * FONT_SIZE_RATIO))
    box_height = (
        round(font_size * LINE_HEIGHT_RATIO) * BOX_LINES + stroke_width_for(font_size) * 2 + 4
    )
    margin_ratio = (
        PORTRAIT_BOTTOM_MARGIN_RATIO if information.is_portrait else LANDSCAPE_BOTTOM_MARGIN_RATIO
    )
    top = information.height - box_height - round(information.height * margin_ratio)
    return SubtitleLayout(
        width=information.width,
        box_height=box_height,
        top=max(0, top),
        font_size=font_size,
        text_width=round(information.width * TEXT_WIDTH_RATIO),
    )


def render_cue_images(
    cues: Sequence[Cue],
    layout: SubtitleLayout,
    face: FontFace,
    directory: Path,
) -> list[TimedImage]:
    """One full-width PNG per cue, text centered against the strip's bottom."""
    images: list[TimedImage] = []
    for number, cue in enumerate(cues, start=1):
        lines, size = fit_text(
            cue.text,
            WHITE,
            face,
            font_size=layout.font_size,
            maximum_width=layout.text_width,
            maximum_lines=MAXIMUM_LINES,
        )
        if not lines:
            continue
        name = f"cue{number:05d}.png"
        layer = render_lines(lines, face, size)
        paste_centered(layer, (layout.width, layout.box_height), bottom=True).save(
            directory / name
        )
        images.append(TimedImage(cue.start_milliseconds / 1000, cue.end_milliseconds / 1000, name))
    return images


def encoder_arguments(encoders: frozenset[str], *, prefer_hardware: bool) -> list[str]:
    if prefer_hardware and HARDWARE_ENCODER in encoders:
        return ["-c:v", HARDWARE_ENCODER, "-q:v", HARDWARE_QUALITY, "-tag:v", "avc1"]
    return ["-c:v", "libx264", "-preset", "veryfast", "-crf", SOFTWARE_CONSTANT_RATE_FACTOR]


def audio_arguments(information: MediaInformation) -> list[str]:
    if not information.has_audio:
        return []
    if information.audio_codec in COPYABLE_AUDIO_CODECS:
        return ["-map", "0:a:0", "-c:a", "copy"]
    return ["-map", "0:a:0", "-c:a", "aac", "-b:a", "192k"]


def burn_subtitles(
    video_path: Path,
    subtitle_path: Path,
    output_path: Path,
    *,
    face: FontFace | None = None,
    prefer_hardware: bool = True,
    runner: CommandRunner = subprocess.run,
) -> Path:
    """Write ``output_path``: ``video_path`` with ``subtitle_path`` drawn on it.

    Raises:
        RenderError: The subtitles are empty or ffmpeg failed.
        MediaError: ffmpeg / ffprobe is missing or the video cannot be read.
        FontNotFoundError: No Korean font is installed.
    """
    information = probe(video_path, runner=runner)
    cues = read_srt(subtitle_path)
    if not cues:
        raise RenderError(f"{subtitle_path.name} has no cues")
    face = face or find_font()
    layout = subtitle_layout(information)
    output_path.parent.mkdir(parents=True, exist_ok=True)
    partial = output_path.with_name(f"{output_path.stem}.partial{output_path.suffix}")
    with tempfile.TemporaryDirectory(prefix="youtune-burn-") as temporary:
        directory = Path(temporary)
        images = render_cue_images(cues, layout, face, directory)
        track = write_image_track(
            images,
            directory,
            size=(layout.width, layout.box_height),
            total_duration=information.duration,
        )
        filter_graph = (
            f"[1:v]format=rgba[subtitles];"
            f"[0:v][subtitles]overlay=0:{layout.top}:eof_action=pass,format=yuv420p[video]"
        )
        run_ffmpeg(
            [
                "-i",
                str(video_path.resolve()),
                *concat_input_arguments(track),
                "-filter_complex",
                filter_graph,
                "-map",
                "[video]",
                *audio_arguments(information),
                *encoder_arguments(
                    available_encoders(runner=runner), prefer_hardware=prefer_hardware
                ),
                "-movflags",
                "+faststart",
                str(partial.resolve()),
            ],
            description=f"burning subtitles into {video_path.name}",
            runner=runner,
        )
    return partial.replace(output_path)
