# youtune

YouTube 영상을 내려받고 한국어 자막(`.ko.srt`)을 만드는 CLI.

```
URL ─▶ yt-dlp 메타데이터 ─▶ 영상 다운로드(mp4)
                      └─▶ 원문 자막(YouTube json3, 없으면 Whisper) ─▶ 문장 단위 병합
                                                                   └─▶ Claude Code CLI 번역 ─▶ .ko.srt (─▶ mp4 임베드)
```

## 요구 사항

| 도구 | 용도 | 설치 |
| --- | --- | --- |
| Python 3.12+, [uv](https://docs.astral.sh/uv/) | 실행 환경 | `brew install uv` |
| `ffmpeg` | 영상/음성 병합, 자막 임베드 | `brew install ffmpeg` |
| `deno` | yt-dlp의 YouTube JS 챌린지 해석 | `brew install deno` |
| `claude` (Claude Code) | 한국어 번역 | 설치 후 `claude` 로그인 |

## 설치

```bash
make install                # uv sync
make install-whisper        # (선택) 자막 없는 영상용 Whisper 음성 인식
```

## 사용법

```bash
# 영상 + 한국어 자막
uv run youtune "https://www.youtube.com/watch?v=PaPpyQocMww" -o downloads

# 자막을 mp4 안에 소프트 자막 트랙으로 넣기
uv run youtune "<url>" -o downloads --embed

# 자막만 (영상 다운로드 생략)
uv run youtune "<url>" --no-video

# Makefile 래퍼
make run URL="<url>" ARGS="--embed"
```

### 출력

`<output-dir>/<제목> [<영상ID>]` 를 기준으로 다음 파일을 만든다.

| 파일 | 내용 |
| --- | --- |
| `….mp4` | 영상 (`--no-video`면 생략, `--embed`면 한국어 자막 트랙 포함) |
| `….<원문언어>.srt` | 문장 단위로 정리한 원문 자막 (원문이 한국어면 생략) |
| `….ko.srt` | 한국어 자막 |

### 옵션

| 옵션 | 기본값 | 설명 |
| --- | --- | --- |
| `-o, --output-dir` | `.` | 출력 디렉터리 |
| `--no-video` | off | 영상 다운로드 생략 |
| `--embed` | off | 한국어 자막을 영상에 임베드 (mp4/m4v/mov/mkv) |
| `--source-language` | 자동 감지 | 원문 언어 코드 (예: `en`) |
| `--transcribe` | off | YouTube 자막을 무시하고 Whisper로 인식 |
| `--whisper-model` | `small` | faster-whisper 모델 크기 |
| `--model` | `sonnet` | 번역에 쓸 Claude 모델 (`sonnet`, `opus`, `haiku`, 전체 모델명) |
| `--batch-size` | `40` | 번역 요청 1회당 문장 수 |
| `--workers` | `4` | 병렬 번역 요청 수 |
| `--format` | mp4 우선 | yt-dlp 포맷 셀렉터 |
| `-v, --verbose` | off | 디버그 로그 |

종료 코드: `0` 성공, `1` 실행 오류, `2` 인자 오류, `130` 사용자 중단.

## 동작 방식

1. **자막 선택** — 수동 한국어 자막 → (원문 언어) 수동 자막 → 자동 자막 `<lang>-orig` → 자동 자막 `<lang>` 순.
   YouTube 자동 번역 트랙(`tlang=ko`)은 429 레이트 리밋이 잦아 쓰지 않는다. 자막이 없으면 Whisper로 폴백.
2. **문장 병합** — 자동 자막의 단어 단위 타임스탬프를 구두점·휴지·길이 기준으로 문장 단위로 묶는다.
   줄 단위 조각을 그대로 번역하면 어순이 다른 한국어가 어색해지기 때문.
3. **번역** — `claude -p --json-schema` 헤드리스 호출. 배치마다 앞 문장 5개를 문맥으로 주고,
   응답 인덱스가 어긋나면 최대 3회 재시도한다.
4. **출력** — SRT 작성(겹침/최소 표시 시간 보정), `--embed`면 ffmpeg로 스트림 복사 임베드.

번역은 사용자의 Claude Code 구독 사용량을 쓴다. 55분 영상(약 600문장) 기준 호출 15회 내외.

## 개발

```bash
make lint               # ruff format + ruff check + mypy --strict
make test               # 단위 테스트
make test-integration   # 실제 YouTube + claude CLI 통합 테스트
```

구조는 [docs/ARCHITECTURE.md](docs/ARCHITECTURE.md) 참고.
