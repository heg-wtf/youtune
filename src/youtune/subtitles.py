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
PAUSE_BREAK_MILLISECONDS = 1200
SOFT_CHARACTER_LIMIT = 80
HARD_CHARACTER_LIMIT = 140
MINIMUM_SPLIT_CHARACTERS = 40
MINIMUM_CUE_MILLISECONDS = 500

_WHITESPACE = re.compile(r"\s+")


@dataclass(frozen=True, slots=True)
class Cue:
    """A piece of text shown on screen between two timestamps."""

    start_milliseconds: int
    end_milliseconds: int
    text: str


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


def merge_into_sentences(tokens: Sequence[Cue]) -> list[Cue]:
    """Merge word- or line-level cues into sentence-sized cues.

    A cue closes at sentence punctuation, at a long pause, or after a clause
    once it is reasonably long. When a token would push a cue past the hard
    length limit, the cue is split so that the sentence does not leave a
    one-word tail behind.
    """
    sentences: list[Cue] = []
    buffer: list[Cue] = []
    for token, next_token in zip(tokens, _followers(tokens), strict=True):
        if buffer and len(_joined_text([*buffer, token])) > HARD_CHARACTER_LIMIT:
            head, buffer = _split_for_limit(buffer)
            sentences.append(_join(head))
        buffer.append(token)
        if _should_close(buffer, next_token):
            sentences.append(_join(buffer))
            buffer = []
    return [sentence for sentence in sentences if sentence.text]


def _followers(cues: Sequence[Cue]) -> list[Cue | None]:
    """Return the cue following each cue, with ``None`` after the last one."""
    followers: list[Cue | None] = list(cues[1:])
    if cues:
        followers.append(None)
    return followers


def _split_for_limit(buffer: list[Cue]) -> tuple[list[Cue], list[Cue]]:
    """Split an over-long buffer into a finished head and a tail that keeps growing.

    Prefers the last clause terminator that leaves a sizeable head. Without one,
    cuts near the middle so the end of a run-on sentence never becomes a stub.
    """
    for position in range(len(buffer) - 1, 0, -1):
        if not buffer[position - 1].text.strip().endswith(CLAUSE_TERMINATORS):
            continue
        if len(_joined_text(buffer[:position])) >= MINIMUM_SPLIT_CHARACTERS:
            return buffer[:position], buffer[position:]

    half = len(_joined_text(buffer)) / 2
    position = min(
        range(1, len(buffer)),
        key=lambda candidate: abs(len(_joined_text(buffer[:candidate])) - half),
        default=len(buffer),
    )
    return buffer[:position], buffer[position:]


def _should_close(buffer: list[Cue], next_token: Cue | None) -> bool:
    if next_token is None:
        return True
    last_text = buffer[-1].text.strip()
    is_pause = (
        next_token.start_milliseconds - buffer[-1].end_milliseconds >= PAUSE_BREAK_MILLISECONDS
    )
    return (
        last_text.endswith(SENTENCE_TERMINATORS)
        or is_pause
        or (
            len(_joined_text(buffer)) >= SOFT_CHARACTER_LIMIT
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
