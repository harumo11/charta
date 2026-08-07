"""`app/scene/arrange.py::layout_positions` のテスト（Qt 不要）。"""

from __future__ import annotations

import pytest

from app.scene import arrange


def test_row_spaces_items_by_width_plus_gap() -> None:
    boxes = {1: (0.0, 0.0, 100.0, 50.0), 2: (500.0, 900.0, 60.0, 50.0)}
    result = arrange.layout_positions(boxes, [1, 2], "row", gap=20.0, origin=(0.0, 0.0))
    assert result[1] == (0.0, 0.0)
    assert result[2] == (120.0, 0.0)


def test_row_follows_the_given_order_not_the_spatial_order() -> None:
    """`distribute_positions` は中心座標で並べ替えるが、こちらは ids の順を尊重する。"""
    boxes = {1: (900.0, 0.0, 100.0, 50.0), 2: (0.0, 0.0, 100.0, 50.0)}
    result = arrange.layout_positions(boxes, [1, 2], "row", gap=10.0, origin=(0.0, 0.0))
    assert result[1][0] < result[2][0], "id 1 が先に渡されたので左に来る"


def test_column_stacks_downwards() -> None:
    boxes = {1: (0.0, 0.0, 100.0, 40.0), 2: (0.0, 0.0, 100.0, 60.0)}
    result = arrange.layout_positions(boxes, [1, 2], "column", gap=15.0, origin=(5.0, 5.0))
    assert result[1] == (5.0, 5.0)
    assert result[2] == (5.0, 60.0)


def test_row_align_center_centres_the_shorter_box() -> None:
    boxes = {1: (0.0, 0.0, 50.0, 100.0), 2: (0.0, 0.0, 50.0, 40.0)}
    result = arrange.layout_positions(
        boxes, [1, 2], "row", gap=10.0, align="center", origin=(0.0, 0.0)
    )
    assert result[1] == (0.0, 0.0)
    assert result[2] == (60.0, 30.0)  # (100 - 40) / 2


def test_row_align_end_bottoms_align() -> None:
    boxes = {1: (0.0, 0.0, 50.0, 100.0), 2: (0.0, 0.0, 50.0, 40.0)}
    result = arrange.layout_positions(
        boxes, [1, 2], "row", gap=10.0, align="end", origin=(0.0, 0.0)
    )
    assert result[2][1] == 60.0


def test_column_align_center_centres_the_narrower_box() -> None:
    boxes = {1: (0.0, 0.0, 100.0, 50.0), 2: (0.0, 0.0, 40.0, 50.0)}
    result = arrange.layout_positions(
        boxes, [1, 2], "column", gap=10.0, align="center", origin=(0.0, 0.0)
    )
    assert result[2][0] == 30.0


def test_grid_uses_per_column_maxima_so_columns_stay_straight() -> None:
    """列幅が一律だと、幅の違う要素が混ざったとき列が揃わない。"""
    boxes = {
        1: (0.0, 0.0, 200.0, 50.0),  # 0 行 0 列（幅が大きい）
        2: (0.0, 0.0, 50.0, 50.0),  # 0 行 1 列
        3: (0.0, 0.0, 50.0, 50.0),  # 1 行 0 列
        4: (0.0, 0.0, 50.0, 50.0),  # 1 行 1 列
    }
    result = arrange.layout_positions(
        boxes, [1, 2, 3, 4], "grid", gap=10.0, columns=2, origin=(0.0, 0.0)
    )
    assert result[1][0] == result[3][0] == 0.0
    # 2 列目は 1 列目の最大幅 (200) + gap から始まる。
    assert result[2][0] == result[4][0] == 210.0


def test_grid_uses_per_row_maxima_for_heights() -> None:
    boxes = {
        1: (0.0, 0.0, 50.0, 120.0),
        2: (0.0, 0.0, 50.0, 50.0),
        3: (0.0, 0.0, 50.0, 50.0),
    }
    result = arrange.layout_positions(
        boxes, [1, 2, 3], "grid", gap=10.0, columns=2, origin=(0.0, 0.0)
    )
    assert result[1][1] == result[2][1] == 0.0
    assert result[3][1] == 130.0  # 1 行目の最大高 120 + gap


def test_grid_gap_y_overrides_the_row_gap() -> None:
    boxes = {1: (0.0, 0.0, 50.0, 50.0), 2: (0.0, 0.0, 50.0, 50.0)}
    result = arrange.layout_positions(
        boxes, [1, 2], "grid", gap=10.0, gap_y=100.0, columns=1, origin=(0.0, 0.0)
    )
    assert result[2][1] == 150.0


def test_origin_defaults_to_the_current_union_top_left() -> None:
    boxes = {1: (300.0, 200.0, 50.0, 50.0), 2: (500.0, 400.0, 50.0, 50.0)}
    result = arrange.layout_positions(boxes, [1, 2], "row", gap=10.0)
    assert result[1] == (300.0, 200.0)


def test_grid_requires_columns() -> None:
    boxes = {1: (0.0, 0.0, 10.0, 10.0)}
    with pytest.raises(ValueError, match="columns"):
        arrange.layout_positions(boxes, [1], "grid")
    with pytest.raises(ValueError, match="columns"):
        arrange.layout_positions(boxes, [1], "grid", columns=0)


def test_unknown_mode_and_align_raise() -> None:
    boxes = {1: (0.0, 0.0, 10.0, 10.0)}
    with pytest.raises(ValueError, match="mode"):
        arrange.layout_positions(boxes, [1], "diagonal")
    with pytest.raises(ValueError, match="align"):
        arrange.layout_positions(boxes, [1], "row", align="middle")


def test_empty_input_returns_empty() -> None:
    assert arrange.layout_positions({}, [], "row") == {}
    assert arrange.layout_positions({1: (0.0, 0.0, 1.0, 1.0)}, [99], "row") == {}


def test_ids_missing_from_boxes_are_skipped() -> None:
    boxes = {1: (0.0, 0.0, 50.0, 50.0)}
    result = arrange.layout_positions(boxes, [99, 1], "row", origin=(0.0, 0.0))
    assert result == {1: (0.0, 0.0)}


def test_sizes_are_never_changed() -> None:
    """位置だけを動かす（サイズを揃える機能ではない）。"""
    boxes = {1: (0.0, 0.0, 111.0, 22.0), 2: (0.0, 0.0, 33.0, 44.0)}
    result = arrange.layout_positions(boxes, [1, 2], "row", origin=(0.0, 0.0))
    assert set(result) == {1, 2}
    assert all(len(pos) == 2 for pos in result.values())
