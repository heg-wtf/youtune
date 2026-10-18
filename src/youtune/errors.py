"""Exception hierarchy for youtune."""


class YoutuneError(Exception):
    """Base error for every failure the CLI reports to the user."""


class DownloadError(YoutuneError):
    """Fetching metadata, captions or media from YouTube failed."""


class CaptionNotFoundError(YoutuneError):
    """No usable caption track exists for the video."""


class SubtitleParseError(YoutuneError):
    """A caption document could not be parsed."""


class TranslationError(YoutuneError):
    """Translating subtitles into Korean failed."""


class TranscriptionError(YoutuneError):
    """Speech-to-text transcription failed or is unavailable."""


class MuxError(YoutuneError):
    """Embedding subtitles into the video container failed."""
