# Architecture

## Modules

| Module | Responsibility | External dependency |
| --- | --- | --- |
| `cli.py` | Argument parsing, pipeline orchestration (`run`), errors → exit codes | — |
| `downloader.py` | Metadata, caption track selection (`select_caption_track`), caption/video/audio downloads | yt-dlp |
| `subtitles.py` | `Cue` model, json3 parsing, sentence merging, SRT serialization | — |
| `translator.py` | Batched `claude -p` translation (parallel, context, schema validation, retries) | Claude Code CLI |
| `transcriber.py` | faster-whisper speech recognition fallback (optional extra) | faster-whisper |
| `muxer.py` | Embeds an SRT file as a soft subtitle track with ffmpeg | ffmpeg |
| `commands.py` | The `analyze` / `frames` / `shorts` / `burn` / `report` subcommands | — |
| `media.py` | ffprobe (`MediaInformation`), ffmpeg runner, output frame rate, encoders | ffmpeg |
| `fonts.py`, `typography.py` | Korean font discovery; outlined text layers, wrapping, fitting | Pillow |
| `image_track.py` | Timed still images as one `ffconcat` input (subtitle tracks) | ffmpeg |
| `burn.py` | Hard subtitles: one PNG per cue in an image track, one `overlay` | ffmpeg, Pillow |
| `shorts_spec.py` | The Shorts spec: dataclasses and validation with JSON paths | — |
| `shorts.py` | 1080×1920 renderer: crop/pan, zoompan, freeze, concat, loudnorm, overlays | ffmpeg, Pillow |
| `analyze.py` | Contact sheets, scene cuts, loudness peaks, transcript; exact frames | ffmpeg, Pillow |
| `report.py` | Manifest validation and the self-contained HTML report | Pillow |
| `errors.py` | Domain exceptions under `YoutuneError` | — |

## Pipeline

```
cli.run
 ├─ downloader.fetch_information(url) ─────────────▶ VideoInformation
 ├─ downloader.download_video(video)   (optional)  ─▶ <title> [<id>].mp4
 ├─ source subtitles
 │   ├─ select_caption_track ─▶ download_caption ─▶ parse_json3 ─▶ merge_into_sentences
 │   └─ (no captions / --transcribe) transcriber.transcribe ─▶ merge_into_sentences
 ├─ source ≠ Korean → write_srt(<lang>.srt) → translator.translate
 ├─ write_srt(ko.srt)
 └─ --embed → muxer.embed_subtitles
```

`cli.run` depends only on the `Downloader` / `Translator` / `Transcriber` protocols, so tests
inject fake implementations.

## Data structures

- `Cue(start_milliseconds, end_milliseconds, text)` — immutable value object used for words,
  lines and sentences alike.
- `VideoInformation` — the fields needed from the yt-dlp info dict (ID, title, language,
  manual/automatic caption tables) plus the original dict (`raw`).
- `CaptionTrack(language, is_automatic, url)` — the selected json3 caption track.

### YouTube json3

```json
{"events": [
  {"tStartMs": 160, "dDurationMs": 5920, "segs": [{"utf8": "Welcome"}, {"utf8": " to", "tOffsetMs": 1039}]},
  {"tStartMs": 2310, "dDurationMs": 3770, "aAppend": 1, "segs": [{"utf8": "\n"}]}
]}
```

- Automatic captions: every word has a `tOffsetMs` → word-level tokens.
  A word ends at min(next word start, event end).
- Manual captions: no offsets → event (line) level tokens.
- Whitespace-only segments (the `aAppend` newline events) are dropped.

### Sentence merging

`merge_into_sentences(tokens, limits)` takes one of two `MergeLimits` sets, chosen by the
source language (`cli.merge_limits`):

| | `TRANSLATION_LIMITS` (source is translated) | `DISPLAY_LIMITS` (Korean source, shown as is) |
| --- | --- | --- |
| Close at a clause once | 80 characters | 20 characters |
| Close at a pause of | 1.2 s | 0.7 s |
| Split before passing | 140 characters | 42 characters (about two lines) |
| Keep in a clause-split head | 40 characters | 12 characters |
| Split before lasting longer than | 10 s | 7 s |

A cue also closes at sentence punctuation. When the next token would push it past either
split limit, it is split at the last clause terminator that leaves a sizeable head or,
if there is none, at the widest pause between its tokens (nearest the middle among equal
pauses; while some cut leaves both parts at least the clause-split minimum long, only those
cuts count), so the end of a run-on sentence never becomes a one-word cue. A single token longer than the
duration limit (a Whisper segment without word timings) stays one cue. A duration overflow
is split rather than closed where it stands: closing there left stubs such as
"using agents." behind.

The translation limits suit English: Korean translations come out about half as long.
Korean shown as it is needs display-sized cues, and Korean speech gives the merger little to
work with — Whisper output has almost no punctuation and its segments are contiguous — so
the display set relies on word timings, short pauses and the 42-character split.

### Whisper word timings

`WhisperTranscriber` asks faster-whisper for word timestamps (`word_timestamps=True`) and
returns one cue per word; a segment without aligned words becomes one cue. A segment's own
timing spans the silences inside it (one lasted 21 s), word timings do not. The first cue of
every segment starts with a space, because a segment boundary is a word boundary and the
merger joins cue texts as they are.

## Translation session isolation

`claude --print --tools "" --strict-mcp-config --disable-slash-commands --no-session-persistence --setting-sources "" --system-prompt … --json-schema …`

- Tools, MCP servers, hooks, settings and slash commands are all disabled, so prompt injection
  inside captions can affect nothing but the translation itself.
- The prompt is sent through stdin (not visible in the process list), and a temporary directory
  is used as the working directory so no project `CLAUDE.md` is picked up.
- A response is accepted only when `structured_output.translations[].index` exactly matches the
  requested indexes.
- An empty or punctuation-only translation means the model folded that line into the previous
  one; the previous cue is extended over its time range.

## Rendering

The Homebrew ffmpeg has neither `drawtext` nor libass, so every piece of text is a PNG drawn
by Pillow and composited with `overlay`.

- **Subtitle tracks** (`image_track.py`): hundreds of cues as separate inputs do not scale, so
  each cue is a same-size PNG listed in one `ffconcat` file with transparent blanks between
  them (the last entry repeated, because the demuxer ignores the final duration). One input,
  one `overlay`, frame-exact.
- **Shorts** (`shorts.py`), per segment: `fps` to the output rate (the source's, halved until
  ≤ 30) → `trim=end_frame` → `split` into a blurred background (square layout) and a
  foreground `crop` that pans with the `center` keyframes → `tpad` clone for a freeze →
  `zoompan` only when the segment zooms or shakes (a shake forces a small zoom so it has room)
  → `settb`/`setpts=N` on every picture layer. Audio: `atrim=end_sample` + `apad` to the
  segment's exact length. Segments are concatenated, `loudnorm` to −14 LUFS, then title,
  labels, the subtitle track, pop sequences and flash sequences are overlaid. Pop and flash
  sequences start after `tpad=start=<frames>`.
- Three timing rules found while making Shorts by hand keep overlays on the right frame:
  `trim=end_frame` instead of a duration, one frame grid on both picture layers, and pop /
  flash offsets in whole frames.
