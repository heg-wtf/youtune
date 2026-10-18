# Plan: YouTube download + Korean subtitle CLI (youtune)
- date: 2026-10-18
- status: done
- author: claude
- approved-by: (not pre-approved — implemented right away on the user's request; the translation engine, Claude Code CLI, was chosen by the user)

## 1. Purpose and background
- We need a CLI that takes a YouTube URL, downloads the video and produces Korean subtitles (`.ko.srt`).
- Example target: `https://www.youtube.com/watch?v=PaPpyQocMww` (English, 55 minutes, no manual captions, automatic captions only).
- Confirmed constraints
  - YouTube's machine-translated Korean track (`tlang=ko`) fails repeatedly with `HTTP 429 Too Many Requests` → unreliable.
  - Automatic captions in the original language (`en-orig`, json3) are available with word-level timestamps and punctuation.
  - `ANTHROPIC_API_KEY` is not set. The user chose the **Claude Code CLI (`claude -p`)** as the translation engine.

## 2. Expected impact
- New project (empty directory), so no existing service, module or API is affected.
- External dependencies: YouTube (yt-dlp), the `claude` CLI (consumes the user's subscription quota), `ffmpeg` (merging, subtitle embedding).
- Performance: about 15–20 translation calls for a 55-minute video (40 sentences per batch), 4 in parallel → expected to finish within a few minutes.

## 3. Implementation options

### 3-1. Obtaining source captions
| Option | Pros | Cons |
| --- | --- | --- |
| A. YouTube json3 captions (manual > automatic `-orig`) | Fast, free, word timestamps | Impossible for videos without captions |
| B. Local Whisper speech recognition | Works without captions | Slow, requires a model download |

**Choice: A first, falling back to B when no captions exist.** B ships `faster-whisper` as an optional extra (`youtune[whisper]`) to keep the default install light.

### 3-2. Korean translation
| Option | Pros | Cons |
| --- | --- | --- |
| A. YouTube machine-translated track | Free | Practically unusable due to 429s, low quality |
| B. Claude API (SDK) | Best quality, stable | Requires an API key |
| C. Headless Claude Code CLI (`claude -p --json-schema`) | No API key (uses the subscription), best quality, structured output | Process start-up overhead, depends on the CLI version |

**Choice: C (user's choice).** `--tools ""`, `--strict-mcp-config`, `--setting-sources ""` and `--system-prompt` exclude tools, MCP, hooks and CLAUDE.md so the session only translates. `--json-schema` enforces a `{translations: [{index, text}]}` response, and index mismatches are retried.

### 3-3. Subtitle granularity
- Automatic caption events break in the middle of sentences → translating them as-is reads awkwardly in Korean, whose word order differs.
- **Choice: regroup word timestamps into sentences (punctuation / pauses / maximum length), then translate 1:1.**

## 4. Implementation steps
- [x] Step 1: Project scaffolding — `pyproject.toml` (uv, src layout), `Makefile`, `.gitignore`
- [x] Step 2: `subtitles.py` — `Cue` model, json3 parsing, sentence merging, SRT serialization
- [x] Step 3: `downloader.py` — yt-dlp metadata, caption track selection/download, video/audio download
- [x] Step 4: `translator.py` — batched Claude Code CLI translation (parallel, context, validation, retries)
- [x] Step 5: `transcriber.py` — faster-whisper fallback (optional)
- [x] Step 6: `muxer.py` — embed a soft subtitle track into the mp4 with ffmpeg (`--embed`)
- [x] Step 7: `cli.py` — argument parsing, pipeline orchestration, error handling / exit codes
- [x] Step 8: README.md, `docs/ARCHITECTURE.md`
- [x] Step 9: Verify with a real run on the target video

## 5. Test plan
**Unit tests:**
- [x] json3 parsing: automatic captions (word offsets, dropping `aAppend` newline events), manual captions (no offsets)
- [x] Sentence merging: sentence punctuation, long pauses, maximum length, empty input
- [x] SRT serialization: timestamp format (including hours), overlap fixes, numbering
- [x] Caption track selection: manual Korean first, manual source > automatic `-orig` > automatic source, inferring `-orig` when the language is unknown, no tracks
- [x] Translator: batching/context, response mapping, retry on index mismatch, error after retries on CLI failure / invalid JSON / `is_error`, missing `claude`
- [x] Embedding: ffmpeg command, original kept on failure
- [x] CLI: argument parsing, no translation when Korean captions exist, missing captions + missing Whisper error message, `--no-video` path

**Integration tests:** (run only with `YOUTUNE_INTEGRATION=1`)
- [x] Scenario 1: translate a few lines with the real `claude` CLI → Korean output, matching indexes
- [x] Scenario 2: real YouTube metadata/caption download → non-empty sentence merge result
- [x] Scenario 3: full run on the target video (video download + `.ko.srt` + `--embed`), verified manually

## 6. Side effects
- Impact on existing features: none (new project).
- Backward compatibility: not applicable.
- Migration: not applicable.
- `claude -p` calls consume the user's Claude subscription quota → handled (documented in the README, model selectable with `--model`).

## 7. Security review
- OWASP A03 (Injection): every external process (`claude`, `ffmpeg`) runs with an argument list, never `shell=True`. The prompt goes through stdin (not exposed in the process list).
- Prompt injection: captions and titles are untrusted input → the translation session has tools, MCP and settings disabled (`--tools ""` etc.), limiting the impact to translation quality. Output is validated by schema and indexes.
- Paths: file names come only from yt-dlp's sanitized output; nothing is written outside the output directory.
- Authentication/authorization changes: none. Sensitive data: none (no API key used). PCI-DSS: not applicable.

## 8. Changes during implementation (vs. the plan)
The first full run (616 cues) left 11 Korean cues as `.` or untranslated English.

- Cause: long sentences without commas were cut at the 140-character hard limit, leaving their last 1–3 words as a separate cue. The model folded that meaning into the previous line and returned `.` or the source text for the tail cue.
- Fixes (all within the planned files)
  - `subtitles.py`: when the hard limit is exceeded, split at the last clause (comma) boundary, or near the middle if there is none → zero tail fragments.
  - `translator.py`: the prompt now says "return an empty string if the line was folded into the previous one; never copy the source". Translations without words extend the previous cue's display time.
  - `downloader.py`: the default format now prefers H.264 (avc1). The first run fetched VP9 in mp4, which QuickTime cannot play.

## 9. Verification results
- `make lint` (ruff + mypy --strict) passes, `make test` 86 passed, `make test-integration` 2 passed.
- Target video run (`--embed`): 717 source cues → 707 Korean cues (10 folded into the previous line), 0 cues without Hangul, 0 copied source lines, 0 retries, timings monotonic and non-overlapping.
- mp4: h264 + aac + mov_text (`language=kor`, default) confirmed.
