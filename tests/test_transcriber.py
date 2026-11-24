import sys
import wave
from collections.abc import Iterator
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from youtune.errors import TranscriptionError
from youtune.subtitles import DISPLAY_LIMITS, Cue, merge_into_sentences
from youtune.transcriber import WhisperTranscriber, _load_whisper_model, segment_cues


class FakeModel:
    def __init__(self, segments: list[SimpleNamespace], *, error: Exception | None = None) -> None:
        self.segments = segments
        self.error = error
        self.calls: list[tuple[str, dict[str, Any]]] = []

    def transcribe(
        self, audio: str, **options: Any
    ) -> tuple[Iterator[SimpleNamespace], SimpleNamespace]:
        self.calls.append((audio, options))
        return self._generate(), SimpleNamespace(language="en")

    def _generate(self) -> Iterator[SimpleNamespace]:
        # faster-whisper decodes lazily, so errors surface while iterating.
        if self.error:
            raise self.error
        yield from self.segments


def segment(
    start: float, end: float, text: str, words: list[SimpleNamespace] | None = None
) -> SimpleNamespace:
    return SimpleNamespace(start=start, end=end, text=text, words=words)


def word(start: float, end: float, text: str) -> SimpleNamespace:
    return SimpleNamespace(start=start, end=end, word=text, probability=0.9)


def test_transcribe_converts_segments_to_cues() -> None:
    model = FakeModel(
        [segment(0.0, 1.25, " Hello."), segment(1.5, 2.0, "   "), segment(2.0, 3.0, " Bye.")]
    )
    requested_sizes: list[str] = []

    def factory(size: str) -> FakeModel:
        requested_sizes.append(size)
        return model

    transcript = WhisperTranscriber("tiny", model_factory=factory).transcribe(
        Path("audio.m4a"), language="en"
    )

    assert transcript.cues == [Cue(0, 1250, " Hello."), Cue(2000, 3000, " Bye.")]
    assert transcript.language == "en"
    assert requested_sizes == ["tiny"]
    assert model.calls == [
        ("audio.m4a", {"language": "en", "vad_filter": True, "word_timestamps": True})
    ]


def test_aligned_words_become_the_cues() -> None:
    model = FakeModel(
        [
            # A segment's own timing spans the pause between its words.
            segment(
                0.0, 9.0, " 오늘은 날씨가", [word(0.0, 0.6, " 오늘은"), word(8.2, 9.0, " 날씨가")]
            ),
            segment(9.0, 10.0, "좋네요", [word(9.1, 9.8, "좋네요"), word(9.8, 9.9, " ")]),
            segment(10.0, 11.0, " 끝", []),
        ]
    )

    transcript = WhisperTranscriber(model_factory=lambda size: model).transcribe(Path("a.m4a"))

    assert transcript.cues == [
        Cue(0, 600, " 오늘은"),
        Cue(8200, 9000, " 날씨가"),
        Cue(9100, 9800, " 좋네요"),
        Cue(10_000, 11_000, " 끝"),
    ]


def test_decoding_failure_raises_transcription_error() -> None:
    model = FakeModel([], error=RuntimeError("bad audio"))

    with pytest.raises(TranscriptionError, match="bad audio"):
        WhisperTranscriber(model_factory=lambda size: model).transcribe(Path("a.m4a"))


def test_missing_faster_whisper_has_install_hint(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setitem(sys.modules, "faster_whisper", None)

    with pytest.raises(TranscriptionError, match="uv sync --extra whisper"):
        _load_whisper_model("tiny")


def test_installed_faster_whisper_can_decode_audio(tmp_path: Path) -> None:
    """Regression: PyAV 19 dropped av.open(metadata_errors=...), which faster-whisper passes."""
    audio = pytest.importorskip("faster_whisper.audio")
    sample_rate = 16_000
    wav_path = tmp_path / "silence.wav"
    with wave.open(str(wav_path), "wb") as wav_file:
        wav_file.setnchannels(1)
        wav_file.setsampwidth(2)
        wav_file.setframerate(sample_rate)
        wav_file.writeframes(b"\x00\x00" * (sample_rate // 2))

    samples = audio.decode_audio(str(wav_path), sampling_rate=sample_rate)

    assert len(samples) == pytest.approx(sample_rate // 2, abs=sample_rate // 100)


def test_merged_segments_keep_the_space_between_them() -> None:
    """Regression: stripped segment texts were merged as "안녕하세요반갑습니다"."""
    cues = [
        cue
        for item in (segment(0.0, 1.0, " 안녕하세요"), segment(1.0, 2.0, "반갑습니다"))
        for cue in segment_cues(item)
    ]

    assert merge_into_sentences(cues, DISPLAY_LIMITS) == [Cue(0, 2000, "안녕하세요 반갑습니다")]
