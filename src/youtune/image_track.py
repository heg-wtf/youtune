"""A run of timed still images as one ffmpeg input (the concat demuxer).

Overlaying hundreds of subtitle PNGs as separate inputs does not scale, so every
cue image goes into one ``ffconcat`` list with transparent blanks between them:
one input, one ``overlay``, frame-exact timing.
"""

from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path

from PIL import Image

BLANK_NAME = "blank.png"
LIST_NAME = "track.ffconcat"
#: Shortest blank worth writing; smaller gaps are absorbed by the next image.
MINIMUM_GAP_SECONDS = 0.001
TRAILING_BLANK_SECONDS = 0.1


@dataclass(frozen=True, slots=True)
class TimedImage:
    """``name`` (a file next to the list) shown from ``start`` to ``end`` seconds."""

    start: float
    end: float
    name: str


def build_concat_list(images: Sequence[TimedImage], *, total_duration: float) -> str:
    """The ``ffconcat`` text for ``images``, with blanks filling every gap.

    Images are ordered by start; one that starts before the previous one ends
    starts when it ends, and one left with no time is dropped. The last entry
    is listed twice because the demuxer ignores the duration of the final file.
    """
    entries: list[tuple[str, float]] = []
    cursor = 0.0
    for image in sorted(images, key=lambda entry: entry.start):
        start = max(image.start, cursor)
        if image.end - start < MINIMUM_GAP_SECONDS:
            continue
        if start - cursor >= MINIMUM_GAP_SECONDS:
            entries.append((BLANK_NAME, start - cursor))
        entries.append((image.name, image.end - start))
        cursor = image.end
    entries.append((BLANK_NAME, max(total_duration - cursor, TRAILING_BLANK_SECONDS)))
    lines = ["ffconcat version 1.0"]
    for name, duration in entries:
        lines += [f"file '{name}'", f"duration {duration:.6f}"]
    lines.append(f"file '{entries[-1][0]}'")
    return "\n".join(lines) + "\n"


def write_image_track(
    images: Sequence[TimedImage],
    directory: Path,
    *,
    size: tuple[int, int],
    total_duration: float,
) -> Path:
    """Write the blank image and the list into ``directory`` (where the images
    already are) and return the list's path."""
    Image.new("RGBA", size, (0, 0, 0, 0)).save(directory / BLANK_NAME)
    path = directory / LIST_NAME
    path.write_text(build_concat_list(images, total_duration=total_duration), encoding="utf-8")
    return path


def concat_input_arguments(list_path: Path) -> list[str]:
    return ["-f", "concat", "-safe", "0", "-i", str(list_path)]
