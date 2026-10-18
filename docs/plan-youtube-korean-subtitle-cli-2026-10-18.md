# Plan: YouTube 다운로드 + 한국어 자막 생성 CLI (youtune)
- date: 2026-10-18
- status: done
- author: claude
- approved-by: (미승인 — 사용자 요청 "만들어"에 따라 즉시 구현, 번역 엔진은 사용자 선택: Claude Code CLI)

## 1. 목적 및 배경
- YouTube URL 하나로 영상을 내려받고, 한국어 자막(`.ko.srt`)을 만드는 CLI가 필요하다.
- 대상 예시: `https://www.youtube.com/watch?v=PaPpyQocMww` (영어, 55분, 수동 자막 없음, 자동 자막만 존재).
- 확인된 제약
  - YouTube 자동번역 한국어 트랙(`tlang=ko`)은 `HTTP 429 Too Many Requests`로 반복 실패 → 신뢰 불가.
  - 원본 언어 자동 자막(`en-orig`, json3)은 단어 단위 타임스탬프와 구두점을 포함해 받을 수 있다.
  - `ANTHROPIC_API_KEY` 미설정. 사용자가 번역 엔진으로 **Claude Code CLI(`claude -p`)** 를 선택했다.

## 2. 예상 임팩트
- 신규 프로젝트(빈 디렉터리)이므로 기존 서비스/모듈/API 영향 없음.
- 외부 의존: YouTube(yt-dlp), `claude` CLI(사용자 구독 사용량 소모), `ffmpeg`(병합/자막 임베드).
- 성능: 55분 영상 기준 번역 호출 약 10~15회(배치 60문장), 4개 병렬 → 수 분 내 완료 예상.

## 3. 구현 방법 비교

### 3-1. 원문 자막 확보
| 방법 | 장점 | 단점 |
| --- | --- | --- |
| A. YouTube 자막 json3(수동 > 자동 `-orig`) | 빠름, 무료, 단어 타임스탬프 | 자막 없는 영상은 불가 |
| B. Whisper 로컬 음성 인식 | 자막 없어도 동작 | 느림, 모델 다운로드 필요 |

**선택: A 우선, 자막이 없으면 B로 폴백.** B는 `faster-whisper`를 optional extra(`youtune[whisper]`)로 두어 기본 설치를 가볍게 유지한다.

### 3-2. 한국어 번역
| 방법 | 장점 | 단점 |
| --- | --- | --- |
| A. YouTube 자동번역 트랙 | 무료 | 429로 사실상 사용 불가, 품질 낮음 |
| B. Claude API (SDK) | 품질 최상, 안정적 | API 키 필요 |
| C. Claude Code CLI 헤드리스 (`claude -p --json-schema`) | API 키 불필요(구독), 품질 최상, 구조화 출력 지원 | 프로세스 기동 오버헤드, CLI 버전 의존 |

**선택: C (사용자 선택).** `--tools ""`, `--strict-mcp-config`, `--setting-sources ""`, `--system-prompt`로 도구/MCP/훅/CLAUDE.md를 배제하고 번역만 수행한다. 응답은 `--json-schema`로 `{translations: [{index, text}]}` 형태를 강제하고, 인덱스 불일치 시 재시도한다.

### 3-3. 자막 단위
- 자동 자막 이벤트는 문장 중간에서 끊긴다 → 그대로 번역하면 어순이 다른 한국어가 어색해진다.
- **선택: 단어 타임스탬프를 문장 단위(구두점/휴지/최대 길이 기준)로 재병합한 뒤 1:1 번역.**

## 4. 구현 단계
- [x] Step 1: 프로젝트 스캐폴딩 — `pyproject.toml`(uv, src 레이아웃), `Makefile`, `.gitignore`
- [x] Step 2: `subtitles.py` — `Cue` 모델, json3 파싱, 문장 병합, SRT 직렬화
- [x] Step 3: `downloader.py` — yt-dlp 메타데이터 조회, 자막 트랙 선택/다운로드, 영상/오디오 다운로드
- [x] Step 4: `translator.py` — Claude Code CLI 배치 번역(병렬, 문맥 제공, 검증, 재시도)
- [x] Step 5: `transcriber.py` — faster-whisper 폴백(optional)
- [x] Step 6: `muxer.py` — ffmpeg로 mp4에 소프트 자막 트랙 임베드(`--embed`)
- [x] Step 7: `cli.py` — 인자 파싱, 파이프라인 오케스트레이션, 에러 처리/종료 코드
- [x] Step 8: README.md, `docs/ARCHITECTURE.md`
- [x] Step 9: 대상 영상으로 실제 실행 검증

## 5. 테스트 계획
**단위 테스트:**
- [x] json3 파싱: 자동 자막(단어 오프셋, `aAppend` 개행 이벤트 제거), 수동 자막(오프셋 없음)
- [x] 문장 병합: 구두점 종료, 긴 휴지, 최대 길이, 빈 입력
- [x] SRT 직렬화: 타임스탬프 포맷(시간 초과 포함), 겹침 보정, 번호
- [x] 자막 트랙 선택: 수동 한국어 우선, 수동 원문 > 자동 `-orig` > 자동 원문, 언어 미지정 시 `-orig` 추론, 트랙 없음
- [x] 번역기: 배치 분할/문맥, 정상 응답 매핑, 인덱스 불일치 재시도, CLI 실패/JSON 오류/`is_error` 재시도 후 예외, `claude` 미설치 예외
- [x] 임베드: ffmpeg 명령 구성, 실패 시 원본 보존
- [x] CLI: 인자 파싱, 한국어 자막 존재 시 번역 생략, 자막 없음 + whisper 미설치 에러 메시지, `--no-video` 경로

**통합 테스트:** (`YOUTUNE_INTEGRATION=1`일 때만 실행)
- [x] 시나리오 1: 실제 `claude` CLI로 소량 문장 번역 → 한국어 결과, 인덱스 일치
- [x] 시나리오 2: 실제 YouTube 메타데이터/자막 다운로드 → 문장 병합 결과 비어있지 않음
- [x] 시나리오 3: 대상 영상 전체 실행(영상 다운로드 + `.ko.srt` 생성 + `--embed`) 수동 검증

## 6. 사이드 이펙트
- 기존 기능 영향: 해당 없음 (신규 프로젝트).
- 하위 호환성: 해당 없음.
- 마이그레이션: 해당 없음.
- `claude -p` 호출은 사용자 Claude 구독 사용량을 소모한다 → 대응 완료 (README 명시, `--model`로 모델 선택).

## 7. 보안 검토
- OWASP A03(Injection): 모든 외부 프로세스(`claude`, `ffmpeg`)는 인자 리스트로 실행, `shell=True` 금지. 프롬프트는 stdin으로 전달(프로세스 목록 노출 방지).
- 프롬프트 인젝션: 자막/제목은 신뢰할 수 없는 입력 → 번역 세션은 도구·MCP·설정 전부 비활성화(`--tools ""` 등)하여 영향은 번역 품질로 한정. 출력은 스키마 + 인덱스 검증.
- 경로: 파일명은 yt-dlp의 sanitize 결과만 사용, 출력 디렉터리 밖으로 쓰지 않음.
- 인증/인가 변경: 없음. 민감 데이터: 없음(API 키 미사용). PCI-DSS: 해당 없음.

## 8. 구현 중 변경 사항 (계획 대비)
1차 전체 실행(616큐)에서 한국어 큐 11개가 `.` 또는 영어 원문으로 남는 문제를 발견했다.

- 원인: 쉼표 없는 긴 문장이 140자 하드 제한에서 끝 1~3단어만 잘려 별도 큐가 됐고, 모델이 그 의미를 앞 줄 번역에 합치면서 꼬리 큐에 `.`/원문을 반환.
- 대응 (모두 계획된 파일 내 변경)
  - `subtitles.py`: 하드 제한 초과 시 마지막 절(쉼표) 경계에서 분할, 없으면 중간 지점에서 분할 → 꼬리 조각 0개.
  - `translator.py`: 프롬프트에 "앞 줄에 합쳤으면 빈 문자열, 원문 복사 금지" 규칙 추가. 단어 없는 번역은 앞 큐의 표시 시간을 연장해 합친다.
  - `downloader.py`: 기본 포맷을 H.264(avc1) 우선으로 변경. 1차 실행에서 VP9-in-mp4가 받아져 QuickTime 재생 불가.

## 9. 검증 결과
- `make lint` (ruff + mypy --strict) 통과, `make test` 86개 통과, `make test-integration` 2개 통과.
- 대상 영상 실행 (`--embed`): 원문 717큐 → 한국어 707큐(10개 앞 줄로 합침), 한글 없는 큐 0, 원문 복사 0, 재시도 0, 타이밍 단조·비중첩.
- mp4: h264 + aac + mov_text(`language=kor`, default) 확인.
