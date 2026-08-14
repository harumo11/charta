"""curve オブジェクトの幾何演算・CurveItem描画・SVG出力のテスト(curve_contract.md §D)。

担当D（テスト）。1〜7は `app.graphics.curves`（担当A実装済み）への直叩きで、
本ファイル作成時点で即座に緑になる。8〜12は `CurveItem`（担当B、`curve_item.py`）
と `svg_exporter._curve_path_d`（担当B）に依存し、並行実装中のため未完成でも
モジュール収集を壊さないよう遅延 import にしてある
（`QT_QPA_PLATFORM=offscreen uv run pytest tests/test_curve.py -k "not item and
not svg and not shape and not bounding"` で 1〜7 のみを検証できる）。

契約が定める正しい挙動を主張するテストであり、実装に合わせて弱めていない。
"""

from __future__ import annotations

import math
import re
from typing import Any

import pytest
from PySide6.QtCore import QPointF, QRectF

from app.graphics import curves

# --------------------------------------------------------------------------
# 1〜7: 純ロジック(curves.py 直叩き。B/C の実装に非依存で即座に緑)
# --------------------------------------------------------------------------


def test_catmull_rom_tension_half_matches_classic_formula() -> None:
    """tension=0.5 の中央セグメント制御点が古典式 P1+(P2-P0)/6, P2-(P3-P1)/6 に一致する。"""
    points = [(0.0, 0.0), (10.0, 0.0), (10.0, 10.0), (0.0, 10.0)]
    segments = curves.catmull_rom_segments(points, closed=False, tension=0.5)
    p0, p1, p2, p3 = points
    c1, c2, end = segments[1]  # P1->P2 = points[1]->points[2] の中央セグメント
    expected_c1 = (p1[0] + (p2[0] - p0[0]) / 6.0, p1[1] + (p2[1] - p0[1]) / 6.0)
    expected_c2 = (p2[0] - (p3[0] - p1[0]) / 6.0, p2[1] - (p3[1] - p1[1]) / 6.0)
    assert c1 == pytest.approx(expected_c1)
    assert c2 == pytest.approx(expected_c2)
    assert end == pytest.approx(p2)


def test_catmull_rom_tension_zero_is_straight_line() -> None:
    """tension=0 では制御点が両端アンカーに一致し、各セグメントが直線になる。"""
    points = [(0.0, 0.0), (10.0, 5.0), (20.0, -5.0)]
    segments = curves.catmull_rom_segments(points, closed=False, tension=0.0)
    anchor_pairs = list(zip(points[:-1], points[1:], strict=True))
    for (p_start, p_end), (c1, c2, end) in zip(anchor_pairs, segments, strict=True):
        assert c1 == pytest.approx(p_start)
        assert c2 == pytest.approx(p_end)
        assert end == pytest.approx(p_end)


def test_catmull_rom_two_points_are_collinear() -> None:
    """点が2個のときは端点複製の結果、特別分岐なしに自然と直線(共線)になる。"""
    points = [(0.0, 0.0), (10.0, 20.0)]
    segments = curves.catmull_rom_segments(points, closed=False, tension=0.5)
    assert len(segments) == 1
    c1, c2, end = segments[0]

    def _is_on_line(pt: tuple[float, float]) -> bool:
        cross = (10.0 - 0.0) * (pt[1] - 0.0) - (20.0 - 0.0) * (pt[0] - 0.0)
        return abs(cross) < 1e-6

    assert _is_on_line(c1)
    assert _is_on_line(c2)
    assert end == pytest.approx((10.0, 20.0))


def test_curve_segments_closed_vs_open_segment_count() -> None:
    """閉曲線: セグメント数 n・最終セグメント終点が points[0]。開曲線: セグメント数 n-1。"""
    points = [[0.0, 0.0], [1.0, 0.0], [1.0, 1.0], [0.0, 1.0]]  # n=4
    open_result = curves.curve_segments(points, width=10.0, height=10.0, closed=False)
    closed_result = curves.curve_segments(points, width=10.0, height=10.0, closed=True)
    assert open_result is not None
    assert closed_result is not None
    _open_start, open_segments = open_result
    closed_start, closed_segments = closed_result
    assert len(open_segments) == 3  # n-1
    assert len(closed_segments) == 4  # n
    _c1, _c2, closed_end = closed_segments[-1]
    assert closed_end == pytest.approx(closed_start)


def test_normalize_points_matches_freehand_normalize() -> None:
    """curves.normalize_points と freehand_item.normalize_freehand_points が同一出力。"""
    from app.scene.items.freehand_item import normalize_freehand_points

    raw = [[10.0, 20.0], [30.0, 10.0], [50.0, 40.0], [20.0, 35.0]]
    assert curves.normalize_points(raw) == normalize_freehand_points(raw)

    # 退化ケース(一辺 1.0 未満)でも一致すること(0除算ガードの共有経路を確認)。
    degenerate = [[5.0, 5.0], [5.2, 5.0]]
    assert curves.normalize_points(degenerate) == normalize_freehand_points(degenerate)


@pytest.mark.parametrize("rotation", [0.0, 30.0, 90.0, -45.0])
def test_rebox_origin_preserves_scene_position_under_rotation(rotation: float) -> None:
    """rebox_origin: rotation ∈ {0, 30, 90, -45} でローカル点を旧/新変換で
    写した scene 座標が一致する。

    契約の式 S(p) = (x,y) + C + R(θ)(p-C) を rebox_origin と独立に自前実装し、
    「旧 bbox でのある点の scene 座標」と「新 bbox に再原点化した後、同じ点を
    新ローカル座標(旧ローカル - (bx,by))で写した scene 座標」が一致することを見る。
    """
    x, y, width, height = 100.0, 50.0, 40.0, 30.0
    local_point = (10.0, 5.0)

    def _to_scene(
        px: float, py: float, ox: float, oy: float, w: float, h: float
    ) -> tuple[float, float]:
        cx, cy = w / 2.0, h / 2.0
        vx, vy = px - cx, py - cy
        theta = math.radians(rotation)
        rx = vx * math.cos(theta) - vy * math.sin(theta)
        ry = vx * math.sin(theta) + vy * math.cos(theta)
        return (ox + cx + rx, oy + cy + ry)

    old_scene = _to_scene(local_point[0], local_point[1], x, y, width, height)

    bx, by, bw, bh = 3.0, -2.0, 50.0, 45.0
    new_x, new_y = curves.rebox_origin(x, y, width, height, rotation, bx, by, bw, bh)
    new_local_point = (local_point[0] - bx, local_point[1] - by)
    new_scene = _to_scene(new_local_point[0], new_local_point[1], new_x, new_y, bw, bh)

    assert new_scene == pytest.approx(old_scene, abs=1e-6)


def test_rebox_origin_identity_when_bbox_unchanged() -> None:
    """bx=by=0 かつ寸法不変なら恒等 (x, y) を返す。"""
    x, y = 12.0, 34.0
    new_x, new_y = curves.rebox_origin(x, y, 40.0, 30.0, 25.0, 0.0, 0.0, 40.0, 30.0)
    assert (new_x, new_y) == pytest.approx((x, y))


def test_nearest_point_on_curve_returns_correct_segment_index() -> None:
    """nearest_point_on_curve が既知曲線で正しいセグメント index を返す。

    Catmull-Rom は張力によらず必ずアンカー上を通る(B0=P1, B3=P2)ため、
    アンカー点そのものを target にすれば samples_per_segment の粗さに
    左右されない厳密な期待値(t=1.0・距離0)を作れる。target がちょうど
    2セグメントの境界(前セグメント終点=次セグメント始点)にあるときは、
    実装上「先に調べた方が勝つ(dist < best_dist の厳密不等号)」ため、
    より小さい index が返ることも合わせて固定する。
    """
    points = [[0.0, 0.0], [0.5, 0.0], [0.5, 1.0], [1.0, 1.0]]  # L字形、4アンカー
    result = curves.curve_segments(points, width=100.0, height=100.0, closed=False)
    assert result is not None
    start, segments = result
    assert len(segments) == 3

    # target=points[1](境界: segments[0]の終点かつsegments[1]の始点)は index=0 側が勝つ。
    index0, t0, point0 = curves.nearest_point_on_curve(start, segments, (50.0, 0.0))
    assert index0 == 0
    assert t0 == pytest.approx(1.0)
    assert point0 == pytest.approx((50.0, 0.0), abs=1e-6)

    # target=points[2](境界: segments[1]の終点かつsegments[2]の始点)は index=1 側が勝つ。
    index1, t1, point1 = curves.nearest_point_on_curve(start, segments, (50.0, 100.0))
    assert index1 == 1
    assert t1 == pytest.approx(1.0)
    assert point1 == pytest.approx((50.0, 100.0), abs=1e-6)


def test_nearest_point_on_curve_empty_segments_returns_start() -> None:
    """segments が空(2点未満由来)なら (0, 0.0, start) を返す。"""
    index, t, point = curves.nearest_point_on_curve((5.0, 5.0), [], (99.0, 99.0))
    assert (index, t, point) == (0, 0.0, (5.0, 5.0))


# --------------------------------------------------------------------------
# 8〜10: CurveItem(担当B、`app/scene/items/curve_item.py`)。遅延 import。
# --------------------------------------------------------------------------


def test_curve_item_paint_does_not_crash_and_scales_with_width(qapp: Any) -> None:
    """CurveItem: paint がクラッシュしない / width 2倍でパス bbox が約2倍になる。"""
    from PySide6.QtGui import QImage, QPainter

    from app.model.objects import CurveObject
    from app.scene.items.curve_item import CurveItem

    obj = CurveObject(
        id=1,
        x=0.0,
        y=0.0,
        width=50.0,
        height=50.0,
        points=[[0.0, 0.0], [0.5, 1.0], [1.0, 0.0]],
        closed=False,
        stroke_width=2.0,
    )
    item = CurveItem(obj)
    image = QImage(150, 150, QImage.Format.Format_ARGB32)
    image.fill(0)
    painter = QPainter(image)
    try:
        item.paint(painter, None, None)  # 例外が飛べば失敗
    finally:
        painter.end()

    path = item._build_local_path()
    assert path is not None
    base_width = path.boundingRect().width()

    obj.width = 100.0
    item.sync_from_model()
    scaled_path = item._build_local_path()
    assert scaled_path is not None
    scaled_width = scaled_path.boundingRect().width()

    ratio = scaled_width / base_width
    assert ratio == pytest.approx(2.0, rel=0.1)


def test_curve_item_bounding_rect_includes_overshoot(qapp: Any) -> None:
    """boundingRect がオーバーシュート込みのパス bbox を stroke_width/2 で膨らませたもの。"""
    from app.model.objects import CurveObject
    from app.scene.items.curve_item import CurveItem

    # 直角に折れる4点の閉曲線・強い張力は Catmull-Rom のオーバーシュートで
    # 単純な (0,0,w,h) の外へ出やすい。
    obj = CurveObject(
        id=1,
        x=0.0,
        y=0.0,
        width=50.0,
        height=50.0,
        points=[[0.0, 0.0], [1.0, 0.0], [1.0, 1.0], [0.0, 1.0]],
        closed=True,
        tension=1.0,
        stroke_width=4.0,
    )
    item = CurveItem(obj)
    path = item._build_local_path()
    assert path is not None
    path_bbox = path.boundingRect()

    plain_box = QRectF(0.0, 0.0, obj.width, obj.height)
    assert not plain_box.contains(path_bbox), (
        "前提: Catmull-Romのオーバーシュートが単純な(0,0,w,h)の外に出ていること"
        "(この前提が崩れるとテストの意味が無くなる)"
    )

    margin = obj.stroke_width / 2.0
    expected = path_bbox.adjusted(-margin, -margin, margin, margin)
    bounding = item.boundingRect()
    assert bounding.left() == pytest.approx(expected.left(), abs=0.5)
    assert bounding.top() == pytest.approx(expected.top(), abs=0.5)
    assert bounding.right() == pytest.approx(expected.right(), abs=0.5)
    assert bounding.bottom() == pytest.approx(expected.bottom(), abs=0.5)


def test_curve_item_shape_hit_test_fill_vs_stroke(qapp: Any) -> None:
    """shape: fill=None は曲線沿いのみヒット・fill ありは内部点もヒットする。"""
    from app.model.objects import CurveObject
    from app.scene.items.curve_item import CurveItem

    points = [[0.0, 0.0], [1.0, 0.0], [1.0, 1.0], [0.0, 1.0]]

    obj_open = CurveObject(
        id=1,
        x=0.0,
        y=0.0,
        width=100.0,
        height=100.0,
        points=points,
        closed=True,
        fill=None,
        stroke_width=2.0,
    )
    item_open = CurveItem(obj_open)
    shape_open = item_open.shape()
    center = QPointF(50.0, 50.0)  # 塗りなし閉曲線の内部(曲線からは離れた点)
    assert not shape_open.contains(center), "fill=None は曲線沿いのみ当たり判定に含むこと"

    obj_filled = CurveObject(
        id=2,
        x=0.0,
        y=0.0,
        width=100.0,
        height=100.0,
        points=points,
        closed=True,
        fill="#FF0000",
        stroke_width=2.0,
    )
    item_filled = CurveItem(obj_filled)
    shape_filled = item_filled.shape()
    assert shape_filled.contains(center), "fill ありは内部点も当たり判定に含むこと"


# --------------------------------------------------------------------------
# 11〜12: CurveItem <-> svg_exporter パリティ(担当B、`app/export/svg_exporter.py`)。
# --------------------------------------------------------------------------


def _round_numbers(s: str) -> list[float]:
    """文字列中の数値トークンをすべて float 化して丸めたリストにする(桁揃え比較用)。"""
    return [round(float(tok), 3) for tok in re.findall(r"-?\d+\.?\d*", s)]


@pytest.mark.parametrize("closed", [False, True])
@pytest.mark.parametrize("tension", [0.0, 0.5])
def test_svg_and_item_path_d_parity_open_closed_tension(
    qapp: Any, closed: bool, tension: float
) -> None:
    """qpainterpath_to_svg_path_d(item._build_local_path()) と
    svg_exporter._curve_path_d(obj) が数値一致すること(_fmt の3桁許容、open/closed
    × tension 0/0.5 の4組)。CurveItem と svg_exporter が同一の
    app.graphics.curves.curve_segments を通ることの回帰固定(画面とSVGの一致)。
    """
    from app.export.svg_exporter import _curve_path_d, qpainterpath_to_svg_path_d
    from app.model.objects import CurveObject
    from app.scene.items.curve_item import CurveItem

    obj = CurveObject(
        id=1,
        x=10.0,
        y=20.0,
        width=80.0,
        height=60.0,
        points=[[0.0, 0.0], [0.4, 1.0], [1.0, 0.3], [0.7, 0.9]],
        closed=closed,
        tension=tension,
    )
    item = CurveItem(obj)
    path = item._build_local_path()
    assert path is not None
    item_d = qpainterpath_to_svg_path_d(path)

    svg_d = _curve_path_d(obj)
    assert svg_d is not None

    assert _round_numbers(item_d) == pytest.approx(_round_numbers(svg_d), abs=1e-3)


def test_svg_curve_attributes_fill_rule_closed_dash(qapp: Any) -> None:
    """SVG 属性: fill-rule="evenodd" が必ず入る / closed で d が Z 終端 /
    fill=None で fill="none" / dash="dash" で stroke-dasharray が出ること。
    """
    from app.export.svg_exporter import _render_curve
    from app.model.objects import CurveObject

    points = [[0.0, 0.0], [1.0, 0.0], [1.0, 1.0], [0.0, 1.0]]

    open_obj = CurveObject(
        id=1, x=0.0, y=0.0, width=50.0, height=50.0, points=points, closed=False, fill=None
    )
    open_svg = _render_curve(open_obj)
    assert 'fill-rule="evenodd"' in open_svg
    assert 'fill="none"' in open_svg
    assert " Z" not in open_svg, "開いた曲線は Z で閉じないこと"

    closed_obj = CurveObject(
        id=2,
        x=0.0,
        y=0.0,
        width=50.0,
        height=50.0,
        points=points,
        closed=True,
        fill="#00FF00",
        dash="dash",
    )
    closed_svg = _render_curve(closed_obj)
    assert 'fill-rule="evenodd"' in closed_svg
    assert " Z" in closed_svg, "閉じた曲線の d は Z で終端すること"
    assert "stroke-dasharray" in closed_svg
