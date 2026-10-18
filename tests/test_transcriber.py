import sys
from collections.abc import Iterator
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from youtune.errors import TranscriptionError
from youtune.subtitles import Cue
from youtune.transcriber import WhisperTranscriber, _load_whisper_model


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


def segment(start: float, end: float, text: str) -> SimpleNamespace:
    return SimpleNamespace(start=start, end=end, text=text)


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

    assert transcript.cues == [Cue(0, 1250, "Hello."), Cue(2000, 3000, "Bye.")]
    assert transcript.language == "en"
    assert requested_sizes == ["tiny"]
    assert model.calls == [("audio.m4a", {"language": "en", "vad_filter": True})]


def test_decoding_failure_raises_transcription_error() -> None:
    model = FakeModel([], error=RuntimeError("bad audio"))

    with pytest.raises(TranscriptionError, match="bad audio"):
        WhisperTranscriber(model_factory=lambda size: model).transcribe(Path("a.m4a"))


def test_missing_faster_whisper_has_install_hint(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setitem(sys.modules, "faster_whisper", None)

    with pytest.raises(TranscriptionError, match="uv sync --extra whisper"):
        _load_whisper_model("tiny")
