"""ビルトインカラーパレット（`app/model/palettes.py`）の形と中身（Qt 不要）。"""

from __future__ import annotations

import re

from app.model.palettes import PALETTES, Palette, palette_by_id, palette_style_bundles

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
