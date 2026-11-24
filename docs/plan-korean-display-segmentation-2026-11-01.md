# Plan: Display-sized cues for Korean speech and Korean captions
- date: 2026-11-01
- status: done
- author: claude
- approved-by: ash84 (2026-11-24, "이거 PR올려")

## 1. Purpose and background
- Transcribing a Korean iPhone video (11:37) with the Whisper fallback produced unusable subtitles:
  364 Whisper segments were merged into 43 cues averaging **70 characters / 16 seconds** (max 129 characters / 46 seconds).
- Cause
  - Korean Whisper output has almost no punctuation (1 of 364 segments ends with `.?!`) and the segments are contiguous
    (median gap 0 ms), so `merge_into_sentences` only splits at the 140-character hard limit.
  - The limits (soft 80 / hard 140 characters) are tuned for English source text that is translated afterwards
    (Korean translations are roughly half as long). Korean text shown as-is needs about 2 lines × 20 characters.
  - There is no duration cap, and segment-level Whisper timings span silences (one segment lasts 21 s).
- A one-off run with word timestamps and display limits (hard 42 / soft 20 characters, 700 ms pause) produced
  158 cues averaging 21 characters / 3.1 seconds (max 42 characters / 8.5 seconds), which reads well.

## 2. Expected impact
- Modules: `subtitles.py` (merge limits), `transcriber.py` (word timestamps), `cli.py` (limit selection per language).
- No CLI flag or output format change.
- Whisper transcription becomes slightly slower (word alignment): 161 s → 168 s for the 11-minute video.
- Videos whose source language is Korean (Whisper or YouTube Korean captions) get shorter, more frequent cues.
  The English → Korean translation path keeps today's limits.

## 3. Implementation options
| Option | Pros | Cons |
| --- | --- | --- |
| A. Use Whisper segments as cues without merging | Trivial | Choppy (8 characters / 1.9 s on average), segments can span 20 s of silence, does not help Korean YouTube captions |
| B. Word timestamps + language-aware merge limits + duration cap | Accurate timing, reuses the existing merger, one rule set for Whisper and captions | Slightly slower transcription, more parameters to test |
| C. Ask Claude to segment the Korean text | Semantically clean breaks | Extra calls and cost, timing must be realigned to words, new failure mode |

**Choice: B.** It matched the one-off result above and keeps segmentation deterministic and local.

## 4. Implementation steps
- [x] Step 1: `subtitles.py` — add a frozen `MergeLimits` (soft/hard characters, minimum split characters, pause, maximum duration) with
      `TRANSLATION_LIMITS` (today's values + 10 s cap) and `DISPLAY_LIMITS` (20 / 42 / 12 characters, 700 ms, 7 s);
      `merge_into_sentences(tokens, limits=TRANSLATION_LIMITS)`
- [x] Step 2: `subtitles.py` — close a cue when it reaches the maximum duration; when splitting an over-long cue without a clause
      boundary, split at the largest gap between tokens (nearest to the middle on ties) instead of the exact middle
      (changed during the run: a duration overflow is split like a length overflow, see Results)
- [x] Step 3: `transcriber.py` — `word_timestamps=True`, emit word-level cues; fall back to segment-level cues when a segment has no words
      (every segment's first cue starts with a space — see Results)
- [x] Step 4: `cli.py` — use `DISPLAY_LIMITS` when the source language is Korean, `TRANSLATION_LIMITS` otherwise (captions and Whisper)
- [x] Step 5: README / ARCHITECTURE — document both limit sets

## 5. Test plan
**Unit tests:**
- [x] Display limits: no cue exceeds 42 characters; short pauses (≥ 700 ms) close cues
- [x] Maximum duration closes a cue even without punctuation or pauses
- [x] Over-long cue without a clause splits at the largest gap
- [x] Default (translation) limits keep current behavior (existing tests stay green)
- [x] Transcriber: word-level cues from `segment.words`, segment-level fallback when `words` is empty
- [x] CLI: Korean source uses display limits, English source uses translation limits

**Integration tests:**
- [x] Scenario 1: Korean local video via Whisper → mean ≤ 30 characters, max ≤ 42 characters, max ≤ 9 s
- [x] Scenario 2: existing YouTube English → Korean run still yields ~700 cues with 0 untranslated lines

## 6. Side effects
- Existing English → Korean output: unchanged limits, plus a 10 s cap that only affects unusually slow speech.
- Korean YouTube captions: re-split into shorter cues (intended).
- Backward compatibility: `merge_into_sentences` keeps its signature with a defaulted `limits` argument.
- Migration: not needed.

## 7. Security review
- OWASP Top 10: no new input surface; no new external processes.
- Authentication/authorization: no change. Sensitive data: no change. PCI-DSS: not applicable.

## Results (2026-11-24)
- Scenario 1, IMG_2338 (11:37, Korean, Whisper `small`): 941 word tokens → 171 cues, mean 18.6 characters /
  2.5 s, max 42 characters / 6.8 s (was 43 cues, mean 70 characters / 16 s). Transcription took 97 s.
- Scenario 2, PaPpyQocMww (55:30, English automatic captions): 738 source cues (was 717), 729 Korean cues
  after the translator folded 9, 0 lines without Hangul. Max cue duration 16.4 s → 9.8 s; cues under 15 characters
  unchanged (31 → 31).
- Deviation from step 2: closing the whole cue at the duration limit cut 694 cues out of the same captions and
  left stubs such as "using agents." (13 characters) after a 139-character cue. A duration overflow now goes
  through the same split as a length overflow (clause, then the widest gap), which gave the numbers above.
- Found on the way: the Whisper path stripped segment texts and the merger joins texts as they are, so merged
  segments read "안녕하세요반갑습니다". Every segment's first cue now starts with a space (regression test added).

## Out of scope (separate proposal)
- Accepting local files (`youtune ./video.mov`) in the CLI. The IMG_2338 run used a one-off script on top of the modules.
