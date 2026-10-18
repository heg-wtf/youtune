# youtune

A CLI that downloads a YouTube video and generates Korean subtitles (`.ko.srt`).

```
URL ─▶ yt-dlp metadata ─▶ video download (mp4)
                   └─▶ source captions (YouTube json3, Whisper if none) ─▶ sentence merging
                                                                        └─▶ Claude Code CLI translation ─▶ .ko.srt (─▶ embed into mp4)
```

## Requirements

| Tool | Purpose | Install |
| --- | --- | --- |
| Python 3.12+, [uv](https://docs.astral.sh/uv/) | Runtime | `brew install uv` |
| `ffmpeg` | Merging video/audio, embedding subtitles | `brew install ffmpeg` |
| `deno` | Solving YouTube JS challenges for yt-dlp | `brew install deno` |
| `claude` (Claude Code) | Korean translation | Install, then log in with `claude` |

## Installation

```bash
make install                         # uv sync (for development)
make install-whisper                 # (optional) Whisper speech recognition for videos without captions
uv tool install --editable .         # expose the `youtune` command globally
```

## Usage

```bash
# Video + Korean subtitles
youtune "https://www.youtube.com/watch?v=PaPpyQocMww" -o downloads

# Embed the subtitles into the mp4 as a soft subtitle track
youtune "<url>" -o downloads --embed

# Subtitles only (skip the video download)
youtune "<url>" --no-video

# Without a global install
uv run youtune "<url>"
make run URL="<url>" ARGS="--embed"
```

### Output

Files are written next to the base path `<output-dir>/<title> [<video id>]`.
The output directory is created if it does not exist.

| File | Content |
| --- | --- |
| `….mp4` | The video (skipped with `--no-video`; carries a Korean subtitle track with `--embed`) |
| `….<source language>.srt` | Source captions merged into sentences (skipped when the source is Korean) |
| `….ko.srt` | Korean subtitles |

### Options

| Option | Default | Description |
| --- | --- | --- |
| `-o, --output-dir` | `.` | Output directory |
| `--no-video` | off | Skip the video download |
| `--embed` | off | Embed the Korean subtitles into the video (mp4/m4v/mov/mkv) |
| `--source-language` | auto-detect | Source language code (e.g. `en`) |
| `--transcribe` | off | Ignore YouTube captions and use Whisper |
| `--whisper-model` | `small` | faster-whisper model size |
| `--model` | `sonnet` | Claude model for translation (`sonnet`, `opus`, `haiku`, or a full model name) |
| `--batch-size` | `40` | Lines per translation request |
| `--workers` | `4` | Parallel translation requests |
| `--format` | H.264 first | yt-dlp format selector |
| `-v, --verbose` | off | Debug logs |

Exit codes: `0` success, `1` runtime error, `2` invalid arguments, `130` interrupted.

## How it works

1. **Caption selection** — manual Korean → manual captions in the source language → automatic
   captions `<lang>-orig` → automatic captions `<lang>`. YouTube's machine-translated tracks
   (`tlang=ko`) are skipped because they are heavily rate-limited (HTTP 429). Falls back to
   Whisper when no captions exist.
2. **Sentence merging** — the word-level timestamps of automatic captions are regrouped into
   sentences by punctuation, pauses and length. Translating the raw line fragments produces
   awkward Korean because the word order differs. Over-long sentences are split at the last
   clause boundary, or near the middle, so no one-word tail is left behind.
3. **Translation** — headless `claude -p --json-schema` calls. Each batch gets the five
   preceding lines as context and is retried up to three times when the returned indexes do
   not match. A line the model folds into the previous one extends that cue's display time.
4. **Output** — SRT writing (overlap and minimum-duration fixes); with `--embed`, ffmpeg copies
   the streams and adds the subtitles as the default track.

Translation uses your Claude Code subscription quota. A 55-minute video (~700 sentences) takes
about 18 calls.

## Development

```bash
make lint               # ruff format + ruff check + mypy --strict
make test               # unit tests
make test-integration   # integration tests against real YouTube and the claude CLI
```

See [docs/ARCHITECTURE.md](docs/ARCHITECTURE.md) for the internal structure.
