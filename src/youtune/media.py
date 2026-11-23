"""ffprobe / ffmpeg helpers shared by the renderers."""

import json
import shutil
import subprocess
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from fractions import Fraction
from pathlib import Path
from typing import Any

from youtune.errors import MediaError, RenderError

#: Shorts and burned videos never need more than 30 frames per second; a 60 fps
#: source is halved so zoom and pop animations cost half as many frames.
MAXIMUM_OUTPUT_FRAME_RATE = Fraction(30)
HARDWARE_ENCODER = "h264_videotoolbox"
#: Audio codecs an mp4 output can carry as they are.
COPYABLE_AUDIO_CODECS = frozenset({"aac", "mp3"})
ERROR_TAIL_CHARACTERS = 800

type CommandRunner = Callable[..., subprocess.CompletedProcess[str]]


@dataclass(frozen=True, slots=True)
class MediaInformation:
    """What the renderers need to know about a video file."""

    width: int
    height: int
    frame_rate: Fraction
    duration: float
    audio_codec: str | None

    @property
    def has_audio(self) -> bool:
        return self.audio_codec is not None

    @property
    def is_portrait(self) -> bool:
        return self.height > self.width


def find_tool(name: str) -> str:
    """Return the absolute path of ``name`` on PATH.

    Raises:
        MediaError: The tool is not installed.
    """
    path = shutil.which(name)
    if path is None:
        raise MediaError(f"{name} not found on PATH (install it with `brew install ffmpeg`)")
    return path


def probe(path: Path, *, runner: CommandRunner = subprocess.run) -> MediaInformation:
    """Read size, frame rate, duration and audio codec of ``path`` with ffprobe.

    Raises:
        MediaError: ffprobe is missing, failed, or the file has no video stream.
    """
    command = [
        find_tool("ffprobe"),
        "-v",
        "error",
        "-show_entries",
        "stream=codec_type,codec_name,width,height,r_frame_rate,avg_frame_rate"
        ":stream_side_data=rotation:format=duration",
        "-of",
        "json",
        str(path.resolve()),
    ]
    completed = runner(command, capture_output=True, text=True, check=False)
    if completed.returncode != 0:
        raise MediaError(f"ffprobe failed on {path.name}: {completed.stderr.strip()[:300]}")
    try:
        payload = json.loads(completed.stdout)
    except json.JSONDecodeError as error:
        raise MediaError(f"ffprobe returned invalid JSON for {path.name}") from error
    return parse_probe(payload, name=path.name)


def parse_probe(payload: dict[str, Any], *, name: str = "video") -> MediaInformation:
    """Build ``MediaInformation`` from ffprobe's JSON output."""
    streams = payload.get("streams") or []
    video = next((stream for stream in streams if stream.get("codec_type") == "video"), None)
    if video is None:
        raise MediaError(f"{name} has no video stream")
    audio = next((stream for stream in streams if stream.get("codec_type") == "audio"), None)
    width, height = int(video.get("width") or 0), int(video.get("height") or 0)
    if width <= 0 or height <= 0:
        raise MediaError(f"{name} reports no frame size")
    if _rotation(video) in (90, 270):
        width, height = height, width
    try:
        duration = float((payload.get("format") or {}).get("duration") or 0)
    except ValueError:
        duration = 0.0
    if duration <= 0:
        raise MediaError(f"{name} reports no duration")
    return MediaInformation(
        width=width,
        height=height,
        frame_rate=_frame_rate(video, name),
        duration=duration,
        audio_codec=str(audio.get("codec_name") or "unknown") if audio else None,
    )


def _rotation(video: dict[str, Any]) -> int:
    for entry in video.get("side_data_list") or []:
        if "rotation" in entry:
            return int(entry["rotation"]) % 360
    return 0


def _frame_rate(video: dict[str, Any], name: str) -> Fraction:
    for key in ("avg_frame_rate", "r_frame_rate"):
        try:
            rate = Fraction(str(video.get(key) or "0/0"))
        except (ValueError, ZeroDivisionError):
            continue
        if 1 <= rate <= 240:
            return rate
    raise MediaError(f"{name} reports no usable frame rate")


def output_frame_rate(source: Fraction) -> Fraction:
    """The frame rate a render uses: the source's, halved until it is at most 30."""
    rate = source
    while rate > MAXIMUM_OUTPUT_FRAME_RATE:
        rate /= 2
    return rate


def available_encoders(*, runner: CommandRunner = subprocess.run) -> frozenset[str]:
    """Names of the video and audio encoders this ffmpeg build offers."""
    completed = runner(
        [find_tool("ffmpeg"), "-hide_banner", "-encoders"],
        capture_output=True,
        text=True,
        check=False,
    )
    names = set()
    for line in completed.stdout.splitlines():
        parts = line.split()
        if len(parts) >= 2 and len(parts[0]) == 6 and parts[0][0] in "VAS":
            names.add(parts[1])
    return frozenset(names)


def run_ffmpeg(
    arguments: Sequence[str],
    *,
    description: str,
    runner: CommandRunner = subprocess.run,
) -> None:
    """Run ffmpeg with ``arguments`` (no program name), quietly.

    Raises:
        RenderError: ffmpeg exited non-zero; the message carries its stderr tail.
    """
    command = [find_tool("ffmpeg"), "-hide_banner", "-loglevel", "error", "-y", *arguments]
    completed = runner(command, capture_output=True, text=True, check=False)
    if completed.returncode != 0:
        tail = completed.stderr.strip()[-ERROR_TAIL_CHARACTERS:]
        raise RenderError(f"ffmpeg failed while {description}: {tail}")


def timebase_filter(frame_rate: Fraction) -> str:
    """Put a stream on an exact frame grid so overlays land on the same frames."""
    return f"settb={frame_rate.denominator}/{frame_rate.numerator},setpts=N"


def format_frame_rate(frame_rate: Fraction) -> str:
    return f"{frame_rate.numerator}/{frame_rate.denominator}"
