# youtune

A CLI that downloads a YouTube video and generates Korean subtitles (`.ko.srt`), then
burns them in or cuts the video into 1080×1920 Shorts and writes an HTML report.

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

# Also write <title> [<id>].ko.mp4 with the subtitles burned in (every player shows them)
youtune "<url>" -o downloads --burn

# Subtitles only (skip the video download)
youtune "<url>" --no-video

# Machine-readable result (paths, title, channel, duration, caption source) on stdout
youtune "<url>" -o downloads --json

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

## Working on the downloaded video

Five subcommands take the files `youtune <url>` wrote. Each prints JSON on stdout.

```bash
# Measure: contact sheets (timestamped thumbnails), scene cuts, loudness peaks, transcript
youtune analyze video.mp4 --subtitles video.ko.srt -o analysis/

# Look closer: exact frames at given seconds
youtune frames video.mp4 --at 443.4 445.0 -o frames/ --width 720

# Cut Shorts from a JSON spec (see below), with the Korean subtitles burned in
youtune shorts video.mp4 --spec spec.json --subtitles video.ko.srt -o shorts/

# Burn any .srt into a video
youtune burn video.mp4 video.ko.srt -o video.burned.mp4

# A self-contained HTML report from a manifest written next to the outputs
youtune report shorts/manifest.json -o shorts/report.html
```

### Shorts spec

Times are **source seconds**. Each short is 5–180 s; `layout` is `square` (a square crop
over a blurred copy, the default for landscape sources) or `full` (a 9:16 crop that fills
the screen, the default for portrait sources).

```json
{"shorts": [{
  "name": "sar-pull",
  "title": [{"text": "포켓몬 카드 개봉하다가", "color": "white"},
            {"text": "인생 첫 SAR 등장!!", "color": "yellow"}],
  "highlight": ["SAR"],
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

| Field | Meaning |
| --- | --- |
| `center` | `[seconds, x]` keyframes: where the subject is horizontally (0–1); the crop follows it |
| `zoom`, `focus` | `[seconds, factor]` keyframes (1–3) and the point (x, y in 0–1) the zoom moves toward |
| `shakes`, `flashes` | Camera shake windows and white flashes |
| `freeze` | Seconds the last frame holds while the audio plays on |
| `pops`, `labels` | Big tilted punch-in text, and a small boxed caption at the top-left |
| `highlight` | Words that turn a subtitle cue yellow |
| `poster_at` | Output second for the poster frame (default: just after the first pop) |

Every problem is reported at once with its JSON path, e.g.
`shorts[0].segments[1].pops[0]: must lie inside the segment (…)`.

Text is drawn with Pillow because the Homebrew ffmpeg has no `drawtext`/libass. The font
is BM Dohyeon when present, then Apple SD Gothic Neo, Noto Sans CJK or Nanum Gothic; set
`YOUTUNE_FONT=/path/font.otf` (or `path#index` for a collection) to choose one.

### Report manifest

`mode` (`shorts` or `subtitles`), `source` (`url`, `title`, `channel`, `duration_seconds`,
`own_video`), `summary`, `outputs` (each `kind` = `short` / `subtitled_video` /
`subtitles`, `file`, optional `poster`, `title`, `duration_seconds`, `source_ranges`,
`reason`, `upload` with `title` / `description` / `hashtags`), `subtitle_file`, `notes`.
Paths are relative to the manifest and must stay inside its folder. The page has no
scripts and no network: posters are embedded, every field is escaped, and it is kept under
5 MiB by shrinking posters and the transcript.

## How it works

1. **Caption selection** — manual Korean → manual captions in the source language → automatic
   captions `<lang>-orig` → automatic captions `<lang>`. YouTube's machine-translated tracks
   (`tlang=ko`) are skipped because they are heavily rate-limited (HTTP 429). Falls back to
   Whisper when no captions exist.
2. **Sentence merging** — the word-level timestamps of automatic captions (and of Whisper
   speech) are regrouped into sentences by punctuation, pauses, length and duration.
   Translating the raw line fragments produces awkward Korean because the word order differs.
   Over-long sentences are split at the last clause boundary, or at the widest pause, so no
   one-word tail is left behind. Text that is translated keeps sentences up to 140 characters
   and 10 s; Korean captions and Korean speech, shown as they are, are cut to display size
   (up to 42 characters and 7 s, closing at 0.7 s pauses).
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
