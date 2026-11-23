# Plan: Shorts, burned subtitles, analysis and HTML report for youtune
- date: 2026-11-23
- status: done
- author: claude
- approved-by: ash84 (2026-11-23, as part of the hyper YouTube Shorts bot plan)

## 1. Purpose and background
- A hyper bot runs youtune on the Mac worker when a YouTube link is sent: it must add Korean
  subtitles or cut Shorts, and report back as an HTML page.
- The Shorts renderer from the 2026-10-22 shorts-manager session existed only in a session
  transcript (its scratchpad was gone); it is recovered here, generalised and tested.
- The full plan (hyper side, transport, bot) is `docs/plan-youtube-shorts-bot-2026-11-23.md`
  in `heg-wtf/hyper`.

## 2. Expected impact
- New subcommands `analyze`, `frames`, `shorts`, `burn`, `report`; `youtune <url>` gains
  `--burn` and `--json`. Existing usage unchanged.
- New dependency: Pillow. yt-dlp messages now go to stderr (stdout carries results only).

## 3. Implementation options
| Option | Pros | Cons |
| --- | --- | --- |
| A. New repository for the renderer | Separation | Two installs on the worker, duplicated ffmpeg / SRT helpers |
| B. **Subcommands in youtune** | One tool on the worker, reuses SRT and download code | youtune grows |

**Choice: B.**

Subtitle overlays: one `ffconcat` image track (chosen) instead of one input per cue, which
does not scale past a few dozen cues.

## 4. Implementation steps
- [x] Step 1: `media.py`, `fonts.py`, `typography.py`, `image_track.py`; `parse_srt`
- [x] Step 2: `burn.py` (VideoToolbox when present, libx264 otherwise)
- [x] Step 3: `shorts_spec.py` + `shorts.py` (square / full layouts, any frame rate, silent sources)
- [x] Step 4: `analyze.py` (sheets, scenes, loudness peaks, transcript, frames)
- [x] Step 5: `report.py`
- [x] Step 6: CLI (`commands.py`, `--burn`, `--json`), README, ARCHITECTURE

## 5. Test plan
**Unit tests:**
- [x] probe parsing, frame-rate halving, encoder listing, ffmpeg error tail
- [x] font discovery order, colour parsing, wrapping, fitting
- [x] concat list (gaps, overlaps, last-entry repeat), subtitle layout, encoder / audio arguments
- [x] spec validation (every error with its path, all at once), expressions, layouts, timeline
- [x] scene / loudness parsing, peak picking, transcript format
- [x] manifest validation (path escape, symlink escape, missing files), escaping, size shrinking
- [x] CLI dispatch, legacy URL form, `--json`, `--burn` without video

**Integration tests (real ffmpeg, synthetic clips):**
- [x] burn: text visible only during its cue
- [x] shorts: 1080×1920, output rate, exact duration with a freeze, srt and poster written
- [x] analyze: scene cut and loud burst found; frames past the end refused
- [x] A real 5-minute YouTube video: download → translate → analyze → short → report

## 6. Side effects
- yt-dlp progress moved from stdout to stderr: anyone parsing stdout now gets only results.
- No migration.

## 7. Security review
- Captions and titles are untrusted: the report escapes every field, has no scripts and
  loads nothing; manifest paths must resolve inside the manifest's folder (symlinks included).
- ffmpeg is run with argument lists (no shell); filter graphs are built from validated numbers.
- Authentication, sensitive data, PCI-DSS: not applicable.
