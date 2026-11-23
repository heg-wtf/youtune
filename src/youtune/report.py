"""A self-contained Korean HTML report of what was made from a video.

The editor writes a manifest (JSON) next to the outputs; this module checks it
and renders one HTML file with no scripts and no network: posters are embedded
as small JPEGs, every text field is escaped (titles and captions come from the
video and are untrusted), and every path must stay inside the manifest's folder.

```json
{
  "mode": "shorts",
  "source": {"url": "https://www.youtube.com/watch?v=…", "title": "…", "channel": "…",
             "duration_seconds": 620.5, "own_video": false},
  "summary": "…",
  "outputs": [
    {"kind": "short", "file": "sar-pull.mp4", "poster": "sar-pull.jpg", "title": "…",
     "duration_seconds": 39.7, "source_ranges": ["07:10-07:28"], "reason": "…",
     "upload": {"title": "…", "description": "…", "hashtags": ["#포켓몬카드"]}},
    {"kind": "subtitles", "file": "video.ko.srt"}
  ],
  "subtitle_file": "video.ko.srt",
  "notes": ["…"]
}
```
"""

import base64
import html
import io
import json
from collections.abc import Sequence
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any

from PIL import Image

from youtune.analyze import format_clock
from youtune.errors import SpecificationError, SubtitleParseError
from youtune.subtitles import Cue, read_srt

MAXIMUM_REPORT_BYTES = 5 * 1024 * 1024
MAXIMUM_OUTPUTS = 10
MAXIMUM_TEXT_CHARACTERS = 5000
POSTER_WIDTHS = (360, 240)
TRANSCRIPT_LIMITS = (None, 400, 120)
MODES = {"shorts": "쇼츠 리포트", "subtitles": "한국어 자막 리포트"}
KINDS = {"short": "쇼츠", "subtitled_video": "자막 영상", "subtitles": "자막 파일"}
IMAGE_SUFFIXES = (".jpg", ".jpeg", ".png", ".webp")


@dataclass(frozen=True, slots=True)
class Upload:
    title: str = ""
    description: str = ""
    hashtags: tuple[str, ...] = ()


@dataclass(frozen=True, slots=True)
class Output:
    kind: str
    file: Path
    title: str = ""
    duration_seconds: float | None = None
    poster: Path | None = None
    source_ranges: tuple[str, ...] = ()
    reason: str = ""
    upload: Upload | None = None


@dataclass(frozen=True, slots=True)
class Manifest:
    base: Path
    mode: str
    url: str
    title: str
    outputs: tuple[Output, ...]
    channel: str = ""
    source_duration: float | None = None
    own_video: bool = False
    summary: str = ""
    subtitle_file: Path | None = None
    notes: tuple[str, ...] = field(default=())


class _Problems:
    def __init__(self) -> None:
        self.items: list[str] = []

    def add(self, path: str, message: str) -> None:
        self.items.append(f"{path}: {message}")


def load_manifest(path: Path) -> Manifest:
    """Read and check a manifest; paths in it are relative to its folder.

    Raises:
        SpecificationError: Not JSON, or any field / path is invalid.
    """
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as error:
        raise SpecificationError(f"cannot read {path.name}: {error}") from error
    return parse_manifest(payload, base=path.resolve().parent)


def parse_manifest(payload: Any, *, base: Path) -> Manifest:
    problems = _Problems()
    if not isinstance(payload, dict):
        raise SpecificationError("manifest: expected an object")
    mode = payload.get("mode")
    if mode not in MODES:
        problems.add("mode", f"one of {', '.join(MODES)}")
    source = payload.get("source")
    if not isinstance(source, dict):
        problems.add("source", "expected an object")
        source = {}
    url = _text(source.get("url"), "source.url", problems, required=True)
    if url and not url.startswith(("https://", "http://")):
        problems.add("source.url", "expected an http(s) URL")
    title = _text(source.get("title"), "source.title", problems, required=True)
    raw_outputs = payload.get("outputs")
    if not isinstance(raw_outputs, list) or not 1 <= len(raw_outputs) <= MAXIMUM_OUTPUTS:
        problems.add("outputs", f"expected 1-{MAXIMUM_OUTPUTS} entries")
        raw_outputs = []
    outputs = [
        output
        for index, raw in enumerate(raw_outputs)
        if (output := _output(raw, f"outputs[{index}]", base, problems)) is not None
    ]
    subtitle_file = None
    if payload.get("subtitle_file") is not None:
        subtitle_file = _inside_path(payload["subtitle_file"], "subtitle_file", base, problems)
    notes = payload.get("notes", [])
    if not isinstance(notes, list) or not all(isinstance(note, str) for note in notes):
        problems.add("notes", "expected a list of strings")
        notes = []
    duration = source.get("duration_seconds")
    if duration is not None and not _is_number(duration):
        problems.add("source.duration_seconds", "expected seconds")
        duration = None
    channel = _text(source.get("channel"), "source.channel", problems)
    summary = _text(payload.get("summary"), "summary", problems)
    if problems.items:
        raise SpecificationError("invalid report manifest:\n- " + "\n- ".join(problems.items))
    return Manifest(
        base=base,
        mode=str(mode),
        url=url,
        title=title,
        outputs=tuple(outputs),
        channel=channel,
        source_duration=float(duration) if duration is not None else None,
        own_video=source.get("own_video") is True,
        summary=summary,
        subtitle_file=subtitle_file,
        notes=tuple(note.strip() for note in notes if note.strip()),
    )


def _output(raw: Any, path: str, base: Path, problems: _Problems) -> Output | None:
    if not isinstance(raw, dict):
        problems.add(path, "expected an object")
        return None
    kind = raw.get("kind")
    if kind not in KINDS:
        problems.add(f"{path}.kind", f"one of {', '.join(KINDS)}")
        return None
    file = _inside_path(raw.get("file"), f"{path}.file", base, problems)
    poster = None
    if raw.get("poster") is not None:
        poster = _inside_path(raw["poster"], f"{path}.poster", base, problems)
        if poster is not None and poster.suffix.lower() not in IMAGE_SUFFIXES:
            problems.add(f"{path}.poster", "expected a JPG/PNG/WEBP image")
            poster = None
    duration = raw.get("duration_seconds")
    if duration is not None and not _is_number(duration):
        problems.add(f"{path}.duration_seconds", "expected seconds")
        duration = None
    ranges = raw.get("source_ranges", [])
    if not isinstance(ranges, list) or not all(isinstance(entry, str) for entry in ranges):
        problems.add(f"{path}.source_ranges", "expected a list of strings")
        ranges = []
    upload = None
    if raw.get("upload") is not None:
        upload = _upload(raw["upload"], f"{path}.upload", problems)
    if file is None:
        return None
    return Output(
        kind=str(kind),
        file=file,
        title=_text(raw.get("title"), f"{path}.title", problems),
        duration_seconds=float(duration) if duration is not None else None,
        poster=poster,
        source_ranges=tuple(entry.strip() for entry in ranges if entry.strip()),
        reason=_text(raw.get("reason"), f"{path}.reason", problems),
        upload=upload,
    )


def _upload(raw: Any, path: str, problems: _Problems) -> Upload | None:
    if not isinstance(raw, dict):
        problems.add(path, "expected an object")
        return None
    hashtags = raw.get("hashtags", [])
    if not isinstance(hashtags, list) or not all(isinstance(tag, str) for tag in hashtags):
        problems.add(f"{path}.hashtags", "expected a list of strings")
        hashtags = []
    return Upload(
        title=_text(raw.get("title"), f"{path}.title", problems),
        description=_text(raw.get("description"), f"{path}.description", problems),
        hashtags=tuple(tag.strip() for tag in hashtags if tag.strip()),
    )


def _text(value: Any, path: str, problems: _Problems, *, required: bool = False) -> str:
    if value is None:
        if required:
            problems.add(path, "required")
        return ""
    if not isinstance(value, str):
        problems.add(path, "expected text")
        return ""
    if len(value) > MAXIMUM_TEXT_CHARACTERS:
        problems.add(path, f"at most {MAXIMUM_TEXT_CHARACTERS} characters")
        return ""
    if required and not value.strip():
        problems.add(path, "required")
    return value.strip()


def _inside_path(value: Any, path: str, base: Path, problems: _Problems) -> Path | None:
    """A relative path that resolves to an existing file inside ``base``."""
    if not isinstance(value, str) or not value.strip():
        problems.add(path, "expected a file name relative to the manifest")
        return None
    relative = Path(value.strip())
    if relative.is_absolute():
        problems.add(path, "must be relative to the manifest's folder")
        return None
    resolved = (base / relative).resolve()
    if not resolved.is_relative_to(base.resolve()):
        problems.add(path, "must stay inside the manifest's folder")
        return None
    if not resolved.is_file():
        problems.add(path, f"file not found: {value}")
        return None
    return resolved


def _is_number(value: Any) -> bool:
    return isinstance(value, int | float) and not isinstance(value, bool)


# ---------------------------------------------------------------- rendering


def _poster_data_uri(path: Path, width: int) -> str:
    with Image.open(path) as image:
        picture = image.convert("RGB")
        if picture.width > width:
            picture = picture.resize(
                (width, round(picture.height * width / picture.width)), Image.Resampling.LANCZOS
            )
        buffer = io.BytesIO()
        picture.save(buffer, format="JPEG", quality=72, optimize=True)
    return "data:image/jpeg;base64," + base64.b64encode(buffer.getvalue()).decode("ascii")


def _size_label(path: Path) -> str:
    size = path.stat().st_size
    if size >= 1024 * 1024 * 1024:
        return f"{size / 1024**3:.1f} GB"
    if size >= 1024 * 1024:
        return f"{size / 1024**2:.1f} MB"
    return f"{max(1, round(size / 1024))} KB"


def _escape(text: str) -> str:
    return html.escape(text, quote=True)


def _paragraphs(text: str) -> str:
    return "".join(
        f"<p>{_escape(block).replace(chr(10), '<br>')}</p>"
        for block in text.split("\n\n")
        if block.strip()
    )


def _output_card(output: Output, poster_width: int) -> str:
    chips = [KINDS[output.kind]]
    if output.duration_seconds is not None:
        chips.append(format_clock(output.duration_seconds))
    chips.append(_size_label(output.file))
    poster = (
        f'<img class="poster" src="{_poster_data_uri(output.poster, poster_width)}" alt="">'
        if output.poster is not None
        else ""
    )
    parts = [
        f'<article class="card{" with-poster" if poster else ""}">',
        poster,
        '<div class="card-body">',
        f"<h2>{_escape(output.title or output.file.name)}</h2>",
        '<p class="chips">' + "".join(f"<span>{_escape(chip)}</span>" for chip in chips) + "</p>",
        f'<p class="file">첨부 파일 <code>{_escape(output.file.name)}</code></p>',
    ]
    if output.source_ranges:
        parts.append(
            '<p class="ranges">원본 구간 '
            + ", ".join(_escape(entry) for entry in output.source_ranges)
            + "</p>"
        )
    if output.reason:
        parts.append(f'<div class="reason">{_paragraphs(output.reason)}</div>')
    if output.upload is not None:
        upload = output.upload
        rows = []
        if upload.title:
            rows.append(f"<dt>제목</dt><dd>{_escape(upload.title)}</dd>")
        if upload.description:
            rows.append(f'<dt>설명</dt><dd class="pre">{_escape(upload.description)}</dd>')
        if upload.hashtags:
            rows.append(f"<dt>해시태그</dt><dd>{_escape(' '.join(upload.hashtags))}</dd>")
        if rows:
            parts.append(
                '<div class="upload"><h3>업로드 문구</h3><dl>' + "".join(rows) + "</dl></div>"
            )
    parts.append("</div></article>")
    return "".join(parts)


def _transcript(cues: Sequence[Cue], limit: int | None) -> str:
    shown = cues if limit is None else cues[:limit]
    items = "".join(
        f"<li><time>{format_clock(cue.start_milliseconds / 1000)}</time>{_escape(cue.text)}</li>"
        for cue in shown
    )
    more = (
        f'<p class="more">… 나머지 {len(cues) - len(shown)}줄은 첨부한 자막 파일에 있습니다.</p>'
        if len(shown) < len(cues)
        else ""
    )
    return (
        f'<details class="transcript"><summary>한국어 자막 전체 ({len(cues)}줄)</summary>'
        f"<ol>{items}</ol>{more}</details>"
    )


STYLE = """
:root{--background:#fafafa;--surface:#fff;--text:#16161a;--muted:#5f6070;--line:#e4e4ea;
--accent:#e5243b;--chip:#f1f1f4;color-scheme:light dark}
@media (prefers-color-scheme:dark){:root{--background:#0f0f12;--surface:#18181d;--text:#f2f2f5;
--muted:#a3a3b2;--line:#2a2a33;--accent:#ff5266;--chip:#24242c}}
*{box-sizing:border-box}
body{margin:0;background:var(--background);color:var(--text);
font:15px/1.6 -apple-system,BlinkMacSystemFont,"Apple SD Gothic Neo","Noto Sans KR",sans-serif;
word-break:keep-all;overflow-wrap:anywhere}
main{max-width:760px;margin:0 auto;padding:28px 16px 40px}
.eyebrow{margin:0 0 6px;color:var(--accent);font-weight:700;font-size:13px;letter-spacing:.02em}
h1{margin:0 0 8px;font-size:24px;line-height:1.3}
.meta{margin:0;color:var(--muted);font-size:13px}
.summary{margin:20px 0;padding:14px 16px;border-left:3px solid var(--accent);
background:var(--surface);border-radius:0 10px 10px 0}
.summary p{margin:0 0 6px}.summary p:last-child{margin:0}
.card{display:flex;gap:16px;margin:14px 0;padding:14px;background:var(--surface);
border:1px solid var(--line);border-radius:14px}
.poster{width:150px;flex:none;align-self:flex-start;border-radius:10px;background:#000}
.card-body{min-width:0;flex:1}
h2{margin:0 0 6px;font-size:17px;line-height:1.35}
h3{margin:12px 0 4px;font-size:13px;color:var(--muted)}
.chips{display:flex;flex-wrap:wrap;gap:6px;margin:0 0 8px}
.chips span{padding:2px 8px;border-radius:999px;background:var(--chip);font-size:12px}
.chips span:first-child{background:var(--accent);color:#fff;font-weight:700}
.file,.ranges{margin:2px 0;font-size:13px;color:var(--muted)}
code{font:12px ui-monospace,Menlo,monospace;color:var(--text)}
.reason p{margin:8px 0 0}
dl{margin:0;display:grid;grid-template-columns:auto 1fr;gap:4px 10px;font-size:14px}
dt{color:var(--muted)}dd{margin:0}.pre{white-space:pre-wrap}
.notes ul{padding-left:20px}
.transcript{margin:20px 0;padding:12px 16px;background:var(--surface);border:1px solid var(--line);
border-radius:14px}
.transcript summary{cursor:pointer;font-weight:700}
.transcript ol{list-style:none;margin:10px 0 0;padding:0;font-size:14px}
.transcript li{display:flex;gap:10px;padding:3px 0;border-top:1px solid var(--line)}
.transcript time{flex:none;color:var(--muted);font:12px/1.9 ui-monospace,Menlo,monospace}
.more{color:var(--muted);font-size:13px}
footer{margin-top:28px;color:var(--muted);font-size:12px}
footer p{margin:2px 0}
@media (max-width:520px){.card{flex-direction:column}.poster{width:100%;max-width:240px}}
"""


def render_report(
    manifest: Manifest,
    *,
    poster_width: int = POSTER_WIDTHS[0],
    transcript_limit: int | None = None,
    generated_at: datetime | None = None,
) -> str:
    cues: list[Cue] = []
    if manifest.subtitle_file is not None:
        try:
            cues = read_srt(manifest.subtitle_file)
        except SubtitleParseError as error:
            raise SpecificationError(f"subtitle_file: {error}") from error
    meta = [part for part in (manifest.channel,) if part]
    if manifest.source_duration is not None:
        meta.append(f"원본 {format_clock(manifest.source_duration)}")
    meta.append(manifest.url)
    body = [
        "<header>",
        f'<p class="eyebrow">{MODES[manifest.mode]}</p>',
        f"<h1>{_escape(manifest.title)}</h1>",
        f'<p class="meta">{" · ".join(_escape(part) for part in meta)}</p>',
        "</header>",
    ]
    if manifest.summary:
        body.append(f'<section class="summary">{_paragraphs(manifest.summary)}</section>')
    body.append('<section class="outputs">')
    body += [_output_card(output, poster_width) for output in manifest.outputs]
    body.append("</section>")
    if manifest.notes:
        body.append(
            '<section class="notes"><h3>메모</h3><ul>'
            + "".join(f"<li>{_escape(note)}</li>" for note in manifest.notes)
            + "</ul></section>"
        )
    if cues:
        body.append(_transcript(cues, transcript_limit))
    moment = (generated_at or datetime.now().astimezone()).strftime("%Y-%m-%d %H:%M")
    footer = [f"<p>원본: {_escape(manifest.url)}</p>"]
    if not manifest.own_video:
        footer.append(
            "<p>다른 채널의 영상입니다. 다시 올리려면 원저작자의 허락이 필요할 수 있습니다.</p>"
        )
    footer.append(f"<p>youtune · {moment}</p>")
    body.append("<footer>" + "".join(footer) + "</footer>")
    return (
        '<!doctype html><html lang="ko"><head><meta charset="utf-8">'
        '<meta name="viewport" content="width=device-width,initial-scale=1">'
        f"<title>{_escape(manifest.title)} · {MODES[manifest.mode]}</title>"
        f"<style>{STYLE}</style></head><body><main>{''.join(body)}</main></body></html>\n"
    )


def write_report(manifest: Manifest, output_path: Path) -> Path:
    """Render and write the report, shrinking posters and the transcript until it
    fits in 5 MiB (the chat's page limit).

    Raises:
        SpecificationError: It does not fit even at the smallest settings.
    """
    for poster_width in POSTER_WIDTHS:
        for limit in TRANSCRIPT_LIMITS:
            document = render_report(manifest, poster_width=poster_width, transcript_limit=limit)
            if len(document.encode("utf-8")) <= MAXIMUM_REPORT_BYTES:
                output_path.write_text(document, encoding="utf-8")
                return output_path
    raise SpecificationError("the report does not fit in 5 MiB; attach fewer posters")
