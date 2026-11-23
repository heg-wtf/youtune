"""Find a font that draws Korean text, for every piece of text burned into a video."""

import functools
import os
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path

from PIL import ImageFont

from youtune.errors import FontNotFoundError

FONT_ENVIRONMENT_VARIABLE = "YOUTUNE_FONT"
#: BM Dohyeon is a downloadable macOS font asset; its folder name is a hash.
ASSET_FONT_PATTERNS = (
    "/System/Library/AssetsV2/com_apple_MobileAsset_Font*/*.asset/AssetData/BMDoHyeon-Regular.otf",
)
COLLECTION_CANDIDATES = (
    # (path, preferred style) — the face with that style name is used.
    ("/System/Library/Fonts/AppleSDGothicNeo.ttc", "Bold"),
    ("/usr/share/fonts/opentype/noto/NotoSansCJK-Bold.ttc", "Bold"),
    ("/usr/share/fonts/noto-cjk/NotoSansCJK-Bold.ttc", "Bold"),
    ("/usr/share/fonts/google-noto-cjk/NotoSansCJK-Bold.ttc", "Bold"),
)
FILE_CANDIDATES = ("/usr/share/fonts/truetype/nanum/NanumGothicBold.ttf",)
MAXIMUM_COLLECTION_FACES = 32


@dataclass(frozen=True, slots=True)
class FontFace:
    """A font file and the face inside it (collections hold several)."""

    path: Path
    index: int = 0


def find_font(
    environment: Mapping[str, str] | None = None,
    *,
    root: Path = Path("/"),
) -> FontFace:
    """Pick the font: ``$YOUTUNE_FONT`` (``path`` or ``path#index``), BM Dohyeon,
    Apple SD Gothic Neo Bold, then common Linux CJK fonts.

    Raises:
        FontNotFoundError: None of them exists.
    """
    environment = os.environ if environment is None else environment
    configured = environment.get(FONT_ENVIRONMENT_VARIABLE, "").strip()
    if configured:
        path_text, _, index_text = configured.partition("#")
        path = Path(path_text).expanduser()
        if not path.is_file():
            raise FontNotFoundError(
                f"{FONT_ENVIRONMENT_VARIABLE} points at a missing file: {path}"
            )
        return FontFace(path, int(index_text) if index_text.isdigit() else 0)
    for pattern in ASSET_FONT_PATTERNS:
        matches = sorted(root.glob(pattern.lstrip("/")))
        if matches:
            return FontFace(matches[0])
    for path_text, style in COLLECTION_CANDIDATES:
        path = root / path_text.lstrip("/")
        if path.is_file():
            return FontFace(path, _face_index(path, style))
    for path_text in FILE_CANDIDATES:
        path = root / path_text.lstrip("/")
        if path.is_file():
            return FontFace(path)
    raise FontNotFoundError(
        f"no Korean font found; install one or set {FONT_ENVIRONMENT_VARIABLE}=<path to .otf/.ttf>"
    )


def _face_index(path: Path, style: str) -> int:
    """The index of the face named ``style`` in a collection, 0 when none is."""
    for index in range(MAXIMUM_COLLECTION_FACES):
        try:
            font = ImageFont.truetype(str(path), 12, index=index)
        except OSError:
            break
        if font.getname()[1] == style:
            return index
    return 0


@functools.lru_cache(maxsize=64)
def load_font(face: FontFace, size: int) -> ImageFont.FreeTypeFont:
    """The face at ``size`` pixels, cached because every frame asks for the same few."""
    return ImageFont.truetype(str(face.path), size, index=face.index)
