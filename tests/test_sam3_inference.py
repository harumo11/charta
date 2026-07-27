"""`app.ai.sam3.Sam3Engine` の実推論テスト（contract_sam3 E-3）。

sam 依存グループ（torch/transformers, facebook/sam3）が未導入、または CUDA が
利用できない環境では全件スキップする（重いモデルロード・ダウンロードを要するため）。
厳密な IoU 検証はせず、候補が得られること・形状・型のみを確認する。
"""

from __future__ import annotations

from typing import Any

import numpy as np
import pytest
from PIL import Image, ImageDraw

from app.ai import sam3

pytestmark = pytest.mark.skipif(
    not sam3.is_available(), reason="sam dependency group not installed"
)


def _cuda_available() -> bool:
    """torch は sam 依存グループ導入時のみ存在するため関数内で遅延 import する。"""
    import torch

    return torch.cuda.is_available()


@pytest.fixture(scope="module")
def engine() -> Any:
    if not _cuda_available():
        pytest.skip("CUDA is not available")

    eng = sam3.get_engine()
    eng.load()
    return eng


@pytest.fixture(scope="module")
def red_square_image() -> tuple[Any, tuple[float, float, float, float]]:
    """640x480 の単色背景に赤い矩形を描いた合成テスト画像と、それを囲む正ボックス。"""
    image = Image.new("RGB", (640, 480), color=(30, 30, 30))
    box = (200.0, 150.0, 400.0, 330.0)
    draw = ImageDraw.Draw(image)
    draw.rectangle(box, fill=(220, 20, 20))
    return image, box


def test_detect_with_text_prompt_only(
    engine: Any, red_square_image: tuple[Any, tuple[float, float, float, float]]
) -> None:
    image, _box = red_square_image
    engine.set_image(image)

    candidates = engine.detect(text="red square", boxes=None)

    assert len(candidates) >= 1
    for candidate in candidates:
        assert candidate.mask.shape == (480, 640)
        assert candidate.mask.dtype == np.bool_ or candidate.mask.dtype == bool
        assert isinstance(candidate.score, float)


def test_detect_with_box_prompt_only(
    engine: Any, red_square_image: tuple[Any, tuple[float, float, float, float]]
) -> None:
    image, box = red_square_image
    engine.set_image(image)

    candidates = engine.detect(text=None, boxes=[(box, 1)])

    assert len(candidates) >= 1
    for candidate in candidates:
        assert candidate.mask.shape == (480, 640)
        assert isinstance(candidate.score, float)


def test_detect_with_text_and_box_prompt(
    engine: Any, red_square_image: tuple[Any, tuple[float, float, float, float]]
) -> None:
    image, box = red_square_image
    engine.set_image(image)

    candidates = engine.detect(text="red square", boxes=[(box, 1)])

    assert len(candidates) >= 1
    for candidate in candidates:
        assert candidate.mask.shape == (480, 640)
        assert isinstance(candidate.score, float)


def test_detect_without_text_or_boxes_raises_value_error(engine: Any) -> None:
    with pytest.raises(ValueError):
        engine.detect(text=None, boxes=None)
