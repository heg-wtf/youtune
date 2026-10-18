import json
from pathlib import Path

import pytest

from youtune.errors import SubtitleParseError
from youtune.subtitles import (
    HARD_CHARACTER_LIMIT,
    Cue,
    format_srt,
    merge_into_sentences,
    parse_json3,
    write_srt,
)


def json3(*events: dict[str, object]) -> str:
    return json.dumps({"wireMagic": "pb3", "events": list(events)})


AUTOMATIC_CAPTIONS = json3(
    {"tStartMs": 0, "dDurationMs": 100_000, "id": 1, "wpWinPosId": 1},
    {
        "tStartMs": 100,
        "dDurationMs": 3000,
        "segs": [
            {"utf8": "Welcome"},
            {"utf8": " to", "tOffsetMs": 400},
            {"utf8": " the", "tOffsetMs": 600},
            {"utf8": " demo.", "tOffsetMs": 800},
            {"utf8": " Today", "tOffsetMs": 1500},
        ],
    },
    {"tStartMs": 2000, "dDurationMs": 1000, "aAppend": 1, "segs": [{"utf8": "\n"}]},
    {
        "tStartMs": 2010,
        "dDurationMs": 3000,
        "segs": [
            {"utf8": "we"},
            {"utf8": " build", "tOffsetMs": 300},
            {"utf8": " things,", "tOffsetMs": 700},
            {"utf8": " okay?", "tOffsetMs": 1200},
        ],
    },
    {"tStartMs": 9000, "dDurationMs": 2000, "segs": [{"utf8": "Thanks"}]},
)

MANUAL_CAPTIONS = json3(
    {"tStartMs": 0, "dDurationMs": 2000, "segs": [{"utf8": "First line\nwrapped"}]},
    {"tStartMs": 2000, "dDurationMs": 2000, "segs": [{"utf8": "continues here."}]},
    {"tStartMs": 6000, "dDurationMs": 1500, "segs": [{"utf8": "After a pause"}]},
    {"tStartMs": 9000, "dDurationMs": 1000, "segs": [{"utf8": "end"}]},
)


class TestParseJson3:
    def test_automatic_captions_get_word_level_timings(self) -> None:
        tokens = parse_json3(AUTOMATIC_CAPTIONS)

        assert [token.text.strip() for token in tokens] == [
            "Welcome", "to", "the", "demo.", "Today", "we", "build", "things,", "okay?", "Thanks",
        ]  # fmt: skip
        assert tokens[0] == Cue(100, 500, " Welcome")
        assert tokens[4] == Cue(1600, 2010, " Today")
        # Last word of an event ends at the event end when the next word is far away.
        assert tokens[8] == Cue(3210, 5010, " okay?")
        assert tokens[-1] == Cue(9000, 11_000, " Thanks")

    def test_event_boundaries_become_word_boundaries(self) -> None:
        tokens = parse_json3(AUTOMATIC_CAPTIONS)

        assert tokens[5].text == " we"

    def test_manual_captions_become_line_level_tokens(self) -> None:
        tokens = parse_json3(MANUAL_CAPTIONS)

        assert tokens[0] == Cue(0, 2000, " First line wrapped")
        assert tokens[2] == Cue(6000, 7500, " After a pause")

    def test_events_without_segments_are_ignored(self) -> None:
        assert parse_json3(json3({"tStartMs": 0, "dDurationMs": 10})) == []

    @pytest.mark.parametrize(
        "content",
        ["not json", '{"no_events": true}', '{"events": 5}', '{"events": [{"segs": [1]}]}'],
    )
    def test_invalid_document_raises(self, content: str) -> None:
        with pytest.raises(SubtitleParseError):
            parse_json3(content)


class TestMergeIntoSentences:
    def test_closes_on_sentence_punctuation(self) -> None:
        sentences = merge_into_sentences(parse_json3(AUTOMATIC_CAPTIONS))

        assert sentences == [
            Cue(100, 1600, "Welcome to the demo."),
            Cue(1600, 5010, "Today we build things, okay?"),
            Cue(9000, 11_000, "Thanks"),
        ]

    def test_closes_on_long_pause(self) -> None:
        sentences = merge_into_sentences(parse_json3(MANUAL_CAPTIONS))

        assert [sentence.text for sentence in sentences] == [
            "First line wrapped continues here.",
            "After a pause",
            "end",
        ]

    def test_respects_hard_character_limit(self) -> None:
        words = [Cue(index * 100, index * 100 + 100, " word") for index in range(100)]

        sentences = merge_into_sentences(words)

        assert len(sentences) > 1
        assert all(len(sentence.text) <= HARD_CHARACTER_LIMIT for sentence in sentences)
        assert " ".join(sentence.text for sentence in sentences).split() == ["word"] * 100

    def test_hard_limit_splits_at_last_clause(self) -> None:
        head = [Cue(index * 100, index * 100 + 100, " alpha") for index in range(8)]
        clause_end = Cue(800, 900, " beta,")
        # 53 characters up to "beta," and 15 more words push the cue past the hard limit.
        body = [Cue(900 + index * 100, 1000 + index * 100, " gamma") for index in range(15)]
        tokens = [*head, clause_end, *body, Cue(2400, 2500, " done.")]

        sentences = merge_into_sentences(tokens)

        assert [sentence.text.split()[-1] for sentence in sentences] == ["beta,", "done."]

    def test_hard_limit_without_clause_never_leaves_a_stub(self) -> None:
        words = [Cue(index * 100, index * 100 + 100, " word") for index in range(28)]
        tokens = [*words, Cue(2800, 2900, " end.")]

        sentences = merge_into_sentences(tokens)

        assert len(sentences) == 2
        assert all(len(sentence.text.split()) > 10 for sentence in sentences)
        assert sentences[-1].text.endswith("end.")

    def test_closes_on_clause_after_soft_limit(self) -> None:
        long_clause = [Cue(index * 100, index * 100 + 100, " lengthy") for index in range(11)]
        tokens = [*long_clause, Cue(1100, 1200, " end,"), Cue(1200, 1300, " next")]

        sentences = merge_into_sentences(tokens)

        assert sentences[0].text.endswith("end,")
        assert sentences[1].text == "next"

    def test_short_clause_does_not_close(self) -> None:
        tokens = [Cue(0, 100, " Hi,"), Cue(100, 200, " there.")]

        assert merge_into_sentences(tokens) == [Cue(0, 200, "Hi, there.")]

    def test_empty_input(self) -> None:
        assert merge_into_sentences([]) == []


class TestFormatSrt:
    def test_formats_numbered_blocks_with_hour_timestamps(self) -> None:
        cues = [Cue(0, 1500, "first line"), Cue(3_723_004, 3_725_000, "second line")]

        assert format_srt(cues) == (
            "1\n00:00:00,000 --> 00:00:01,500\nfirst line\n\n"
            "2\n01:02:03,004 --> 01:02:05,000\nsecond line\n"
        )

    def test_trims_overlap_with_next_cue(self) -> None:
        srt = format_srt([Cue(0, 5000, "a"), Cue(3000, 4000, "b")])

        assert "00:00:00,000 --> 00:00:03,000" in srt

    def test_extends_too_short_cue_without_overlapping(self) -> None:
        srt = format_srt([Cue(0, 10, "a"), Cue(300, 2000, "b"), Cue(5000, 5001, "c")])

        assert "00:00:00,000 --> 00:00:00,300" in srt
        assert "00:00:05,000 --> 00:00:05,500" in srt

    def test_skips_blank_cues_and_renumbers(self) -> None:
        srt = format_srt([Cue(0, 1000, "  "), Cue(1000, 2000, " text ")])

        assert srt == "1\n00:00:01,000 --> 00:00:02,000\ntext\n"

    def test_empty(self) -> None:
        assert format_srt([]) == ""


def test_write_srt_uses_utf8(tmp_path: Path) -> None:
    path = tmp_path / "video.ko.srt"

    korean_text = "\uc548\ub155\ud558\uc138\uc694"  # "hello" in Korean, multi-byte UTF-8

    returned = write_srt([Cue(0, 1000, korean_text)], path)

    assert returned == path
    assert korean_text in path.read_text(encoding="utf-8")
