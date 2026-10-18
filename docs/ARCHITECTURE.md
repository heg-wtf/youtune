# Architecture

## 모듈

| 모듈 | 책임 | 외부 의존 |
| --- | --- | --- |
| `cli.py` | 인자 파싱, 파이프라인 오케스트레이션(`run`), 에러 → 종료 코드 | — |
| `downloader.py` | 메타데이터 조회, 자막 트랙 선택(`select_caption_track`), 자막/영상/음성 다운로드 | yt-dlp |
| `subtitles.py` | `Cue` 모델, json3 파싱, 문장 병합, SRT 직렬화 | — |
| `translator.py` | `claude -p` 배치 번역(병렬, 문맥, 스키마 검증, 재시도) | Claude Code CLI |
| `transcriber.py` | faster-whisper 음성 인식 폴백 (optional extra) | faster-whisper |
| `muxer.py` | ffmpeg로 SRT를 소프트 자막 트랙으로 임베드 | ffmpeg |
| `errors.py` | `YoutuneError` 하위 도메인 예외 | — |

## 파이프라인

```
cli.run
 ├─ downloader.fetch_information(url) ─────────────▶ VideoInformation
 ├─ downloader.download_video(video)   (옵션)      ─▶ <title> [<id>].mp4
 ├─ 원문 확보
 │   ├─ select_caption_track ─▶ download_caption ─▶ parse_json3 ─▶ merge_into_sentences
 │   └─ (자막 없음 / --transcribe) transcriber.transcribe ─▶ merge_into_sentences
 ├─ 원문 ≠ 한국어 → write_srt(<lang>.srt) → translator.translate
 ├─ write_srt(ko.srt)
 └─ --embed → muxer.embed_subtitles
```

`cli.run`은 `Downloader`/`Translator`/`Transcriber` 프로토콜에만 의존하므로 테스트에서 가짜 구현을 주입한다.

## 데이터 구조

- `Cue(start_milliseconds, end_milliseconds, text)` — 불변 값 객체. 단어·줄·문장 단위 모두 같은 타입.
- `VideoInformation` — yt-dlp info dict에서 필요한 필드(ID, 제목, 언어, 수동/자동 자막 표)와 원본(`raw`).
- `CaptionTrack(language, is_automatic, url)` — 선택된 json3 자막 트랙.

### YouTube json3

```json
{"events": [
  {"tStartMs": 160, "dDurationMs": 5920, "segs": [{"utf8": "Welcome"}, {"utf8": " to", "tOffsetMs": 1039}]},
  {"tStartMs": 2310, "dDurationMs": 3770, "aAppend": 1, "segs": [{"utf8": "\n"}]}
]}
```

- 자동 자막: 단어마다 `tOffsetMs` → 단어 단위 토큰. 단어 종료 시각 = min(다음 단어 시작, 이벤트 종료).
- 수동 자막: 오프셋 없음 → 이벤트(줄) 단위 토큰.
- 공백뿐인 세그먼트(`aAppend` 개행 이벤트)는 버린다.

## 번역 세션 격리

`claude --print --tools "" --strict-mcp-config --disable-slash-commands --no-session-persistence --setting-sources "" --system-prompt … --json-schema …`

- 도구·MCP·훅·설정·슬래시 커맨드를 모두 끈다 → 자막 속 프롬프트 인젝션이 번역 품질 외에 영향을 줄 수 없다.
- 프롬프트는 stdin으로 전달(프로세스 목록 노출 방지), 임시 디렉터리를 cwd로 써 프로젝트 `CLAUDE.md`를 읽지 않는다.
- `structured_output.translations[].index`가 요청 인덱스와 정확히 일치해야 채택한다.
