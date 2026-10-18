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

A sentence closes at sentence punctuation, at a pause of 1.2 s or more, or at a clause
terminator once it is at least 80 characters long. When the next token would push it past
140 characters, it is split at the last clause terminator (keeping at least 40 characters in
the head) or, if there is none, near the middle, so the end of a run-on sentence never becomes
a one-word cue.

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
