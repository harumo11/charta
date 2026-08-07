"""`app/graphics/legibility.py` のテスト（Qt 不要）。"""

from __future__ import annotations

import pytest

from app.graphics import legibility


def test_black_on_white_is_the_maximum_ratio() -> None:
    assert legibility.contrast_ratio("#000000", "#ffffff") == pytest.approx(21.0)


def test_same_colour_is_the_minimum_ratio() -> None:
    assert legibility.contrast_ratio("#3366cc", "#3366cc") == pytest.approx(1.0)


def test_contrast_is_symmetric() -> None:
    assert legibility.contrast_ratio("#123456", "#fedcba") == pytest.approx(
        legibility.contrast_ratio("#fedcba", "#123456")
    )


def test_grey_on_white_sits_just_below_wcag_aa() -> None:
    """`#777` on white ≈ 4.48 — AA の 4.5 を僅かに割る古典的な境界。"""
    ratio = legibility.contrast_ratio("#777777", "#ffffff")
    assert 4.4 < ratio < 4.5
    assert ratio < legibility.WCAG_AA_NORMAL


def test_luminance_bounds() -> None:
    assert legibility.relative_luminance("#000000") == pytest.approx(0.0)
    assert legibility.relative_luminance("#ffffff") == pytest.approx(1.0)


def test_invalid_colour_raises() -> None:
    with pytest.raises(ValueError):
        legibility.relative_luminance("red")


def test_blend_over_at_full_alpha_is_identity() -> None:
    assert legibility.blend_over("#112233", "#ffffff", 1.0) == "#112233"


def test_blend_over_halves_towards_the_background() -> None:
    assert legibility.blend_over("#000000", "#ffffff", 0.5) == "#808080"


def test_faded_text_loses_contrast() -> None:
    solid = legibility.contrast_ratio("#000000", "#ffffff")
    faded = legibility.contrast_ratio(legibility.blend_over("#000000", "#ffffff", 0.2), "#ffffff")
    assert faded < solid


def test_required_ratio_relaxes_for_large_text() -> None:
    assert legibility.required_ratio(9.0) == legibility.WCAG_AA_NORMAL
    assert legibility.required_ratio(20.0) == legibility.WCAG_AA_LARGE


def test_effective_point_size_uses_physical_width() -> None:
    """1600px 幅 = 170mm のアートボードで 18px のフォントは何 pt か。"""
    pt = legibility.effective_point_size(18.0, 1600.0, 170.0)
    expected = 18.0 / (1600.0 / 170.0) / 25.4 * 72.0
    assert pt == pytest.approx(expected)
    assert 5.0 < pt < 6.0  # 論文幅に落とすと 6pt を切る、という実際の失敗


def test_point_size_round_trip() -> None:
    px = legibility.point_size_to_px(9.0, 1600.0, 170.0)
    assert legibility.effective_point_size(px, 1600.0, 170.0) == pytest.approx(9.0)


def test_effective_point_size_is_zero_without_physical_width() -> None:
    assert legibility.effective_point_size(18.0, 1600.0, 0.0) == 0.0
    assert legibility.px_per_mm(1600.0, 0.0) == 0.0


def test_readable_color_picks_the_high_contrast_option() -> None:
    assert legibility.readable_color("#ffffff") == "#000000"
    assert legibility.readable_color("#101020") == "#ffffff"
