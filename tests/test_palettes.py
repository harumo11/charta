"""ビルトインカラーパレット（`app/model/palettes.py`）の形と中身（Qt 不要）。"""

from __future__ import annotations

import re

from app.model.palettes import (
    BASIC_COLORS,
    PALETTES,
    Palette,
    dropdown_colors,
    palette_by_id,
    palette_style_bundles,
)

_HEX_RE = re.compile(r"^#[0-9A-Fa-f]{6}$")

_EXPECTED_IDS = (
    "material",
    "apple",
    "seaborn_deep",
    "tableau10",
    "okabe_ito",
    "kusumi",
)

_EXPECTED_COLORS = {
    "material": (
        "#F44336",
        "#FF9800",
        "#FFC107",
        "#4CAF50",
        "#009688",
        "#2196F3",
        "#3F51B5",
        "#9C27B0",
    ),
    "apple": (
        "#FF3B30",
        "#FF9500",
        "#FFCC00",
        "#34C759",
        "#30B0C7",
        "#007AFF",
        "#5856D6",
        "#AF52DE",
    ),
    "seaborn_deep": (
        "#4C72B0",
        "#DD8452",
        "#55A868",
        "#C44E52",
        "#8172B3",
        "#937860",
        "#DA8BC3",
        "#8C8C8C",
    ),
    "tableau10": (
        "#4E79A7",
        "#F28E2B",
        "#E15759",
        "#76B7B2",
        "#59A14F",
        "#EDC948",
        "#B07AA1",
        "#9C755F",
    ),
    "okabe_ito": (
        "#E69F00",
        "#56B4E9",
        "#009E73",
        "#F0E442",
        "#0072B2",
        "#D55E00",
        "#CC79A7",
        "#000000",
    ),
    "kusumi": (
        "#C89EA3",
        "#C68B77",
        "#C9A86A",
        "#9CAF88",
        "#8FA8BF",
        "#A79AB2",
        "#B5A79A",
        "#6E6A67",
    ),
}


def test_there_are_exactly_six_palettes_in_the_contract_order() -> None:
    assert tuple(p.id for p in PALETTES) == _EXPECTED_IDS


def test_each_palette_has_exactly_eight_colors() -> None:
    for palette in PALETTES:
        assert len(palette.colors) == 8, palette.id


def test_colors_match_hex_format() -> None:
    for palette in PALETTES:
        for color in palette.colors:
            assert _HEX_RE.match(color), f"{palette.id}: {color!r} is not #RRGGBB"


def test_colors_match_contract_values_verbatim() -> None:
    for palette in PALETTES:
        assert palette.colors == _EXPECTED_COLORS[palette.id], palette.id


def test_palette_ids_are_unique() -> None:
    ids = [p.id for p in PALETTES]
    assert len(ids) == len(set(ids))


def test_palettes_have_non_empty_name_and_note() -> None:
    for palette in PALETTES:
        assert palette.name
        assert palette.note


def test_palette_by_id_found() -> None:
    palette = palette_by_id("okabe_ito")
    assert palette is not None
    assert palette.name == "Okabe-Ito（カラーユニバーサル）"


def test_palette_by_id_not_found_returns_none() -> None:
    assert palette_by_id("no_such_palette") is None


def test_palette_is_frozen() -> None:
    palette = PALETTES[0]
    try:
        palette.id = "changed"  # type: ignore[misc]
    except Exception:
        pass
    else:
        raise AssertionError("Palette は frozen dataclass のはず")


def test_palette_style_bundles_key_format_and_content() -> None:
    palette = palette_by_id("material")
    assert palette is not None
    bundles = palette_style_bundles(palette)

    expected_keys = {f"material-{i}" for i in range(1, 9)}
    assert set(bundles.keys()) == expected_keys

    for i, color in enumerate(palette.colors, start=1):
        bundle = bundles[f"material-{i}"]
        assert bundle == {"fill": color, "stroke": color, "color": color}


def test_palette_style_bundles_is_one_indexed() -> None:
    palette = palette_by_id("apple")
    assert palette is not None
    bundles = palette_style_bundles(palette)
    assert "apple-0" not in bundles
    assert "apple-1" in bundles
    assert "apple-8" in bundles
    assert "apple-9" not in bundles


def test_palette_style_bundles_works_for_every_builtin_palette() -> None:
    for palette in PALETTES:
        bundles = palette_style_bundles(palette)
        assert len(bundles) == 8
        for key, bundle in bundles.items():
            assert key.startswith(f"{palette.id}-")
            assert set(bundle.keys()) == {"fill", "stroke", "color"}


def test_palette_dataclass_shape() -> None:
    """フィールド名が契約どおりであることの回帰（typo で壊れたら他担当が読めなくなる）。"""
    palette = Palette(id="x", name="X", note="note", colors=("#000000",) * 8)
    assert palette.id == "x"
    assert palette.name == "X"
    assert palette.note == "note"
    assert palette.colors == ("#000000",) * 8


# --------------------------------------------------------------------------
# 色ドロップダウン（要望8/11、P2契約 D1-2）: BASIC_COLORS / dropdown_colors
# --------------------------------------------------------------------------


def test_basic_colors_are_eight_uppercase_hex_values() -> None:
    assert len(BASIC_COLORS) == 8
    for color in BASIC_COLORS:
        assert _HEX_RE.match(color), color
        assert color == color.upper()


def test_basic_colors_match_contract_values_verbatim() -> None:
    assert BASIC_COLORS == (
        "#000000",
        "#808080",
        "#D9D9D9",
        "#FFFFFF",
        "#FF0000",
        "#FFFF00",
        "#00B050",
        "#0070C0",
    )


def test_dropdown_colors_with_no_palette_falls_back_to_basic_colors_plus_bw() -> None:
    # BASIC_COLORS はすでに黒・白を含むので、追加で重複することはない。
    assert dropdown_colors(None) == BASIC_COLORS


def test_dropdown_colors_appends_missing_black_and_white() -> None:
    palette = palette_by_id("tableau10")
    assert palette is not None
    result = dropdown_colors(palette)
    assert result[:8] == palette.colors
    assert result[8:] == ("#000000", "#FFFFFF")
    assert len(result) == 10


def test_dropdown_colors_does_not_duplicate_black_already_in_palette() -> None:
    # okabe_ito の8色目はすでに #000000。黒は重複追加せず、白だけ足す。
    palette = palette_by_id("okabe_ito")
    assert palette is not None
    result = dropdown_colors(palette)
    assert result.count("#000000") == 1
    assert result[-1] == "#FFFFFF"
    assert len(result) == 9


def test_dropdown_colors_matching_is_case_insensitive() -> None:
    lower_case_bw = Palette(
        id="lowercase",
        name="lowercase",
        note="",
        colors=(
            "#111111",
            "#222222",
            "#333333",
            "#444444",
            "#555555",
            "#666666",
            "#000000",
            "#ffffff",
        ),
    )
    result = dropdown_colors(lower_case_bw)
    # 既出（大文字小文字を無視）なので黒・白は追加されない。かつ全部大文字。
    assert result == (
        "#111111",
        "#222222",
        "#333333",
        "#444444",
        "#555555",
        "#666666",
        "#000000",
        "#FFFFFF",
    )


def test_dropdown_colors_all_returned_as_uppercase() -> None:
    for color in dropdown_colors(palette_by_id("apple")):
        assert color == color.upper()
