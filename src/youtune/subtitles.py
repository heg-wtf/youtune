"""Subtitle model, YouTube json3 parsing, sentence merging and SRT serialization."""

import json
import re
from collections.abc import Iterator, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from youtune.errors import SubtitleParseError

SENTENCE_TERMINATORS = (".", "?", "!", "…", "。", "？", "！")  # noqa: RUF001 - full-width CJK punctuation
CLAUSE_TERMINATORS = (",", ";", ":", "，", "、")  # noqa: RUF001 - full-width CJK punctuation
MINIMUM_CUE_MILLISECONDS = 500

_WHITESPACE = re.compile(r"\s+")
_SRT_TIMING = re.compile(
    r"^\s*(\d{1,2}):(\d{2}):(\d{2})[,.](\d{1,3})\s*-->\s*"
    r"(\d{1,2}):(\d{2}):(\d{2})[,.](\d{1,3})"
)


@dataclass(frozen=True, slots=True)
class Cue:
    """A piece of text shown on screen between two timestamps."""

    start_milliseconds: int
    end_milliseconds: int
    text: str


@dataclass(frozen=True, slots=True)
class MergeLimits:
    """When a merged cue closes and how far it may grow.

    A cue closes at sentence punctuation, at a pause of ``pause_milliseconds`` or more, or at a
    clause terminator once it has ``soft_characters``. Before a token would take it past
    ``hard_characters`` or ``maximum_milliseconds``, it is split; a split at a clause keeps at
    least ``minimum_split_characters`` in the head.
    """

    soft_characters: int
    hard_characters: int
    minimum_split_characters: int
    pause_milliseconds: int
    maximum_milliseconds: int


# Sentences that are translated afterwards; the Korean comes out about half as long.
TRANSLATION_LIMITS = MergeLimits(
    soft_characters=80,
    hard_characters=140,
    minimum_split_characters=40,
    pause_milliseconds=1200,
    maximum_milliseconds=10_000,
)
# Text shown as it is (Korean speech or Korean captions): about two lines of 20 characters.
DISPLAY_LIMITS = MergeLimits(
    soft_characters=20,
    hard_characters=42,
    minimum_split_characters=12,
    pause_milliseconds=700,
    maximum_milliseconds=7000,
)


def parse_json3(content: str) -> list[Cue]:
    """Parse a YouTube json3 caption document into word- or line-level cues.

    Automatic captions carry per-word offsets, so each word becomes a cue.
    Manual captions have no offsets, so each caption line becomes a cue.
    The first token of every event gets a leading space because event
    boundaries are always word boundaries.

    Raises:
        SubtitleParseError: The document is not valid json3.
    """
    try:
        events = json.loads(content)["events"]
        tokens = sorted(
            (token for event in events for token in _event_tokens(event)),
            key=lambda token: token[0],
        )
    except (json.JSONDecodeError, KeyError, TypeError, AttributeError) as error:
        raise SubtitleParseError("invalid json3 caption document") from error

    next_starts: list[int | None] = [token[0] for token in tokens[1:]]
    if tokens:
        next_starts.append(None)
    return [
        Cue(start, _token_end(start, event_end, next_start), text)
        for (start, event_end, text), next_start in zip(tokens, next_starts, strict=True)
    ]


def _event_tokens(event: dict[str, Any]) -> Iterator[tuple[int, int, str]]:
    event_start = int(event.get("tStartMs", 0))
    event_end = event_start + int(event.get("dDurationMs", 0))
    is_first = True
    for segment in event.get("segs") or []:
        text = segment.get("utf8", "").replace("\n", " ")
        if not text.strip():
            continue
        if is_first and not text[0].isspace():
            text = f" {text}"
        is_first = False
        yield event_start + int(segment.get("tOffsetMs", 0)), event_end, text


def _token_end(start: int, event_end: int, next_start: int | None) -> int:
    if next_start is None or next_start <= start:
        return max(event_end, start)
    return max(min(event_end, next_start), start)


def merge_into_sentences(
    tokens: Sequence[Cue], limits: MergeLimits = TRANSLATION_LIMITS
) -> list[Cue]:
    """Merge word- or line-level cues into sentence-sized cues.

    A cue closes at sentence punctuation, at a long pause, or after a clause
    once it is reasonably long. When a token would push a cue past the hard
    length limit or the maximum duration, the cue is split so that the sentence
    does not leave a one-word tail behind.
    """
    sentences: list[Cue] = []
    buffer: list[Cue] = []
    for token, next_token in zip(tokens, _followers(tokens), strict=True):
        while buffer and _would_overflow(buffer, token, limits):
            head, buffer = _split_for_limit(buffer, limits)
            sentences.append(_join(head))
        buffer.append(token)
        if _should_close(buffer, next_token, limits):
            sentences.append(_join(buffer))
            buffer = []
    return [sentence for sentence in sentences if sentence.text]


def _followers(cues: Sequence[Cue]) -> list[Cue | None]:
    """Return the cue following each cue, with ``None`` after the last one."""
    followers: list[Cue | None] = list(cues[1:])
    if cues:
        followers.append(None)
    return followers


def _would_overflow(buffer: list[Cue], token: Cue, limits: MergeLimits) -> bool:
    # Closing the whole cue at the duration limit left stubs such as "using agents."
    # behind, so a cue that would last too long is split the same way as one too long.
    duration = token.end_milliseconds - buffer[0].start_milliseconds
    return (
        len(_joined_text([*buffer, token])) > limits.hard_characters
        or duration > limits.maximum_milliseconds
    )


def _split_for_limit(buffer: list[Cue], limits: MergeLimits) -> tuple[list[Cue], list[Cue]]:
    """Split an over-long buffer into a finished head and a tail that keeps growing.

    Prefers the last clause terminator that leaves a sizeable head. Without one,
    cuts at the widest pause between tokens, so the end of a run-on sentence
    never becomes a stub.
    """
    for position in range(len(buffer) - 1, 0, -1):
        if not buffer[position - 1].text.strip().endswith(CLAUSE_TERMINATORS):
            continue
        if len(_joined_text(buffer[:position])) >= limits.minimum_split_characters:
            return buffer[:position], buffer[position:]
    position = _widest_gap_position(buffer, limits)
    return buffer[:position], buffer[position:]


def _widest_gap_position(buffer: list[Cue], limits: MergeLimits) -> int:
    """The token index after the widest pause, nearest the middle among equal pauses.

    Only cuts that leave both parts ``minimum_split_characters`` long are
    considered while there are any; a single-token buffer is returned whole.
    """
    positions = list(range(1, len(buffer)))
    if not positions:
        return len(buffer)
    heads = {position: len(_joined_text(buffer[:position])) for position in positions}
    tails = {position: len(_joined_text(buffer[position:])) for position in positions}
    balanced = [
        position
        for position in positions
        if min(heads[position], tails[position]) >= limits.minimum_split_characters
    ]

    def preference(position: int) -> tuple[int, int]:
        gap = buffer[position].start_milliseconds - buffer[position - 1].end_milliseconds
        return gap, -abs(heads[position] - tails[position])

    return max(balanced or positions, key=preference)


def _should_close(buffer: list[Cue], next_token: Cue | None, limits: MergeLimits) -> bool:
    if next_token is None:
        return True
    last_text = buffer[-1].text.strip()
    is_pause = (
        next_token.start_milliseconds - buffer[-1].end_milliseconds >= limits.pause_milliseconds
    )
    return (
        last_text.endswith(SENTENCE_TERMINATORS)
        or is_pause
        or (
            len(_joined_text(buffer)) >= limits.soft_characters
            and last_text.endswith(CLAUSE_TERMINATORS)
        )
    )


def _joined_text(buffer: list[Cue]) -> str:
    return _normalize("".join(token.text for token in buffer))


def _join(buffer: list[Cue]) -> Cue:
    return Cue(buffer[0].start_milliseconds, buffer[-1].end_milliseconds, _joined_text(buffer))


def _normalize(text: str) -> str:
    return _WHITESPACE.sub(" ", text).strip()


def format_srt(cues: Sequence[Cue]) -> str:
    """Serialize cues as SRT, fixing overlaps and too-short durations."""
    blocks = [
        f"{number}\n{_timestamp(cue.start_milliseconds)} --> "
        f"{_timestamp(cue.end_milliseconds)}\n{cue.text}\n"
        for number, cue in enumerate(_normalize_timings(cues), start=1)
    ]
    return "\n".join(blocks)


def _normalize_timings(cues: Sequence[Cue]) -> list[Cue]:
    visible = [cue for cue in cues if cue.text.strip()]
    normalized: list[Cue] = []
    for cue, next_cue in zip(visible, _followers(visible), strict=True):
        end = max(cue.end_milliseconds, cue.start_milliseconds + MINIMUM_CUE_MILLISECONDS)
        if next_cue is not None and next_cue.start_milliseconds > cue.start_milliseconds:
            end = min(end, next_cue.start_milliseconds)
        normalized.append(Cue(cue.start_milliseconds, end, cue.text.strip()))
    return normalized


def _timestamp(milliseconds: int) -> str:
    hours, remainder = divmod(max(milliseconds, 0), 3_600_000)
    minutes, remainder = divmod(remainder, 60_000)
    seconds, milliseconds = divmod(remainder, 1000)
    return f"{hours:02}:{minutes:02}:{seconds:02},{milliseconds:03}"


def write_srt(cues: Sequence[Cue], path: Path) -> Path:
    """Write cues to ``path`` as UTF-8 SRT and return the path."""
    path.write_text(format_srt(cues), encoding="utf-8")
    return path


def parse_srt(content: str) -> list[Cue]:
    """Parse SRT text into cues; the lines of a multi-line cue are joined with a space.

    Raises:
        SubtitleParseError: A block has no valid timing line.
    """
    cues: list[Cue] = []
    blocks = re.split(r"\n\s*\n", content.lstrip("\ufeff").replace("\r\n", "\n").strip())
    for block in blocks:
        lines = [line for line in block.split("\n") if line.strip()]
        if not lines:
            continue
        found = next(
            (
                (index, match)
                for index, line in enumerate(lines[:2])
                if (match := _SRT_TIMING.match(line)) is not None
            ),
            None,
        )
        if found is None:
            raise SubtitleParseError(f"invalid SRT block: {lines[0][:80]!r}")
        timing_index, match = found
        groups: tuple[str, ...] = match.groups()
        start = _milliseconds(groups[:4])
        end = _milliseconds(groups[4:])
        text = _normalize(" ".join(lines[timing_index + 1 :]))
        if text:
            cues.append(Cue(start, max(end, start), text))
    return cues


def _milliseconds(parts: tuple[str, ...]) -> int:
    """(hours, minutes, seconds, fraction) as matched; a fraction of 1-3 digits."""
    hours, minutes, seconds, fraction = parts
    total_seconds = (int(hours) * 60 + int(minutes)) * 60 + int(seconds)
    return total_seconds * 1000 + int(fraction.ljust(3, "0"))


def read_srt(path: Path) -> list[Cue]:
    """Read an SRT file as UTF-8 and parse it."""
    return parse_srt(path.read_text(encoding="utf-8"))
