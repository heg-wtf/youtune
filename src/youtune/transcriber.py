"""Optional speech-to-text fallback with faster-whisper for videos without captions."""

import logging
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from youtune.errors import TranscriptionError
from youtune.subtitles import Cue

logger = logging.getLogger(__name__)

INSTALL_HINT = "Whisper support is not installed; run `uv sync --extra whisper`"

type ModelFactory = Callable[[str], Any]


@dataclass(frozen=True, slots=True)
class Transcript:
    """Recognized speech with the detected (or forced) language."""

    cues: list[Cue]
    language: str


def _load_whisper_model(model_size: str) -> Any:
    try:
        from faster_whisper import WhisperModel
    except ImportError as error:
        raise TranscriptionError(INSTALL_HINT) from error
    return WhisperModel(model_size, device="auto", compute_type="int8")


class WhisperTranscriber:
    """Transcribe media files into cues with a lazily loaded Whisper model."""

    def __init__(
        self, model_size: str = "small", *, model_factory: ModelFactory = _load_whisper_model
    ) -> None:
        self.model_size = model_size
        self._model_factory = model_factory

    def transcribe(self, media_path: Path, *, language: str | None = None) -> Transcript:
        """Transcribe ``media_path``; ``language`` skips auto-detection when given.

        Raises:
            TranscriptionError: Whisper is unavailable or decoding failed.
        """
        model = self._model_factory(self.model_size)
        logger.info("Transcribing %s with Whisper (%s)", media_path.name, self.model_size)
        try:
            segments, information = model.transcribe(
                str(media_path), language=language, vad_filter=True
            )
            cues = [
                Cue(round(segment.start * 1000), round(segment.end * 1000), segment.text.strip())
                for segment in segments
                if segment.text.strip()
            ]
        except (RuntimeError, ValueError, OSError) as error:
            raise TranscriptionError(f"Whisper failed on {media_path.name}: {error}") from error
        return Transcript(cues, information.language)
