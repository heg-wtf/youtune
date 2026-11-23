from pathlib import Path

import pytest
from support import needs_font

from youtune.errors import FontNotFoundError
from youtune.fonts import FONT_ENVIRONMENT_VARIABLE, FontFace, find_font, load_font
from youtune.typography import (
    NAMED_COLORS,
    TextLine,
    fit_text,
    parse_color,
    render_lines,
    wrap_text,
)


def touch(path: Path) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(b"not a real font")
    return path


class TestFindFont:
    def test_environment_variable_wins(self, tmp_path: Path) -> None:
        font = touch(tmp_path / "Custom.ttc")

        face = find_font({FONT_ENVIRONMENT_VARIABLE: f"{font}#3"}, root=tmp_path)

        assert face == FontFace(font, 3)

    def test_environment_variable_pointing_nowhere_fails(self, tmp_path: Path) -> None:
        with pytest.raises(FontNotFoundError, match="missing file"):
            find_font({FONT_ENVIRONMENT_VARIABLE: str(tmp_path / "gone.otf")}, root=tmp_path)

    def test_prefers_the_bm_dohyeon_asset(self, tmp_path: Path) -> None:
        asset = touch(
            tmp_path / "System/Library/AssetsV2/com_apple_MobileAsset_Font8/abc.asset"
            "/AssetData/BMDoHyeon-Regular.otf"
        )
        touch(tmp_path / "System/Library/Fonts/AppleSDGothicNeo.ttc")

        assert find_font({}, root=tmp_path) == FontFace(asset)

    def test_falls_back_to_a_system_collection(self, tmp_path: Path) -> None:
        collection = touch(tmp_path / "usr/share/fonts/opentype/noto/NotoSansCJK-Bold.ttc")

        # The fake file cannot be opened, so no face index is found: the first face.
        assert find_font({}, root=tmp_path) == FontFace(collection, 0)

    def test_falls_back_to_nanum(self, tmp_path: Path) -> None:
        nanum = touch(tmp_path / "usr/share/fonts/truetype/nanum/NanumGothicBold.ttf")

        assert find_font({}, root=tmp_path) == FontFace(nanum)

    def test_nothing_installed(self, tmp_path: Path) -> None:
        with pytest.raises(FontNotFoundError, match=FONT_ENVIRONMENT_VARIABLE):
            find_font({}, root=tmp_path)


class TestParseColor:
    def test_names_case_insensitive(self) -> None:
        assert parse_color(" Yellow ") == NAMED_COLORS["yellow"]

    def test_hex(self) -> None:
        assert parse_color("#10ff80") == (16, 255, 128, 255)

    @pytest.mark.parametrize("value", ["purple-ish", "#12345", "rgb(1,2,3)", ""])
    def test_rejects_others(self, value: str) -> None:
        with pytest.raises(ValueError, match="unknown color"):
            parse_color(value)


@needs_font
class TestWrapping:
    def test_breaks_at_spaces(self, font: FontFace) -> None:
        typeface = load_font(font, 40)
        width = typeface.getlength("가나다 라마바") + 1

        assert wrap_text("가나다 라마바 사아자 차카타", typeface, width) == [
            "가나다 라마바",
            "사아자 차카타",
        ]

    def test_breaks_a_word_wider_than_the_line(self, font: FontFace) -> None:
        typeface = load_font(font, 40)
        width = typeface.getlength("가나다") + 1

        lines = wrap_text("가나다라마바사", typeface, width)

        assert "".join(lines) == "가나다라마바사"
        assert all(typeface.getlength(line) <= width for line in lines)

    def test_empty_text_has_no_lines(self, font: FontFace) -> None:
        assert wrap_text("   ", load_font(font, 40), 100) == []

    def test_fit_text_shrinks_before_adding_lines(self, font: FontFace) -> None:
        text = "아주 길어서 한 번에 들어가지 않는 자막 문장을 하나 씁니다"
        width = load_font(font, 60).getlength(text) / 2.2

        lines, size = fit_text(
            text, (255, 255, 255, 255), font, font_size=60, maximum_width=width, maximum_lines=2
        )

        assert len(lines) <= 2
        assert size < 60

    def test_render_lines_fits_every_line(self, font: FontFace) -> None:
        lines = [TextLine("첫 줄"), TextLine("두 번째 줄은 더 깁니다")]

        image = render_lines(lines, font, 50)

        assert image.mode == "RGBA"
        assert image.width >= load_font(font, 50).getlength("두 번째 줄은 더 깁니다")
        assert image.getbbox() is not None
