import copy
import json
from pathlib import Path
from typing import Any

import pytest
from PIL import Image

from youtune import report
from youtune.errors import SpecificationError
from youtune.report import load_manifest, parse_manifest, render_report, write_report

HOSTILE = '<script>alert("x")</script>'


@pytest.fixture
def folder(tmp_path: Path) -> Path:
    (tmp_path / "short.mp4").write_bytes(b"\0" * 2048)
    (tmp_path / "video.ko.srt").write_text(
        "1\n00:00:01,000 --> 00:00:02,000\n첫 줄\n\n2\n00:01:05,000 --> 00:01:06,000\n"
        f"{HOSTILE}\n",
        encoding="utf-8",
    )
    Image.new("RGB", (1080, 1920), (200, 30, 30)).save(tmp_path / "short.jpg")
    return tmp_path


def manifest_document(**changes: Any) -> dict[str, Any]:
    document: dict[str, Any] = {
        "mode": "shorts",
        "source": {
            "url": "https://www.youtube.com/watch?v=abc",
            "title": f"Title {HOSTILE}",
            "channel": "Channel",
            "duration_seconds": 620.5,
        },
        "summary": f"요약 첫 문단\n\n둘째 문단 {HOSTILE}",
        "outputs": [
            {
                "kind": "short",
                "file": "short.mp4",
                "poster": "short.jpg",
                "title": "쇼츠 제목",
                "duration_seconds": 39.7,
                "source_ranges": ["07:10-07:28"],
                "reason": "이유",
                "upload": {
                    "title": "업로드 제목",
                    "description": f"설명\n{HOSTILE}",
                    "hashtags": ["#태그", HOSTILE],
                },
            },
            {"kind": "subtitles", "file": "video.ko.srt"},
        ],
        "subtitle_file": "video.ko.srt",
        "notes": ["메모 하나"],
    }
    document.update(changes)
    return document


class TestManifest:
    def test_parses_and_resolves_paths(self, folder: Path) -> None:
        manifest = parse_manifest(manifest_document(), base=folder)

        assert manifest.outputs[0].file == (folder / "short.mp4").resolve()
        assert manifest.outputs[0].upload is not None
        assert manifest.outputs[0].upload.hashtags == ("#태그", HOSTILE)
        assert manifest.source_duration == 620.5
        assert manifest.own_video is False

    def test_reads_a_file_next_to_the_outputs(self, folder: Path) -> None:
        path = folder / "manifest.json"
        path.write_text(json.dumps(manifest_document()), encoding="utf-8")

        assert load_manifest(path).base == folder.resolve()

    @pytest.mark.parametrize(
        ("change", "fragment"),
        [
            ({"mode": "vlog"}, "mode: one of"),
            ({"source": {"url": "file:///etc/passwd", "title": "t"}}, "source.url"),
            ({"source": {"url": "https://x"}}, "source.title: required"),
            ({"outputs": []}, "outputs: expected 1-10"),
            ({"outputs": [{"kind": "short", "file": "../escape.mp4"}]}, "inside the manifest"),
            ({"outputs": [{"kind": "short", "file": "/etc/hosts"}]}, "must be relative"),
            ({"outputs": [{"kind": "short", "file": "missing.mp4"}]}, "file not found"),
            ({"outputs": [{"kind": "movie", "file": "short.mp4"}]}, "outputs[0].kind"),
            (
                {"outputs": [{"kind": "short", "file": "short.mp4", "poster": "video.ko.srt"}]},
                "JPG/PNG/WEBP",
            ),
            ({"notes": "not a list"}, "notes: expected a list"),
            ({"summary": "x" * 6000}, "summary: at most"),
        ],
    )
    def test_rejects_bad_fields(self, folder: Path, change: dict[str, Any], fragment: str) -> None:
        with pytest.raises(SpecificationError) as error:
            parse_manifest(manifest_document(**change), base=folder)

        assert fragment in str(error.value)

    def test_rejects_a_symlink_that_leaves_the_folder(
        self, folder: Path, tmp_path_factory: Any
    ) -> None:
        outside = tmp_path_factory.mktemp("outside") / "secret.mp4"
        outside.write_bytes(b"secret")
        (folder / "link.mp4").symlink_to(outside)
        document = manifest_document(outputs=[{"kind": "short", "file": "link.mp4"}])

        with pytest.raises(SpecificationError, match="inside the manifest"):
            parse_manifest(document, base=folder)


class TestRender:
    def test_escapes_every_untrusted_field(self, folder: Path) -> None:
        document = render_report(parse_manifest(manifest_document(), base=folder))

        assert "<script" not in document
        assert document.count("&lt;script&gt;") >= 5

    def test_embeds_the_poster_and_lists_the_outputs(self, folder: Path) -> None:
        document = render_report(parse_manifest(manifest_document(), base=folder))

        assert 'src="data:image/jpeg;base64,' in document
        assert "short.mp4" in document and "video.ko.srt" in document
        assert "00:39" in document and "2 KB" in document
        assert "원본 10:20" in document
        assert "한국어 자막 전체 (2줄)" in document

    def test_rights_note_only_for_someone_elses_video(self, folder: Path) -> None:
        theirs = render_report(parse_manifest(manifest_document(), base=folder))
        source = dict(manifest_document()["source"], own_video=True)
        mine = render_report(parse_manifest(manifest_document(source=source), base=folder))

        assert "원저작자의 허락" in theirs
        assert "원저작자의 허락" not in mine

    def test_transcript_limit_points_at_the_file(self, folder: Path) -> None:
        manifest = parse_manifest(manifest_document(), base=folder)

        document = render_report(manifest, transcript_limit=1)

        assert "나머지 1줄은" in document


class TestWrite:
    def test_writes_a_page_under_the_cap(self, folder: Path) -> None:
        manifest = parse_manifest(manifest_document(), base=folder)

        path = write_report(manifest, folder / "report.html")

        assert path.read_text(encoding="utf-8").startswith("<!doctype html>")

    def test_shrinks_until_it_fits(self, folder: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        manifest = parse_manifest(manifest_document(), base=folder)
        full = len(render_report(manifest).encode("utf-8"))
        smallest = len(
            render_report(manifest, poster_width=240, transcript_limit=120).encode("utf-8")
        )
        monkeypatch.setattr(report, "MAXIMUM_REPORT_BYTES", (full + smallest) // 2)

        path = write_report(manifest, folder / "report.html")

        assert path.stat().st_size < full

    def test_gives_up_when_nothing_fits(
        self, folder: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(report, "MAXIMUM_REPORT_BYTES", 100)

        with pytest.raises(SpecificationError, match="does not fit"):
            write_report(parse_manifest(manifest_document(), base=folder), folder / "r.html")

    def test_a_broken_subtitle_file_is_a_manifest_error(self, folder: Path) -> None:
        (folder / "video.ko.srt").write_text("garbage\n", encoding="utf-8")
        manifest = parse_manifest(copy.deepcopy(manifest_document()), base=folder)

        with pytest.raises(SpecificationError, match="subtitle_file"):
            render_report(manifest)
