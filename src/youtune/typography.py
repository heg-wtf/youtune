"""Outlined text layers drawn with Pillow (the Homebrew ffmpeg has no drawtext or libass)."""

import re
from collections.abc import Sequence
from dataclasses import dataclass

from PIL import Image, ImageDraw, ImageFont

from youtune.fonts import FontFace, load_font

type Color = tuple[int, int, int, int]

WHITE: Color = (255, 255, 255, 255)
YELLOW: Color = (255, 225, 77, 255)
BLACK: Color = (0, 0, 0, 255)
NAMED_COLORS: dict[str, Color] = {
    "white": WHITE,
    "yellow": YELLOW,
    "red": (255, 82, 82, 255),
    "cyan": (94, 234, 255, 255),
    "green": (120, 230, 120, 255),
    "pink": (255, 128, 191, 255),
    "orange": (255, 159, 67, 255),
}
_HEX_COLOR = re.compile(r"^#([0-9a-fA-F]{6})$")
LINE_HEIGHT_RATIO = 1.25


@dataclass(frozen=True, slots=True)
class TextLine:
    text: str
    color: Color = WHITE


def parse_color(value: str) -> Color:
    """A color name from ``NAMED_COLORS`` or ``#RRGGBB``.

    Raises:
        ValueError: Anything else.
    """
    name = value.strip().lower()
    if name in NAMED_COLORS:
        return NAMED_COLORS[name]
    match = _HEX_COLOR.match(value.strip())
    if match is None:
        raise ValueError(f"unknown color {value!r} (use {', '.join(NAMED_COLORS)} or #RRGGBB)")
    number = int(match.group(1), 16)
    return (number >> 16 & 0xFF, number >> 8 & 0xFF, number & 0xFF, 255)


def stroke_width_for(font_size: int) -> int:
    return max(3, font_size // 10)


def wrap_text(text: str, font: ImageFont.FreeTypeFont, maximum_width: float) -> list[str]:
    """Break ``text`` into lines no wider than ``maximum_width``.

    Breaks at spaces; a single word wider than the line (long Korean runs,
    URLs) is broken between characters instead of overflowing.
    """
    lines: list[str] = []
    current = ""
    for word in text.split():
        candidate = f"{current} {word}".strip()
        if font.getlength(candidate) <= maximum_width:
            current = candidate
            continue
        if current:
            lines.append(current)
        current = ""
        for character in word:
            if current and font.getlength(current + character) > maximum_width:
                lines.append(current)
                current = character
            else:
                current += character
    if current:
        lines.append(current)
    return lines


def fit_text(
    text: str,
    color: Color,
    face: FontFace,
    *,
    font_size: int,
    maximum_width: float,
    maximum_lines: int,
    minimum_ratio: float = 0.7,
) -> tuple[list[TextLine], int]:
    """Wrap ``text`` at ``font_size``, shrinking (down to ``minimum_ratio``) until it
    fits in ``maximum_lines``. Returns the lines and the size used; at the smallest
    size the text is kept whole even when it needs more lines."""
    size = font_size
    minimum = max(8, round(font_size * minimum_ratio))
    while True:
        lines = wrap_text(text, load_font(face, size), maximum_width)
        if len(lines) <= maximum_lines or size <= minimum:
            return [TextLine(line, color) for line in lines], size
        size = max(minimum, round(size * 0.9))


def render_lines(
    lines: Sequence[TextLine],
    face: FontFace,
    font_size: int,
) -> Image.Image:
    """Centered, black-outlined lines on a tight transparent image."""
    font = load_font(face, font_size)
    stroke = stroke_width_for(font_size)
    line_height = round(font_size * LINE_HEIGHT_RATIO)
    widest = max((font.getlength(line.text) for line in lines), default=0)
    width = max(1, int(widest) + stroke * 2 + 8)
    height = max(1, line_height * len(lines) + stroke * 2)
    layer = Image.new("RGBA", (width, height), (0, 0, 0, 0))
    draw = ImageDraw.Draw(layer)
    for index, line in enumerate(lines):
        draw.text(
            (width / 2, stroke + index * line_height),
            line.text,
            font=font,
            fill=line.color,
            anchor="ma",
            stroke_width=stroke,
            stroke_fill=BLACK,
        )
    return layer


def paste_centered(
    layer: Image.Image, canvas_size: tuple[int, int], *, bottom: bool = False
) -> Image.Image:
    """``layer`` on a transparent canvas, centered horizontally (and vertically,
    or against the bottom edge)."""
    canvas = Image.new("RGBA", canvas_size, (0, 0, 0, 0))
    x = (canvas_size[0] - layer.width) // 2
    y = canvas_size[1] - layer.height if bottom else (canvas_size[1] - layer.height) // 2
    canvas.paste(layer, (x, max(0, y)), layer)
    return canvas
