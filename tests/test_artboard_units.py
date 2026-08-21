"""px↔mm ヘルパの一本化 + 書き出しスケールの単体テスト（P1 契約 §S0）。

`px_from_mm`/`mm_from_px`/`clamp_artboard_px`/`artboard_with_pixel_size`
（`app/model/document.py`、Qt 非依存）と `artboard_export_scale`
（`app/export/png_exporter.py`）が仕様どおりに振る舞うこと、および
既存の 3 箇所（artboard_presets/png_exporter/schema）の置き換えが
1px も結果を変えていないことを検証する。
"""

from __future__ import annotations

from typing import Any

import pytest

from app.export.png_exporter import artboard_export_scale, artboard_pixel_size
from app.model.document import (
    ARTBOARD_PX_MAX,
    ARTBOARD_PX_MIN,
    Artboard,
    Document,
    Physical,
    artboard_with_pixel_size,
    clamp_artboard_px,
    mm_from_px,
    px_from_mm,
)
from app.ui.artboard_presets import ARTBOARD_PRESETS, preset_px_size


def _artboard(
    width_px: int = 800, height_px: int = 600, width_mm: float = 50.0, dpi: int = 150
) -> Artboard:
    return Artboard(
        width_px=width_px,
        height_px=height_px,
        physical=Physical(width_mm=width_mm, target_dpi=dpi),
    )


def test_px_from_mm_matches_the_existing_formula() -> None:
    # 旧実装は round(width_mm / 25.4 * target_dpi) だった（3 箇所とも同じ式）。
    assert px_from_mm(170.0, 300) == 2008


def test_mm_from_px_is_the_inverse_of_px_from_mm() -> None:
    for width_mm, dpi in [(170.0, 300), (84.0, 300), (250.0, 150), (1.0, 72)]:
        px = px_from_mm(width_mm, dpi)
        back_mm = mm_from_px(px, dpi)
        # mm -> px -> mm -> px の往復で丸め誤差 1px 以内に収まること。
        assert abs(px_from_mm(back_mm, dpi) - px) <= 1


def test_mm_from_px_with_non_positive_dpi_is_zero() -> None:
    assert mm_from_px(100.0, 0) == 0.0
    assert mm_from_px(100.0, -5) == 0.0


def test_clamp_artboard_px_bounds() -> None:
    assert clamp_artboard_px(0) == ARTBOARD_PX_MIN
    assert clamp_artboard_px(-100) == ARTBOARD_PX_MIN
    assert clamp_artboard_px(999999) == ARTBOARD_PX_MAX
    assert clamp_artboard_px(500.4) == 500
    assert clamp_artboard_px(500.6) == 501
    assert isinstance(clamp_artboard_px(500.4), int)


def test_artboard_with_pixel_size_keeps_dpi_and_recomputes_mm() -> None:
    artboard = _artboard()
    new_artboard = artboard_with_pixel_size(artboard, 1600.0, 1200.0)

    assert new_artboard.width_px == 1600
    assert new_artboard.height_px == 1200
    assert new_artboard.physical.target_dpi == 150  # DPI は維持
    assert new_artboard.physical.width_mm == pytest.approx(mm_from_px(1600.0, 150))
    assert new_artboard.background == artboard.background


def test_artboard_with_pixel_size_makes_export_size_equal_to_artboard_px() -> None:
    artboard = _artboard()
    new_artboard = artboard_with_pixel_size(artboard, 1234.0, 987.0)
    doc = Document(artboard=new_artboard)

    assert artboard_pixel_size(doc) == (new_artboard.width_px, new_artboard.height_px)


def test_artboard_with_pixel_size_does_not_mutate_the_input() -> None:
    artboard = _artboard()
    original_physical = artboard.physical

    _ = artboard_with_pixel_size(artboard, 1600.0, 1200.0)

    assert artboard.width_px == 800
    assert artboard.height_px == 600
    assert artboard.physical is original_physical
    assert artboard.physical.width_mm == 50.0


def test_artboard_export_scale_is_one_after_with_pixel_size() -> None:
    artboard = _artboard()
    new_artboard = artboard_with_pixel_size(artboard, 1234.0, 987.0)
    doc = Document(artboard=new_artboard)

    assert artboard_export_scale(doc) == pytest.approx(1.0)


def test_preset_px_size_is_unchanged_by_the_refactor() -> None:
    # 旧実装 `round(width_mm / 25.4 * target_dpi)` で個別に計算した既知値
    # （このテスト自身は px_from_mm を経由しないよう、独立に固定した数値）。
    expected: dict[tuple[float, int, int, int], tuple[int, int]] = {
        (84.0, 300, 4, 3): (992, 744),
        (174.0, 300, 4, 3): (2055, 1541),
        (89.0, 300, 4, 3): (1051, 788),
        (120.0, 300, 4, 3): (1417, 1063),
        (136.0, 300, 4, 3): (1606, 1204),
        (183.0, 300, 4, 3): (2161, 1621),
        (250.0, 150, 16, 9): (1476, 830),
    }
    assert len(ARTBOARD_PRESETS) == len(expected)
    for _label, width_mm, dpi, aspect_w, aspect_h in ARTBOARD_PRESETS:
        key = (width_mm, dpi, aspect_w, aspect_h)
        assert preset_px_size(width_mm, dpi, aspect_w, aspect_h) == expected[key]


def test_agent_schema_artboard_info_export_px_is_unchanged_by_the_refactor(qapp: Any) -> None:
    # app/agent/schema.py:273 の置き換えも同じ式で 1px も変わらないことを確認する。
    from app.agent.schema import artboard_info

    artboard = _artboard(width_px=800, height_px=600, width_mm=170.0, dpi=300)
    doc = Document(artboard=artboard)
    info = artboard_info(doc)
    assert info["export_px"] == [2008, round(2008 * 600 / 800)]
