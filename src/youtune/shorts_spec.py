"""The Shorts specification: which moments to cut and which effects to add.

The editor (a person or a model) writes it as JSON in source seconds; this
module validates it and reports every problem at once, with the JSON path of
each, so a bad spec can be fixed in one pass.

```json
{"shorts": [{
  "name": "sar-pull",
  "layout": "square",
  "title": [{"text": "포켓몬 카드 개봉하다가", "color": "white"},
            {"text": "인생 첫 SAR 등장!!", "color": "yellow"}],
  "highlight": ["SAR"],
  "poster_at": 10.8,
  "segments": [{
    "start": 443.4, "end": 447.8,
    "center": [[443.4, 0.5], [445.0, 0.62]],
    "zoom": [[443.6, 1.0], [443.9, 1.35]], "focus": [0.78, 0.24],
    "shakes": [[443.7, 444.3]], "flashes": [443.7], "freeze": 1.3,
    "pops": [{"start": 443.8, "end": 445.2, "text": "SAR!!!", "color": "yellow"}],
    "labels": [{"start": 444.0, "end": 446.0, "text": "15초 전…"}]
  }]
}]}
```
"""

import dataclasses
import json
import re
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from enum import StrEnum
from pathlib import Path
from typing import Any

from youtune.errors import SpecificationError
from youtune.typography import WHITE, YELLOW, Color, parse_color

type Keyframes = tuple[tuple[float, float], ...]

MINIMUM_SHORT_SECONDS = 5.0
MAXIMUM_SHORT_SECONDS = 180.0
MAXIMUM_SHORTS = 6
MAXIMUM_SEGMENTS = 12
MAXIMUM_OVERLAYS = 24
MAXIMUM_TITLE_LINES = 3
MAXIMUM_TITLE_CHARACTERS = 30
MAXIMUM_POP_CHARACTERS = 40
MAXIMUM_LABEL_CHARACTERS = 30
MAXIMUM_FREEZE_SECONDS = 5.0
MINIMUM_ZOOM = 1.0
MAXIMUM_ZOOM = 3.0
_NAME = re.compile(r"^[a-z0-9][a-z0-9-]{0,39}$")


class Layout(StrEnum):
    """``square``: a square crop over a blurred copy (landscape sources).
    ``full``: the frame cropped to 9:16 and filling the screen (portrait sources,
    or a landscape shot whose subject stays in one place)."""

    SQUARE = "square"
    FULL = "full"


@dataclass(frozen=True, slots=True)
class TimedText:
    start: float  # source seconds
    end: float
    text: str
    color: Color = YELLOW


@dataclass(frozen=True, slots=True)
class Segment:
    start: float  # source seconds
    end: float
    center: Keyframes = ()  # (source seconds, horizontal subject position 0..1)
    zoom: Keyframes = ()  # (source seconds, zoom factor ≥ 1)
    focus: tuple[float, float] = (0.5, 0.5)  # zoom target inside the crop, 0..1
    shakes: tuple[tuple[float, float], ...] = ()
    flashes: tuple[float, ...] = ()
    pops: tuple[TimedText, ...] = ()
    labels: tuple[TimedText, ...] = ()
    freeze: float = 0.0  # seconds the last frame holds while the audio plays on

    @property
    def video_duration(self) -> float:
        return self.end - self.start

    @property
    def duration(self) -> float:
        return self.video_duration + self.freeze


@dataclass(frozen=True, slots=True)
class ShortSpecification:
    name: str
    segments: tuple[Segment, ...]
    title: tuple[tuple[str, Color], ...] = ()
    layout: Layout | None = None  # None: chosen from the source's orientation
    highlight: tuple[str, ...] = ()
    poster_at: float | None = None  # output seconds
    description: str = field(default="", compare=False)

    @property
    def duration(self) -> float:
        return sum(segment.duration for segment in self.segments)


class _Collector:
    """Gathers every validation problem with the JSON path it came from."""

    def __init__(self) -> None:
        self.problems: list[str] = []

    def add(self, path: str, message: str) -> None:
        self.problems.append(f"{path}: {message}")


def load_specifications(path: Path, *, video_duration: float) -> list[ShortSpecification]:
    """Read and validate a spec file.

    Raises:
        SpecificationError: The file is not JSON or any field is invalid.
    """
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as error:
        raise SpecificationError(f"cannot read {path.name}: {error}") from error
    return parse_specifications(payload, video_duration=video_duration)


def parse_specifications(payload: Any, *, video_duration: float) -> list[ShortSpecification]:
    """Validate a decoded spec; raises ``SpecificationError`` listing every problem."""
    collector = _Collector()
    raw_shorts = payload.get("shorts") if isinstance(payload, dict) else None
    if not isinstance(raw_shorts, list) or not raw_shorts:
        raise SpecificationError("shorts: expected a non-empty list")
    if len(raw_shorts) > MAXIMUM_SHORTS:
        collector.add("shorts", f"at most {MAXIMUM_SHORTS} shorts")
    specifications: list[ShortSpecification] = []
    seen_names: set[str] = set()
    for index, raw in enumerate(raw_shorts[:MAXIMUM_SHORTS]):
        specification = _short(raw, f"shorts[{index}]", video_duration, collector)
        if specification is None:
            continue
        if specification.name in seen_names:
            collector.add(f"shorts[{index}].name", f"duplicate name {specification.name!r}")
        seen_names.add(specification.name)
        specifications.append(specification)
    if collector.problems:
        raise SpecificationError("invalid shorts spec:\n- " + "\n- ".join(collector.problems))
    return specifications


def _short(
    raw: Any, path: str, video_duration: float, collector: _Collector
) -> ShortSpecification | None:
    if not isinstance(raw, dict):
        collector.add(path, "expected an object")
        return None
    name = raw.get("name")
    if not isinstance(name, str) or not _NAME.match(name):
        collector.add(f"{path}.name", "lowercase letters, digits and '-', at most 40")
        name = "invalid"
    layout = None
    if raw.get("layout") is not None:
        try:
            layout = Layout(str(raw["layout"]))
        except ValueError:
            collector.add(f"{path}.layout", "one of 'square', 'full'")
    title = _title(raw.get("title", []), f"{path}.title", collector)
    highlight = raw.get("highlight", [])
    if not isinstance(highlight, list) or not all(
        isinstance(word, str) and word.strip() for word in highlight
    ):
        collector.add(f"{path}.highlight", "expected a list of words")
        highlight = []
    raw_segments = raw.get("segments")
    if not isinstance(raw_segments, list) or not raw_segments:
        collector.add(f"{path}.segments", "expected a non-empty list")
        return None
    if len(raw_segments) > MAXIMUM_SEGMENTS:
        collector.add(f"{path}.segments", f"at most {MAXIMUM_SEGMENTS} segments")
    segments = [
        segment
        for segment_index, raw_segment in enumerate(raw_segments[:MAXIMUM_SEGMENTS])
        if (
            segment := _segment(
                raw_segment, f"{path}.segments[{segment_index}]", video_duration, collector
            )
        )
        is not None
    ]
    specification = ShortSpecification(
        name=name,
        segments=tuple(segments),
        title=title,
        layout=layout,
        highlight=tuple(word.strip() for word in highlight),
        description=str(raw.get("description") or ""),
    )
    if len(segments) == len(raw_segments):
        duration = specification.duration
        if not MINIMUM_SHORT_SECONDS <= duration <= MAXIMUM_SHORT_SECONDS:
            collector.add(
                path,
                f"total length {duration:.1f}s is outside "
                f"{MINIMUM_SHORT_SECONDS:.0f}-{MAXIMUM_SHORT_SECONDS:.0f}s",
            )
        poster_at = raw.get("poster_at")
        if poster_at is not None:
            if not _is_number(poster_at) or not 0 <= float(poster_at) < duration:
                collector.add(f"{path}.poster_at", f"output seconds within 0-{duration:.1f}")
            else:
                specification = dataclasses.replace(specification, poster_at=float(poster_at))
    return specification


def _title(raw: Any, path: str, collector: _Collector) -> tuple[tuple[str, Color], ...]:
    if not isinstance(raw, list):
        collector.add(path, "expected a list of {text, color}")
        return ()
    if len(raw) > MAXIMUM_TITLE_LINES:
        collector.add(path, f"at most {MAXIMUM_TITLE_LINES} lines")
    lines = []
    for index, entry in enumerate(raw[:MAXIMUM_TITLE_LINES]):
        text_and_color = _text_and_color(
            entry, f"{path}[{index}]", MAXIMUM_TITLE_CHARACTERS, WHITE, collector
        )
        if text_and_color is not None:
            lines.append(text_and_color)
    return tuple(lines)


def _segment(raw: Any, path: str, video_duration: float, collector: _Collector) -> Segment | None:
    if not isinstance(raw, dict):
        collector.add(path, "expected an object")
        return None
    start, end = _number(raw.get("start")), _number(raw.get("end"))
    if start is None or end is None:
        collector.add(path, "start and end must be numbers (source seconds)")
        return None
    if not 0 <= start < end <= video_duration + 0.05:
        collector.add(path, f"needs 0 ≤ start < end ≤ {video_duration:.2f} (got {start}-{end})")
        return None
    end = min(end, video_duration)
    freeze = raw.get("freeze", 0)
    if not _is_number(freeze) or not 0 <= float(freeze) <= MAXIMUM_FREEZE_SECONDS:
        collector.add(f"{path}.freeze", f"seconds within 0-{MAXIMUM_FREEZE_SECONDS:.0f}")
        freeze = 0
    window = (start, end + float(freeze))
    center = _keyframes(raw.get("center", []), f"{path}.center", (0.0, 1.0), collector)
    zoom = _keyframes(raw.get("zoom", []), f"{path}.zoom", (MINIMUM_ZOOM, MAXIMUM_ZOOM), collector)
    focus = raw.get("focus", [0.5, 0.5])
    if (
        not isinstance(focus, list)
        or len(focus) != 2
        or not all(_is_number(value) and 0 <= float(value) <= 1 for value in focus)
    ):
        collector.add(f"{path}.focus", "[x, y] within 0..1")
        focus = [0.5, 0.5]
    shakes = _windows(raw.get("shakes", []), f"{path}.shakes", window, collector)
    flashes = _instants(raw.get("flashes", []), f"{path}.flashes", window, collector)
    pops = _timed_texts(
        raw.get("pops", []), f"{path}.pops", window, MAXIMUM_POP_CHARACTERS, YELLOW, collector
    )
    labels = _timed_texts(
        raw.get("labels", []), f"{path}.labels", window, MAXIMUM_LABEL_CHARACTERS, WHITE, collector
    )
    return Segment(
        start=start,
        end=end,
        center=center,
        zoom=zoom,
        focus=(float(focus[0]), float(focus[1])),
        shakes=shakes,
        flashes=flashes,
        pops=pops,
        labels=labels,
        freeze=float(freeze),
    )


def _keyframes(
    raw: Any, path: str, value_range: tuple[float, float], collector: _Collector
) -> Keyframes:
    if not isinstance(raw, list):
        collector.add(path, "expected a list of [source seconds, value]")
        return ()
    points: list[tuple[float, float]] = []
    for index, entry in enumerate(raw):
        if (
            not isinstance(entry, list)
            or len(entry) != 2
            or not all(_is_number(value) for value in entry)
        ):
            collector.add(f"{path}[{index}]", "expected [source seconds, value]")
            return ()
        time, value = float(entry[0]), float(entry[1])
        if not value_range[0] <= value <= value_range[1]:
            collector.add(f"{path}[{index}]", f"value within {value_range[0]}-{value_range[1]}")
            return ()
        if points and time <= points[-1][0]:
            collector.add(f"{path}[{index}]", "times must increase")
            return ()
        points.append((time, value))
    return tuple(points)


def _windows(
    raw: Any, path: str, window: tuple[float, float], collector: _Collector
) -> tuple[tuple[float, float], ...]:
    def parse(entry: Any, entry_path: str) -> tuple[float, float] | None:
        if (
            not isinstance(entry, list)
            or len(entry) != 2
            or not all(_is_number(value) for value in entry)
            or not float(entry[0]) < float(entry[1])
        ):
            collector.add(entry_path, "expected [start, end] with start < end")
            return None
        if not _inside(float(entry[0]), window) or not _inside(float(entry[1]), window):
            collector.add(entry_path, _outside_message(window))
            return None
        return float(entry[0]), float(entry[1])

    return tuple(_each(raw, path, parse, collector))


def _instants(
    raw: Any, path: str, window: tuple[float, float], collector: _Collector
) -> tuple[float, ...]:
    def parse(entry: Any, entry_path: str) -> float | None:
        if not _is_number(entry):
            collector.add(entry_path, "expected source seconds")
            return None
        if not _inside(float(entry), window):
            collector.add(entry_path, _outside_message(window))
            return None
        return float(entry)

    return tuple(_each(raw, path, parse, collector))


def _timed_texts(
    raw: Any,
    path: str,
    window: tuple[float, float],
    maximum_characters: int,
    default_color: Color,
    collector: _Collector,
) -> tuple[TimedText, ...]:
    def parse(entry: Any, entry_path: str) -> TimedText | None:
        if not isinstance(entry, dict):
            collector.add(entry_path, "expected {start, end, text, color?}")
            return None
        start, end = _number(entry.get("start")), _number(entry.get("end"))
        if start is None or end is None or not start < end:
            collector.add(entry_path, "start < end, both source seconds")
            return None
        if not _inside(start, window) or not _inside(end, window):
            collector.add(entry_path, _outside_message(window))
            return None
        text_and_color = _text_and_color(
            entry, entry_path, maximum_characters, default_color, collector
        )
        if text_and_color is None:
            return None
        return TimedText(start, end, *text_and_color)

    return tuple(_each(raw, path, parse, collector))


def _each[T](
    raw: Any,
    path: str,
    parse: Callable[[Any, str], T | None],
    collector: _Collector,
) -> list[T]:
    if not isinstance(raw, list):
        collector.add(path, "expected a list")
        return []
    if len(raw) > MAXIMUM_OVERLAYS:
        collector.add(path, f"at most {MAXIMUM_OVERLAYS} entries")
    return [
        value
        for index, entry in enumerate(raw[:MAXIMUM_OVERLAYS])
        if (value := parse(entry, f"{path}[{index}]")) is not None
    ]


def _text_and_color(
    entry: Any,
    path: str,
    maximum_characters: int,
    default_color: Color,
    collector: _Collector,
) -> tuple[str, Color] | None:
    if not isinstance(entry, dict):
        collector.add(path, "expected {text, color?}")
        return None
    text = entry.get("text")
    if not isinstance(text, str) or not text.strip():
        collector.add(f"{path}.text", "expected non-empty text")
        return None
    if len(text.strip()) > maximum_characters:
        collector.add(f"{path}.text", f"at most {maximum_characters} characters")
        return None
    color = default_color
    if entry.get("color") is not None:
        try:
            color = parse_color(str(entry["color"]))
        except ValueError as error:
            collector.add(f"{path}.color", str(error))
    return text.strip(), color


def _number(value: Any) -> float | None:
    return float(value) if _is_number(value) else None


def _is_number(value: Any) -> bool:
    return isinstance(value, int | float) and not isinstance(value, bool)


def _inside(time: float, window: tuple[float, float]) -> bool:
    return window[0] - 1e-6 <= time <= window[1] + 1e-6


def _outside_message(window: tuple[float, float]) -> str:
    return f"must lie inside the segment ({window[0]:.2f}-{window[1]:.2f}s, freeze included)"


def describe(specifications: Sequence[ShortSpecification]) -> str:
    """One line per short: name, length, segment ranges — for logs and reports."""
    lines = []
    for specification in specifications:
        ranges = ", ".join(
            f"{segment.start:.1f}-{segment.end:.1f}" for segment in specification.segments
        )
        lines.append(f"{specification.name}: {specification.duration:.1f}s ({ranges})")
    return "\n".join(lines)
