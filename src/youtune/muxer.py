"""Embed an SRT file into a video container as a soft subtitle track with ffmpeg."""

import shutil
import subprocess
from collections.abc import Callable
from pathlib import Path

from youtune.errors import MuxError

SUBTITLE_CODECS = {".mp4": "mov_text", ".m4v": "mov_text", ".mov": "mov_text", ".mkv": "srt"}

type CommandRunner = Callable[..., subprocess.CompletedProcess[str]]


def embed_subtitles(
    video_path: Path,
    subtitle_path: Path,
    *,
    language: str = "kor",
    runner: CommandRunner = subprocess.run,
) -> Path:
    """Replace ``video_path`` with a copy that carries ``subtitle_path`` as default track.

    Streams are copied without re-encoding. Existing subtitle tracks are dropped,
    so running it twice is safe. The original file is untouched on failure.

    Raises:
        MuxError: ffmpeg is missing, the container is unsupported or ffmpeg failed.
    """
    codec = SUBTITLE_CODECS.get(video_path.suffix.lower())
    if codec is None:
        raise MuxError(f"cannot embed subtitles into '{video_path.suffix}' files")
    ffmpeg = shutil.which("ffmpeg")
    if ffmpeg is None:
        raise MuxError("ffmpeg not found on PATH")

    # Absolute paths keep a title starting with '-' from being parsed as an option.
    source = video_path.resolve()
    temporary = source.with_name(f"{source.stem}.embedding{source.suffix}")
    command = [
        ffmpeg,
        "-y",
        "-loglevel",
        "error",
        "-i",
        str(source),
        "-i",
        str(subtitle_path.resolve()),
        "-map",
        "0:v",
        "-map",
        "0:a?",
        "-map",
        "1:s",
        "-c",
        "copy",
        "-c:s",
        codec,
        "-metadata:s:s:0",
        f"language={language}",
        "-disposition:s:0",
        "default",
        str(temporary),
    ]
    completed = runner(command, capture_output=True, text=True, check=False)
    if completed.returncode != 0:
        temporary.unlink(missing_ok=True)
        raise MuxError(f"ffmpeg failed: {completed.stderr.strip()[:500]}")
    return temporary.replace(source)
