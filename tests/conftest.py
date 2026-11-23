import pytest
from support import FONT

from youtune.fonts import FontFace


@pytest.fixture
def font() -> FontFace:
    if FONT is None:
        pytest.skip("no Korean font installed")
    return FONT
