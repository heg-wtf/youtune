"""Test helpers: a Korean font, short synthetic videos made by ffmpeg, and an
evaluator for the ffmpeg expressions the renderers build.

Tests that need real rendering are skipped when ffmpeg or a Korean font is
missing, so the pure-logic tests still run anywhere.
"""

import re
import shutil
import subprocess
from pathlib import Path

import pytest

from youtune.errors import FontNotFoundError
from youtune.fonts import FontFace, find_font

HAS_FFMPEG = shutil.which("ffmpeg") is not None and shutil.which("ffprobe") is not None


def _font_or_none() -> FontFace | None:
    try:
        return find_font()
    except FontNotFoundError:
        return None


FONT = _font_or_none()

needs_ffmpeg = pytest.mark.skipif(not HAS_FFMPEG, reason="ffmpeg / ffprobe not installed")
needs_font = pytest.mark.skipif(FONT is None, reason="no Korean font installed")


def make_video(
    path: Path,
    *,
    size: str = "640x360",
    rate: str = "30",
    seconds: float = 4.0,
    audio: bool = True,
) -> Path:
    """A test-pattern clip (with a sine tone unless ``audio`` is false)."""
    command = [
        "ffmpeg",
        "-hide_banner",
        "-loglevel",
        "error",
        "-y",
        "-f",
        "lavfi",
        "-i",
        f"testsrc2=size={size}:rate={rate}",
    ]
    if audio:
        command += ["-f", "lavfi", "-i", "sine=frequency=440:sample_rate=48000"]
    command += ["-t", str(seconds), "-c:v", "libx264", "-pix_fmt", "yuv420p"]
    command += ["-c:a", "aac", "-shortest"] if audio else []
    command.append(str(path))
    subprocess.run(command, check=True)
    return path


def evaluate(expression: str, **variables: float) -> float:
    """Evaluate the subset of ffmpeg's expression language the builders emit."""
    python_expression = re.sub(r"\bif\(", "if_(", expression)
    namespace = {
        "if_": lambda condition, then, otherwise: then if condition else otherwise,
        "lt": lambda left, right: left < right,
        "gt": lambda left, right: left > right,
        "between": lambda value, low, high: float(low <= value <= high),
        "max": max,
        "clip": lambda value, low, high: min(max(value, low), high),
        **variables,
    }
    return float(eval(python_expression, namespace))  # noqa: S307 - our own generated expression
