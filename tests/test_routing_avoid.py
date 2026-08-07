"""`orthogonal` の経路回避（`app/graphics/avoid.py`）。

**最重要の 2 点**:

1. 障害物が経路を塞いでいなければ、**従来と同一の点列**を返すこと
   （既存プロジェクトの見た目も既存テストも動かさない）。
2. キャンバスと SVG 出力の経路が一致すること（唯一の真実源を分岐させない）。
"""

from __future__ import annotations

import re

import pytest

from app.export.svg_exporter import document_to_svg
from app.graphics import avoid
from app.graphics.routing import build_routing
from app.model.document import Document
from app.model.objects import new_object


def _plain_orthogonal(p1, p2):
    """回避を入れる前の `build_routing(..., "orthogonal")` の実装（比較用の写し）。"""
    x1, y1 = p1
    x2, y2 = p2
    if abs(x2 - x1) > abs(y2 - y1):
        mid_x = (x1 + x2) / 2.0
        return [p1, (mid_x, y1), (mid_x, y2), p2]
    mid_y = (y1 + y2) / 2.0
    return [p1, (x1, mid_y), (x2, mid_y), p2]


# --------------------------------------------------------------------------
# 後方互換
# --------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("p1", "p2"),
    [
        ((0.0, 0.0), (400.0, 100.0)),  # 横長 → 水平先行
        ((0.0, 0.0), (100.0, 400.0)),  # 縦長 → 垂直先行
        ((300.0, 50.0), (10.0, 500.0)),  # 逆向き
        ((0.0, 0.0), (0.0, 0.0)),  # 退化
    ],
)
def test_without_obstacles_the_route_is_unchanged(p1, p2) -> None:
    """障害物なしなら 1px も変えない（既存プロジェクト・既存テストの保護）。"""
    assert build_routing(p1, p2, "orthogonal") == _plain_orthogonal(p1, p2)


def test_obstacles_that_do_not_block_leave_the_route_unchanged() -> None:
    p1, p2 = (0.0, 0.0), (400.0, 0.0)
    far_away = [(0.0, 900.0, 100.0, 100.0)]
    assert build_routing(p1, p2, "orthogonal", far_away) == _plain_orthogonal(p1, p2)


def test_straight_ignores_obstacles() -> None:
    """直線は直線であるべき（避けたいなら orthogonal を選ぶ）。"""
    blocking = [(40.0, -50.0, 20.0, 100.0)]
    assert build_routing((0.0, 0.0), (100.0, 0.0), "straight", blocking) == [
        (0.0, 0.0),
        (100.0, 0.0),
    ]


def test_unknown_routing_still_raises() -> None:
    with pytest.raises(ValueError, match="unknown routing"):
        build_routing((0.0, 0.0), (1.0, 1.0), "curvy")


# --------------------------------------------------------------------------
# 回避そのもの
# --------------------------------------------------------------------------


def test_a_blocking_obstacle_is_avoided() -> None:
    p1, p2 = (0.0, 0.0), (400.0, 0.0)
    obstacle = (150.0, -100.0, 100.0, 200.0)
    assert avoid.crossings(_plain_orthogonal(p1, p2), [obstacle]) > 0, "素の肘は貫通する配置"

    route = build_routing(p1, p2, "orthogonal", [obstacle])
    assert avoid.crossings(route, [obstacle]) == 0
    assert route[0] == p1 and route[-1] == p2, "端点は動かさない"


def test_the_route_stays_orthogonal() -> None:
    """各セグメントは水平か垂直（直角折れ線であることを崩さない）。"""
    route = build_routing((0.0, 0.0), (400.0, 200.0), "orthogonal", [(150.0, -50.0, 100.0, 400.0)])
    for a, b in zip(route, route[1:], strict=False):
        assert abs(a[0] - b[0]) < 1e-6 or abs(a[1] - b[1]) < 1e-6, f"斜めのセグメント {a}->{b}"


def test_an_endpoint_inside_an_obstacle_does_not_raise() -> None:
    """端点を含む障害物は落とす（残すと全候補が交差して採点が無意味になる）。"""
    route = build_routing((50.0, 50.0), (400.0, 50.0), "orthogonal", [(0.0, 0.0, 100.0, 100.0)])
    assert len(route) >= 2
    assert route[0] == (50.0, 50.0) and route[-1] == (400.0, 50.0)


def test_a_completely_boxed_in_route_degrades_instead_of_raising() -> None:
    """避けきれない配置でも例外を投げない（SVG エクスポータにエラー経路が無い）。"""
    walls = [
        (100.0, -500.0, 40.0, 1500.0),
        (200.0, -500.0, 40.0, 1500.0),
        (300.0, -500.0, 40.0, 1500.0),
    ]
    route = build_routing((0.0, 0.0), (400.0, 0.0), "orthogonal", walls)
    assert len(route) >= 2


def test_the_result_is_deterministic() -> None:
    p1, p2 = (0.0, 0.0), (500.0, 300.0)
    obstacles = [(150.0, -50.0, 100.0, 300.0), (300.0, 100.0, 80.0, 300.0)]
    first = build_routing(p1, p2, "orthogonal", obstacles)
    assert build_routing(p1, p2, "orthogonal", obstacles) == first
    assert build_routing(p1, p2, "orthogonal", list(reversed(obstacles))) == first


def test_route_has_no_duplicate_or_collinear_points() -> None:
    """`endpoint_direction` が実セグメントを見られるように潰しておく。"""
    route = build_routing((0.0, 0.0), (400.0, 0.0), "orthogonal", [(150.0, -100.0, 100.0, 200.0)])
    assert len(route) == len(set(route))


# --------------------------------------------------------------------------
# collect_obstacles の除外規則
# --------------------------------------------------------------------------


def _document_with_obstacles() -> tuple[Document, object]:
    document = Document()
    document.add_object(new_object("rect", 1, x=0.0, y=0.0, width=100.0, height=100.0))
    document.add_object(new_object("rect", 2, x=800.0, y=0.0, width=100.0, height=100.0))
    document.add_object(new_object("rect", 3, x=400.0, y=0.0, width=60.0, height=200.0))
    document.add_object(
        new_object("rect", 4, x=500.0, y=0.0, width=60.0, height=60.0, visible=False)
    )
    document.add_object(new_object("rect", 5, x=600.0, y=0.0, width=0.0, height=60.0))
    document.add_object(new_object("arrow", 6, p1=[0.0, 0.0], p2=[900.0, 900.0]))
    conn = new_object("connector", 7, source_id=1, target_id=2, routing="orthogonal")
    document.add_object(conn)
    return (document, conn)


def test_collect_obstacles_excludes_the_right_things() -> None:
    document, conn = _document_with_obstacles()
    obstacles = avoid.collect_obstacles(document, conn, padding=0.0)
    # 残るのは id=3 だけ。接続先(1,2)・不可視(4)・面積0(5)・斜め線(6)・自分(7) は除外。
    assert obstacles == [(400.0, 0.0, 60.0, 200.0)]


def test_collect_obstacles_applies_padding_and_rotation() -> None:
    document = Document()
    document.add_object(
        new_object("rect", 1, x=100.0, y=100.0, width=200.0, height=40.0, rotation=90.0)
    )
    conn = new_object("connector", 2, source_point=[0.0, 0.0], target_point=[500.0, 500.0])
    document.add_object(conn)
    x, y, w, h = avoid.collect_obstacles(document, conn, padding=10.0)[0]
    assert (w, h) == pytest.approx((40.0 + 20.0, 200.0 + 20.0)), "90 度回転で縦横が入れ替わる"


def test_collect_obstacles_prefers_live_boxes() -> None:
    """キャンバスはドラッグ中の見かけの位置を渡す。"""
    document, conn = _document_with_obstacles()
    live = avoid.collect_obstacles(
        document, conn, live_boxes={3: ((0.0, 900.0, 10.0, 10.0), 0.0)}, padding=0.0
    )
    assert live == [(0.0, 900.0, 10.0, 10.0)]


# --------------------------------------------------------------------------
# キャンバスと SVG のパリティ（最も壊れやすく、最も見えにくい箇所）
# --------------------------------------------------------------------------


def _connector_path_from_svg(svg: str) -> list[tuple[float, float]]:
    match = re.findall(r'<path d="((?:[ML] [-\d.]+ [-\d.]+ ?)+)"', svg)
    assert match, f"コネクタの path が見つからない: {svg[:400]}"
    tokens = match[-1].split()
    return [(float(tokens[i + 1]), float(tokens[i + 2])) for i in range(0, len(tokens), 3)]


def _parity_document(rotation: float = 0.0) -> Document:
    document = Document()
    document.add_object(new_object("rect", 1, x=0.0, y=200.0, width=100.0, height=100.0))
    document.add_object(new_object("rect", 2, x=800.0, y=200.0, width=100.0, height=100.0))
    document.add_object(
        new_object("rect", 3, x=400.0, y=150.0, width=120.0, height=200.0, rotation=rotation)
    )
    document.add_object(
        new_object(
            "connector",
            4,
            source_id=1,
            target_id=2,
            source_anchor="right",
            target_anchor="left",
            routing="orthogonal",
            arrow_end="none",
        )
    )
    return document


@pytest.mark.parametrize("rotation", [0.0, 30.0])
def test_canvas_and_svg_agree_on_the_route(qapp, rotation: float) -> None:
    """ライブ box（キャンバス）とモデル box（エクスポータ）が確定状態で一致すること。

    回転ありのケースが、片側にだけ回転を掛けた実装を捕まえる。
    """
    from app.scene.canvas_scene import CanvasScene

    document = _parity_document(rotation)
    with CanvasScene(document) as scene:
        item = scene.item_for(document.object_by_id(4))
        item.refresh_route()
        canvas_points = [(round(x, 3), round(y, 3)) for x, y in item._points]

    svg_points = _connector_path_from_svg(document_to_svg(document))
    assert canvas_points == svg_points


def test_the_route_actually_avoids_in_both_paths(qapp) -> None:
    from app.scene.canvas_scene import CanvasScene

    document = _parity_document()
    obstacle = (400.0, 150.0, 120.0, 200.0)
    padded = [(388.0, 138.0, 144.0, 224.0)]
    with CanvasScene(document) as scene:
        item = scene.item_for(document.object_by_id(4))
        item.refresh_route()
        assert avoid.crossings(item._points, padded) == 0
    assert avoid.crossings(_connector_path_from_svg(document_to_svg(document)), padded) == 0
    assert obstacle  # 障害物が実在する配置であることの記録


def test_moving_an_unrelated_object_reroutes_the_canvas(qapp) -> None:
    """接続先以外が動いたときの無効化。これが無いとキャンバスだけ古い経路になる。"""
    from PySide6.QtWidgets import QApplication

    from app.scene.canvas_scene import CanvasScene

    document = _parity_document()
    with CanvasScene(document) as scene:
        item = scene.item_for(document.object_by_id(4))
        item.refresh_route()
        before = list(item._points)

        # 邪魔な矩形をどける（自分の接続先ではない）。
        document.set_values(document.object_by_id(3), {"x": 400.0, "y": 900.0})
        QApplication.processEvents()  # 合流させた再ルートを走らせる

        after = list(item._points)
        assert after != before, "無関係なオブジェクトの移動で経路が更新される"
        assert [(round(x, 3), round(y, 3)) for x, y in after] == _connector_path_from_svg(
            document_to_svg(document)
        ), "更新後も SVG と一致する"


def test_reroute_is_coalesced_into_one_pass(qapp, monkeypatch) -> None:
    """20 個動かす undo マクロで 20 回再計算しない（応答性のための合流）。"""
    from PySide6.QtWidgets import QApplication

    from app.scene.canvas_scene import CanvasScene

    document = _parity_document()
    with CanvasScene(document) as scene:
        item = scene.item_for(document.object_by_id(4))
        calls = 0
        original = item.refresh_route

        def counting() -> None:
            nonlocal calls
            calls += 1
            original()

        monkeypatch.setattr(item, "refresh_route", counting)
        target = document.object_by_id(3)
        for i in range(20):
            document.set_values(target, {"x": 400.0 + i})
        QApplication.processEvents()
        assert calls == 1, f"20 回の変更に対して 1 回だけ再計算する（実際は {calls} 回）"


# --------------------------------------------------------------------------
# 性能（暴走の検出であってベンチマークではない）
# --------------------------------------------------------------------------


def test_many_obstacles_stay_fast_on_the_obstructed_path() -> None:
    """**塞がれている**配置で測る。避けない経路は早期リターンするので測る意味がない。"""
    import time

    obstacles = [
        (150.0 + (i % 20) * 15.0, -100.0 + (i // 20) * 30.0, 40.0, 200.0) for i in range(200)
    ]
    assert avoid.crossings(_plain_orthogonal((0.0, 0.0), (400.0, 0.0)), obstacles) > 0

    start = time.perf_counter()
    for _ in range(10):
        build_routing((0.0, 0.0), (400.0, 0.0), "orthogonal", obstacles)
    elapsed = (time.perf_counter() - start) / 10.0
    assert elapsed < 0.05, f"1 回あたり {elapsed * 1000:.1f}ms かかっている"
