"""`app/graphics/boxes.py` の純幾何テスト（Qt 不要・fixture 不要）。"""

from __future__ import annotations

import math

import pytest

from app.graphics import boxes

# --------------------------------------------------------------------------
# 基本
# --------------------------------------------------------------------------


def test_normalized_fixes_negative_extents() -> None:
    assert boxes.normalized((10.0, 10.0, -4.0, -6.0)) == (6.0, 4.0, 4.0, 6.0)


def test_rotate_point_is_clockwise_with_y_down() -> None:
    """Qt 規約: 正の角度は（y 下向きの画面で）時計回り。"""
    rotated = boxes.rotate_point((1.0, 0.0), (0.0, 0.0), 90.0)
    assert rotated[0] == pytest.approx(0.0, abs=1e-9)
    assert rotated[1] == pytest.approx(1.0, abs=1e-9)


def test_rotate_point_is_identity_at_zero() -> None:
    p = (3.5, -2.25)
    assert boxes.rotate_point(p, (10.0, 10.0), 0.0) is p


def test_rotated_aabb_at_90_swaps_extents() -> None:
    result = boxes.rotated_aabb((0.0, 0.0, 100.0, 40.0), 90.0)
    assert result[2] == pytest.approx(40.0)
    assert result[3] == pytest.approx(100.0)
    # 中心は不変。
    assert boxes.box_center(result) == pytest.approx((50.0, 20.0))


def test_rotated_aabb_at_45_grows_by_sqrt2() -> None:
    result = boxes.rotated_aabb((0.0, 0.0, 100.0, 100.0), 45.0)
    assert result[2] == pytest.approx(100.0 * math.sqrt(2.0))


def test_inflate_and_area() -> None:
    assert boxes.inflate((10.0, 20.0, 30.0, 40.0), 5.0) == (5.0, 15.0, 40.0, 50.0)
    assert boxes.area((0.0, 0.0, 3.0, 4.0)) == pytest.approx(12.0)


# --------------------------------------------------------------------------
# 軸並行の重なり
# --------------------------------------------------------------------------


def test_disjoint_boxes_do_not_overlap() -> None:
    assert not boxes.boxes_overlap((0.0, 0.0, 10.0, 10.0), (20.0, 0.0, 10.0, 10.0))
    assert boxes.intersection((0.0, 0.0, 10.0, 10.0), (20.0, 0.0, 10.0, 10.0)) is None


def test_edge_touching_is_not_an_overlap() -> None:
    """辺で接するだけを重なりにすると、隙間なく並べた図が全部警告になる。"""
    assert not boxes.boxes_overlap((0.0, 0.0, 10.0, 10.0), (10.0, 0.0, 10.0, 10.0))
    assert boxes.intersection((0.0, 0.0, 10.0, 10.0), (10.0, 0.0, 10.0, 10.0)) is None


def test_partial_overlap_area() -> None:
    inter = boxes.intersection((0.0, 0.0, 10.0, 10.0), (5.0, 5.0, 10.0, 10.0))
    assert inter == (5.0, 5.0, 5.0, 5.0)
    assert boxes.area(inter) == pytest.approx(25.0)


def test_containment_is_an_overlap() -> None:
    assert boxes.boxes_overlap((0.0, 0.0, 100.0, 100.0), (10.0, 10.0, 5.0, 5.0))


# --------------------------------------------------------------------------
# SAT（回転矩形）
# --------------------------------------------------------------------------


def test_sat_agrees_with_aabb_when_unrotated() -> None:
    a = (0.0, 0.0, 10.0, 10.0)
    b = (5.0, 5.0, 10.0, 10.0)
    assert boxes.obb_overlap(a, 0.0, b, 0.0) is boxes.boxes_overlap(a, b)


def test_sat_rejects_what_the_aabb_approximation_would_accept() -> None:
    """SAT を選んだ理由そのものの回帰線。

    45 度回した正方形の外接矩形は元より約 41% 大きい。隣に置いた矩形は
    その外接矩形とは重なるが、回転した正方形の実体とは重なっていない。
    """
    rotated = (0.0, 0.0, 100.0, 100.0)
    # 45 度回した正方形は菱形になる。外接矩形の「隅」は菱形の外側の空白。
    neighbour = (-18.0, -18.0, 12.0, 12.0)

    envelope = boxes.rotated_aabb(rotated, 45.0)
    assert boxes.boxes_overlap(envelope, neighbour), "外接矩形近似なら重なると誤判定する配置"
    assert not boxes.obb_overlap(rotated, 45.0, neighbour, 0.0), "SAT は重なっていないと判定する"


def test_sat_detects_a_genuine_rotated_overlap() -> None:
    assert boxes.obb_overlap((0.0, 0.0, 100.0, 20.0), 45.0, (40.0, 40.0, 40.0, 40.0), 0.0)


def test_sat_is_symmetric() -> None:
    a, ra = (0.0, 0.0, 60.0, 30.0), 30.0
    b, rb = (20.0, 10.0, 60.0, 30.0), -15.0
    assert boxes.obb_overlap(a, ra, b, rb) == boxes.obb_overlap(b, rb, a, ra)


def test_obb_overlap_area_matches_axis_aligned_case() -> None:
    a = (0.0, 0.0, 10.0, 10.0)
    b = (5.0, 5.0, 10.0, 10.0)
    assert boxes.obb_overlap_area(a, 0.0, b, 0.0) == pytest.approx(25.0)


def test_obb_overlap_area_of_identical_rotated_boxes_is_its_area() -> None:
    box = (10.0, 10.0, 40.0, 20.0)
    assert boxes.obb_overlap_area(box, 37.0, box, 37.0) == pytest.approx(800.0, rel=1e-6)


def test_obb_overlap_area_is_zero_when_separated() -> None:
    assert boxes.obb_overlap_area(
        (0.0, 0.0, 10.0, 10.0), 45.0, (100.0, 100.0, 10.0, 10.0), 0.0
    ) == (pytest.approx(0.0))


def test_obb_contains() -> None:
    outer = (0.0, 0.0, 100.0, 100.0)
    assert boxes.obb_contains(outer, 0.0, (10.0, 10.0, 20.0, 20.0), 0.0)
    assert not boxes.obb_contains(outer, 0.0, (90.0, 90.0, 20.0, 20.0), 0.0)


# --------------------------------------------------------------------------
# covers（実形状で覆うか）
# --------------------------------------------------------------------------


def test_ellipse_does_not_cover_a_target_poking_into_its_corner() -> None:
    """bbox 判定なら「覆っている」と誤答する配置。"""
    ellipse = (0.0, 0.0, 100.0, 100.0)
    corner_target = (2.0, 2.0, 12.0, 12.0)
    assert boxes.obb_contains(ellipse, 0.0, corner_target, 0.0), "bbox には収まっている"
    assert not boxes.covers("ellipse", ellipse, 0.0, corner_target)


def test_ellipse_covers_a_concentric_small_box() -> None:
    assert boxes.covers("ellipse", (0.0, 0.0, 100.0, 100.0), 0.0, (45.0, 45.0, 10.0, 10.0))


def test_arealess_types_never_cover() -> None:
    big = (0.0, 0.0, 1000.0, 1000.0)
    small = (10.0, 10.0, 5.0, 5.0)
    for type_name in ("line", "arrow", "freehand", "connector"):
        assert not boxes.covers(type_name, big, 0.0, small)


def test_rect_covers_by_containment() -> None:
    assert boxes.covers("rect", (0.0, 0.0, 100.0, 100.0), 0.0, (10.0, 10.0, 10.0, 10.0))
    assert not boxes.covers("rect", (0.0, 0.0, 100.0, 100.0), 0.0, (95.0, 10.0, 10.0, 10.0))


# --------------------------------------------------------------------------
# 線分 × box
# --------------------------------------------------------------------------


def test_segment_through_box_intersects() -> None:
    assert boxes.segment_intersects_box((-10.0, 5.0), (20.0, 5.0), (0.0, 0.0, 10.0, 10.0))


def test_segment_missing_box_does_not_intersect() -> None:
    assert not boxes.segment_intersects_box((-10.0, 50.0), (20.0, 50.0), (0.0, 0.0, 10.0, 10.0))


def test_segment_grazing_the_edge_is_not_an_intersection() -> None:
    """辺をなぞるだけの経路まで交差にすると、箱に接して回る経路が全部弾かれる。"""
    assert not boxes.segment_intersects_box((-10.0, 0.0), (20.0, 0.0), (0.0, 0.0, 10.0, 10.0))


def test_segment_penetration_length() -> None:
    length = boxes.segment_box_penetration((-10.0, 5.0), (20.0, 5.0), (0.0, 0.0, 10.0, 10.0))
    assert length == pytest.approx(10.0)


def test_segment_fully_inside_box() -> None:
    assert boxes.segment_intersects_box((2.0, 2.0), (8.0, 8.0), (0.0, 0.0, 10.0, 10.0))


def test_point_in_ellipse() -> None:
    box = (0.0, 0.0, 100.0, 50.0)
    assert boxes.point_in_ellipse((50.0, 25.0), box)
    assert not boxes.point_in_ellipse((2.0, 2.0), box)


def test_point_in_obb_respects_rotation() -> None:
    box = (0.0, 0.0, 100.0, 20.0)
    # 無回転なら (5, 5) は内側、回転すると外れる位置。
    assert boxes.point_in_obb((5.0, 5.0), box, 0.0)
    assert not boxes.point_in_obb((5.0, 5.0), box, 90.0)
