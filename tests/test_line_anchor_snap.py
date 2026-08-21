"""line の端点ハンドル吸着 + 作図中のアンカー表示・吸着統合テスト
（P4/P5契約 (B) 項目8「線の接着点表示 + 追従」B-3/B-4）。

`EndpointHandleSet`（`app.scene.handles`）が `ConnectorHandleSet` と同じ磁石スナップ・
アンカードット文法を共有すること（`tests/test_anchor_snap.py`/`test_type_anchors.py`
の文法を踏襲）、および line/arrow の作図ツール（`ToolManager._draw_*`）が作図中に
同じアンカーへ吸着し `AnchorHintOverlay` でヒントを表示することを検証する。

line は connector と異なり「図形の胴体でドロップしただけでは接続しない」
（表示されたアンカーへ磁石吸着したときだけ接続する、というユーザー決定の核心）。
すべて undo で復元できることを確認する（§13）。

`tests/conftest.py` の `qapp` フィクスチャを再利用する。ヘルパー
(`window`/`_add_rect`/`_add_line`/`_select_only`) は `tests/test_anchor_snap.py` と
同じ形の最小実装を本ファイル内に用意する（テストファイル間の import 結合を避ける
ため）。
"""

from __future__ import annotations

from typing import Any

import pytest
from PySide6.QtCore import QPointF, Qt

from app.commands.commands import AddObjectCommand, SetGeometryCommand, SetPropertyCommand
from app.model.objects import LineObject, RectObject
from app.scene.handles import _HANDLE_DEFAULT_PEN, _HANDLE_SNAP_COLOR
from app.ui.main_window import MainWindow


class _FakeEvent:
    """`ToolManager.handle_mouse_*` に渡す最小限のマウスイベント代替（`modifiers()` 付き）。"""

    def __init__(
        self,
        button: Qt.MouseButton = Qt.MouseButton.LeftButton,
        modifiers: Qt.KeyboardModifier = Qt.KeyboardModifier.NoModifier,
    ) -> None:
        self._button = button
        self._modifiers = modifiers

    def button(self) -> Qt.MouseButton:
        return self._button

    def modifiers(self) -> Qt.KeyboardModifier:
        return self._modifiers


@pytest.fixture
def window(qapp: Any) -> Any:
    import shiboken6

    w = MainWindow()
    yield w
    if shiboken6.isValid(w):
        w.close()


def _add_rect(window: Any, x: float, y: float, w: float = 100.0, h: float = 80.0) -> RectObject:
    scene = window.scene
    stack = window.undo_stack
    rect = RectObject(id=scene.document.new_id(), x=x, y=y, width=w, height=h)
    stack.push(AddObjectCommand(scene.document, rect))
    return rect


def _add_line(window: Any, p1: tuple[float, float], p2: tuple[float, float]) -> LineObject:
    scene = window.scene
    stack = window.undo_stack
    line = LineObject(id=scene.document.new_id(), type="line", p1=list(p1), p2=list(p2))
    stack.push(AddObjectCommand(scene.document, line))
    return line


def _select_only(window: Any, obj: Any) -> Any:
    scene = window.scene
    scene.clearSelection()
    item = scene.item_for(obj)
    item.setSelected(True)
    return item


# ==========================================================================
# B-3: EndpointHandleSet のアンカードット表示（`ConnectorHandleSet` と共有）
# ==========================================================================


def test_endpoint_anchor_dot_counts_box_line_and_unconnected(window: Any) -> None:
    scene = window.scene
    stack = window.undo_stack

    rect = _add_rect(window, 0.0, 0.0, 100.0, 80.0)
    other_line = _add_line(window, (300.0, 0.0), (400.0, 100.0))
    line = _add_line(window, (10.0, 10.0), (20.0, 20.0))

    stack.push(SetPropertyCommand(scene.document, line, "p1_id", rect.id, None))
    stack.push(SetPropertyCommand(scene.document, line, "p2_id", other_line.id, None))

    item = _select_only(window, line)
    handles = item._handles
    assert handles is not None

    p1_roles = {r for r in handles._anchor_dots if r.startswith("anchor:p1:")}
    p2_roles = {r for r in handles._anchor_dots if r.startswith("anchor:p2:")}
    assert len(p1_roles) == 10, "箱型: 9固定アンカー + 自動ドット1個"
    assert len(p2_roles) == 4, "直線: 3固定アンカー + 自動ドット1個"

    stack.push(SetPropertyCommand(scene.document, line, "p1_id", None, rect.id))
    stack.push(SetPropertyCommand(scene.document, line, "p2_id", None, other_line.id))
    assert len(handles._anchor_dots) == 0, "未接着端にはドットを出さない"


def test_anchor_dot_click_changes_anchor_with_one_undo(window: Any) -> None:
    scene = window.scene
    stack = window.undo_stack

    rect = _add_rect(window, 0.0, 0.0, 100.0, 80.0)
    line = _add_line(window, (10.0, 10.0), (20.0, 20.0))
    stack.push(SetPropertyCommand(scene.document, line, "p1_id", rect.id, None))

    item = _select_only(window, line)
    handles = item._handles

    idx_before = stack.index()
    handles.begin_drag("anchor:p1:top", QPointF(50.0, 0.0))
    assert stack.index() == idx_before + 1
    assert line.p1_anchor == "top"

    stack.undo()
    assert line.p1_anchor == "center"


def test_endpoint_handle_color_changes_while_snapped(window: Any) -> None:
    _add_rect(window, 0.0, 0.0, 100.0, 80.0)  # top アンカー = (50, 0)
    line = _add_line(window, (10.0, 700.0), (900.0, 900.0))
    item = _select_only(window, line)
    handles = item._handles
    p1_handle = handles._handles["p1"]

    assert p1_handle._pen_color == _HANDLE_DEFAULT_PEN

    handles.begin_drag("p1", QPointF(*line.p1))
    handles.drag_to("p1", QPointF(53.0, 4.0))  # top アンカー近傍 -> スナップ
    assert p1_handle._pen_color == _HANDLE_SNAP_COLOR

    handles.drag_to("p1", QPointF(1500.0, 1500.0))  # 遠くへ -> 非スナップに戻る
    assert p1_handle._pen_color == _HANDLE_DEFAULT_PEN

    handles.end_drag("p1")
    assert p1_handle._pen_color == _HANDLE_DEFAULT_PEN, "確定後は既定色に戻る"


# ==========================================================================
# B-3: 端点ハンドルの磁石スナップ（`ConnectorHandleSet`/`ConnectorItem` と同じ規則）
# ==========================================================================


def test_drag_within_threshold_snaps_endpoint_to_anchor_point(window: Any) -> None:
    stack = window.undo_stack

    rect_a = _add_rect(window, 0.0, 0.0, 100.0, 80.0)  # top アンカー = (50, 0)
    _add_rect(window, 500.0, 500.0, 100.0, 80.0)  # 無関係な rect(混入確認用)
    line = _add_line(window, (10.0, 700.0), (900.0, 900.0))
    item = _select_only(window, line)
    handles = item._handles

    idx_before = stack.index()
    handles.begin_drag("p1", QPointF(*line.p1))
    mouse_pos = QPointF(53.0, 4.0)  # top アンカー(50,0)から距離 5px(<12px閾値)
    handles.drag_to("p1", mouse_pos)

    assert item.live_geometry()["p1"] == pytest.approx([50.0, 0.0])
    assert item.live_geometry()["p1"] != pytest.approx([mouse_pos.x(), mouse_pos.y()])
    assert item._snap_target == (rect_a.id, "top")
    assert line.p1_id is None, "コミット前はモデルを書き換えない"

    handles.end_drag("p1")

    assert line.p1_id == rect_a.id
    assert line.p1_anchor == "top"
    assert stack.index() == idx_before + 1
    assert item._snap_target is None, "確定後はスナップ状態をリセットする"

    stack.undo()
    assert line.p1_id is None
    assert line.p1_anchor == "center"  # LineObject の既定値
    assert line.p1 == pytest.approx([10.0, 700.0])


def test_drag_outside_threshold_stays_free_and_unsnapped(window: Any) -> None:
    stack = window.undo_stack

    _add_rect(window, 0.0, 0.0, 100.0, 80.0)
    line = _add_line(window, (10.0, 700.0), (900.0, 900.0))
    item = _select_only(window, line)
    handles = item._handles

    idx_before = stack.index()
    handles.begin_drag("p1", QPointF(*line.p1))
    mouse_pos = QPointF(1500.0, 1500.0)  # どのアンカーからも遠い空白
    handles.drag_to("p1", mouse_pos)

    assert item.live_geometry()["p1"] == pytest.approx([mouse_pos.x(), mouse_pos.y()])
    assert item._snap_target is None

    handles.end_drag("p1")

    assert line.p1_id is None, "空白へのドロップは接着しない(切り離し/固定点のまま)"
    assert line.p1 == pytest.approx([mouse_pos.x(), mouse_pos.y()])
    assert stack.index() == idx_before + 1


def test_drag_connected_endpoint_outside_threshold_detaches_to_fixed_point(window: Any) -> None:
    """接続済み端点を空白へドラッグ: 閾値外なら切り離す(固定点化)。

    `test_drag_outside_threshold_stays_free_and_unsnapped` は未接着の端点から
    始めるため、この「切り離し」分岐（id が非None→None に変わるマクロ経路）を
    通らない。ここでは事前に接着済みの端点から始める。
    """
    scene = window.scene
    stack = window.undo_stack

    rect_a = _add_rect(window, 0.0, 0.0, 100.0, 80.0)
    rect_b = _add_rect(window, 300.0, 300.0, 100.0, 80.0)
    line = _add_line(window, (50.0, 0.0), (350.0, 300.0))
    stack.push(SetPropertyCommand(scene.document, line, "p1_id", rect_a.id, None))
    stack.push(SetPropertyCommand(scene.document, line, "p1_anchor", "nearest", "center"))
    stack.push(SetPropertyCommand(scene.document, line, "p2_id", rect_b.id, None))
    stack.push(SetPropertyCommand(scene.document, line, "p2_anchor", "nearest", "center"))
    item = _select_only(window, line)
    handles = item._handles

    idx_before = stack.index()
    handles.begin_drag("p2", QPointF(*item.live_geometry()["p2"]))
    mouse_pos = QPointF(1500.0, 1500.0)  # どのアンカーからも遠い空白
    handles.drag_to("p2", mouse_pos)
    assert item._snap_target is None

    handles.end_drag("p2")

    assert line.p2_id is None, "空白へのドロップは切り離す"
    assert line.p2 == pytest.approx([mouse_pos.x(), mouse_pos.y()]), "座標を焼く"
    assert stack.index() == idx_before + 1, "切り離しは1 undoエントリ"

    stack.undo()
    assert line.p2_id == rect_b.id, "undo で接着まで復旧する"


def test_click_without_drag_on_bound_endpoint_does_not_detach(window: Any) -> None:
    """接着済み端点のハンドルをドラッグなしでクリックしただけでは切り離さない。

    line は connector と違い「胴体でドロップしたら再接続」（`_hit_connectable_at`）
    を持たないため、`drag_to` が一度も呼ばれない(=マウスが動かないまま
    press→release だけが来た)場合に無条件で切り離し処理へ落ちると、接着済み
    端点がただのクリックで壊れてしまう。
    """
    scene = window.scene
    stack = window.undo_stack

    rect = _add_rect(window, 0.0, 0.0, 100.0, 80.0)  # top アンカー = (50, 0)
    line = _add_line(window, (10.0, 700.0), (900.0, 900.0))
    stack.push(SetPropertyCommand(scene.document, line, "p1_id", rect.id, None))
    stack.push(SetPropertyCommand(scene.document, line, "p1_anchor", "top", "center"))
    stack.push(SetPropertyCommand(scene.document, line, "p1", [50.0, 0.0], list(line.p1)))
    item = _select_only(window, line)
    handles = item._handles

    idx_before = stack.index()
    handles.begin_drag("p1", QPointF(*item.live_geometry()["p1"]))
    # `drag_to` を一度も呼ばずに即 `end_drag`(=クリックのみ)。
    handles.end_drag("p1")

    assert line.p1_id == rect.id, "クリックだけでは切り離さない"
    assert line.p1_anchor == "top"
    assert stack.index() == idx_before, "モデル変更なし=undoエントリを積まない"


def test_two_consecutive_free_endpoint_drags_are_two_undo_entries(window: Any) -> None:
    """自由端の連続ドラッグはそれぞれ独立した undo エントリのまま残ること。

    `SetPropertyCommand` は同一 (obj.id, key) への連続 push を `mergeWith` で
    1エントリに吸収するため、点移動だけの経路に単独の `SetPropertyCommand` を
    使うと2回のドラッグが1エントリに潰れてしまう
    （`LineItem.commit_endpoint_drop` が `SetGeometryCommand` を使う理由）。
    """
    stack = window.undo_stack
    line = _add_line(window, (10.0, 700.0), (900.0, 900.0))
    item = _select_only(window, line)
    handles = item._handles

    idx_before = stack.index()

    handles.begin_drag("p1", QPointF(*line.p1))
    handles.drag_to("p1", QPointF(20.0, 710.0))
    handles.end_drag("p1")
    assert stack.index() == idx_before + 1

    handles.begin_drag("p1", QPointF(*line.p1))
    handles.drag_to("p1", QPointF(30.0, 720.0))
    handles.end_drag("p1")
    assert stack.index() == idx_before + 2, "2回のドラッグは2エントリのまま(mergeされない)"

    stack.undo()
    assert line.p1 == pytest.approx([20.0, 710.0]), "1回目の位置まで戻る(2回分が1つに潰れていない)"
    stack.undo()
    assert line.p1 == pytest.approx([10.0, 700.0])


def test_line_can_bind_both_endpoints_to_the_same_object(window: Any) -> None:
    """line は両端を同じオブジェクトに接着できる（弦。connector の同一 id 拒否は
    持ち込まない、B-3の設計メモの固定）。
    """
    scene = window.scene
    stack = window.undo_stack

    rect = _add_rect(window, 0.0, 0.0, 100.0, 80.0)  # top=(50,0), right=(100,40)
    line = _add_line(window, (10.0, 700.0), (900.0, 900.0))
    item = _select_only(window, line)
    handles = item._handles

    handles.begin_drag("p1", QPointF(*line.p1))
    handles.drag_to("p1", QPointF(53.0, 4.0))  # top 近傍
    handles.end_drag("p1")
    handles.begin_drag("p2", QPointF(*item.live_geometry()["p2"]))
    handles.drag_to("p2", QPointF(97.0, 43.0))  # right 近傍(同じ rect)
    handles.end_drag("p2")

    assert line.p1_id == rect.id
    assert line.p2_id == rect.id
    assert line.p1_anchor == "top"
    assert line.p2_anchor == "right"

    # 接着先(rect)が動くと弦の両端が追従すること。
    old_geom = {"x": rect.x, "y": rect.y}
    new_geom = {"x": rect.x + 10.0, "y": rect.y + 5.0}
    stack.push(SetGeometryCommand(scene.document, rect, new_geom, old_geom))
    assert item.live_geometry()["p1"] == pytest.approx([60.0, 5.0])
    assert item.live_geometry()["p2"] == pytest.approx([110.0, 45.0])


def test_dragging_a_second_endpoint_into_a_cycle_does_not_bind(window: Any) -> None:
    """A.p2 を B へ接着済みのとき、B.p1 を A へドラッグしても循環は作らない。

    レビュー major所見: 普通のハンドルドラッグ2回（A→B, 続けて B→A）だけで
    line 同士の相互接着(循環)が作れてしまうと、画面(信号カスケードで不動点に
    収束)とモデルのみで解くリゾルバ(SVG・エージェント API。`_visiting` 打ち切りで
    生キャッシュにフォールバック)が構造的に食い違い続ける。
    `LineItem.commit_endpoint_drop` の `_would_create_binding_cycle` ガードで
    UI からの到達を防ぐ（B の p1 は接着せず drop 座標を焼く。line B に空の
    キャンバスしか無いので他に接着先が無く、非循環時と挙動を区別できる）。
    """
    stack = window.undo_stack

    line_a = _add_line(window, (0.0, 0.0), (100.0, 0.0))
    line_b = _add_line(window, (200.0, 0.0), (300.0, 0.0))

    item_a = _select_only(window, line_a)
    handles_a = item_a._handles
    handles_a.begin_drag("p2", QPointF(*line_a.p2))
    handles_a.drag_to("p2", QPointF(*line_b.p1))  # B の start(=p1) 近傍
    handles_a.end_drag("p2")
    assert line_a.p2_id == line_b.id, "非循環な line→line 接着は従来どおり成立する"

    item_b = _select_only(window, line_b)
    handles_b = item_b._handles
    handles_b.begin_drag("p1", QPointF(*line_b.p1))
    drop_pos = QPointF(*line_a.p1)  # A の start(=p1) 近傍(循環になる接着先)
    handles_b.drag_to("p1", drop_pos)
    handles_b.end_drag("p1")

    assert line_b.p1_id is None, "循環になる接着はせず、drop 座標を焼く"
    assert line_b.p1 == pytest.approx([drop_pos.x(), drop_pos.y()])
    assert line_a.p2_id == line_b.id, "既存の(非循環な)接着は壊れない"
    stack.undo()
    assert line_b.p1 == pytest.approx([200.0, 0.0])


def test_dropping_inside_rect_body_does_not_connect(window: Any) -> None:
    """図形の胴体でドロップしただけでは接続しない（ユーザー決定の核心）。

    connector と違い line は「胴体上でドロップしたら nearest で自動接続」を
    行わない。ここでは rect のどの9アンカーからも12px超の内部点を選ぶ。
    """
    stack = window.undo_stack

    _add_rect(window, 0.0, 0.0, 100.0, 80.0)  # tl/top/tr/left/center/right/bl/bottom/br
    line = _add_line(window, (10.0, 700.0), (900.0, 900.0))
    item = _select_only(window, line)
    handles = item._handles

    idx_before = stack.index()
    handles.begin_drag("p1", QPointF(*line.p1))
    drop_pos = QPointF(30.0, 20.0)  # 胴体内部・全アンカーから12px超
    handles.drag_to("p1", drop_pos)
    assert item._snap_target is None

    handles.end_drag("p1")

    assert line.p1_id is None, "胴体でのドロップは接続しない"
    assert line.p1 == pytest.approx([drop_pos.x(), drop_pos.y()])
    assert stack.index() == idx_before + 1


def test_drag_near_invisible_rect_anchor_does_not_snap(window: Any) -> None:
    scene = window.scene
    stack = window.undo_stack

    rect_a = _add_rect(window, 0.0, 0.0, 100.0, 80.0)  # top アンカー = (50, 0)
    stack.push(SetPropertyCommand(scene.document, rect_a, "visible", False, True))
    line = _add_line(window, (10.0, 700.0), (900.0, 900.0))
    item = _select_only(window, line)
    handles = item._handles

    idx_before = stack.index()
    handles.begin_drag("p1", QPointF(*line.p1))
    mouse_pos = QPointF(53.0, 4.0)  # top アンカー(50,0)から距離5px(<12px) だが非表示
    handles.drag_to("p1", mouse_pos)
    assert item._snap_target is None

    handles.end_drag("p1")

    assert line.p1_id is None, "非表示オブジェクトへは接着しない"
    assert stack.index() == idx_before + 1


def test_endpoint_drag_does_not_self_snap(window: Any) -> None:
    """自己吸着しない: シーン内に他の接続可能図形が無い状態で、自分自身の
    center/end アンカー近傍へドラッグしても吸着しないこと。
    """
    line = _add_line(window, (0.0, 0.0), (100.0, 0.0))
    item = _select_only(window, line)
    handles = item._handles

    handles.begin_drag("p1", QPointF(0.0, 0.0))
    handles.drag_to("p1", QPointF(51.0, 1.0))  # 自分自身の center=(50,0) 近傍
    assert item._snap_target is None, "自己吸着しない"

    handles.end_drag("p1")
    assert line.p1_id is None
    assert line.p1 == pytest.approx([51.0, 1.0])


def test_commit_with_vanished_snap_target_detaches_endpoint(window: Any) -> None:
    stack = window.undo_stack

    rect_a = _add_rect(window, 0.0, 0.0, 100.0, 80.0)  # top アンカー = (50, 0)
    line = _add_line(window, (10.0, 700.0), (900.0, 900.0))
    item = _select_only(window, line)
    handles = item._handles

    idx_before = stack.index()
    handles.begin_drag("p1", QPointF(*line.p1))
    mouse_pos = QPointF(53.0, 4.0)  # top アンカー近傍 -> 通常はスナップする
    handles.drag_to("p1", mouse_pos)
    assert item._snap_target == (rect_a.id, "top")

    # ドラッグ〜確定の間にスナップ先が消失したケースを模擬する。
    missing_id = rect_a.id + 9999
    item._snap_target = (missing_id, "top")

    handles.end_drag("p1")

    assert line.p1_id is None
    assert line.p1 == pytest.approx([50.0, 0.0])
    assert stack.index() == idx_before + 1

    stack.undo()
    assert line.p1_id is None
    assert line.p1 == pytest.approx([10.0, 700.0])


def test_shift_suppresses_snap_during_endpoint_handle_drag(window: Any) -> None:
    """Shift 中は吸着しない（(A)×(B) の優先順位。B-3 と B-4 で共通の規則）。"""
    _add_rect(window, 0.0, 0.0, 100.0, 80.0)  # top アンカー = (50, 0)
    line = _add_line(window, (10.0, 700.0), (900.0, 900.0))
    item = _select_only(window, line)
    handles = item._handles

    handles.begin_drag("p1", QPointF(*line.p1))
    handles.drag_to("p1", QPointF(53.0, 4.0), Qt.KeyboardModifier.ShiftModifier)
    assert item._snap_target is None, "Shift中は磁石吸着しない"

    handles.end_drag("p1")
    assert line.p1_id is None


def test_endpoint_follows_target_after_commit_via_handle_drag(window: Any) -> None:
    """確定後に接続先を動かして追従すること（`geometryChanged` 購読の生存確認）。"""
    scene = window.scene
    stack = window.undo_stack

    rect = _add_rect(window, 0.0, 0.0, 100.0, 80.0)  # top アンカー = (50, 0)
    line = _add_line(window, (10.0, 700.0), (900.0, 900.0))
    item = _select_only(window, line)
    handles = item._handles

    handles.begin_drag("p1", QPointF(*line.p1))
    handles.drag_to("p1", QPointF(53.0, 4.0))
    handles.end_drag("p1")
    assert line.p1_id == rect.id

    old_geom = {"x": rect.x, "y": rect.y}
    new_geom = {"x": rect.x + 20.0, "y": rect.y + 30.0}
    stack.push(SetGeometryCommand(scene.document, rect, new_geom, old_geom))

    assert item.live_geometry()["p1"] == pytest.approx([70.0, 30.0])


# ==========================================================================
# B-4: 作図中のアンカー表示・吸着（`ToolManager._draw_*`）
# ==========================================================================


def test_draw_press_snaps_p1_to_anchor_and_records_binding(window: Any) -> None:
    rect = _add_rect(window, 0.0, 0.0, 100.0, 80.0)  # top アンカー = (50, 0)
    tm = window.tool_manager

    tm.set_tool("line")
    tm.handle_mouse_press(_FakeEvent(), QPointF(53.0, 4.0))  # top アンカー近傍
    assert tm._draw_p1_bind == (rect.id, "top")
    assert tm._draw_start is not None
    assert (tm._draw_start.x(), tm._draw_start.y()) == pytest.approx((50.0, 0.0))

    tm.handle_mouse_move(_FakeEvent(), QPointF(500.0, 500.0))
    tm.handle_mouse_release(_FakeEvent(), QPointF(500.0, 500.0))

    obj = window.scene.document.objects[-1]
    assert isinstance(obj, LineObject)
    assert obj.p1_id == rect.id
    assert obj.p1_anchor == "top"
    assert obj.p1 == pytest.approx([50.0, 0.0])
    assert obj.p2_id is None


def test_draw_release_snaps_p2_to_anchor(window: Any) -> None:
    rect = _add_rect(window, 300.0, 300.0, 100.0, 80.0)  # top アンカー = (350, 300)
    tm = window.tool_manager

    tm.set_tool("line")
    tm.handle_mouse_press(_FakeEvent(), QPointF(10.0, 10.0))
    tm.handle_mouse_move(_FakeEvent(), QPointF(353.0, 304.0))  # top アンカー近傍
    tm.handle_mouse_release(_FakeEvent(), QPointF(353.0, 304.0))

    obj = window.scene.document.objects[-1]
    assert obj.p1_id is None
    assert obj.p2_id == rect.id
    assert obj.p2_anchor == "top"
    assert obj.p2 == pytest.approx([350.0, 300.0])


def test_draw_release_inside_rect_body_does_not_connect(window: Any) -> None:
    _add_rect(window, 0.0, 0.0, 100.0, 80.0)
    tm = window.tool_manager

    tm.set_tool("line")
    tm.handle_mouse_press(_FakeEvent(), QPointF(-50.0, -50.0))
    tm.handle_mouse_move(_FakeEvent(), QPointF(30.0, 20.0))  # 胴体内部、全アンカーから12px超
    tm.handle_mouse_release(_FakeEvent(), QPointF(30.0, 20.0))

    obj = window.scene.document.objects[-1]
    assert obj.p2_id is None
    assert obj.p2 == pytest.approx([30.0, 20.0])


def test_draw_with_anchor_binding_is_one_undo_entry(window: Any) -> None:
    rect = _add_rect(window, 0.0, 0.0, 100.0, 80.0)
    tm = window.tool_manager
    stack = window.undo_stack

    idx_before = stack.index()
    tm.set_tool("line")
    tm.handle_mouse_press(_FakeEvent(), QPointF(53.0, 4.0))
    tm.handle_mouse_move(_FakeEvent(), QPointF(500.0, 500.0))
    tm.handle_mouse_release(_FakeEvent(), QPointF(500.0, 500.0))

    assert stack.index() == idx_before + 1
    obj = window.scene.document.objects[-1]
    assert obj.p1_id == rect.id


def test_anchor_hint_overlay_disappears_after_release(window: Any) -> None:
    _add_rect(window, 0.0, 0.0, 100.0, 80.0)
    tm = window.tool_manager

    tm.set_tool("line")
    tm.handle_mouse_press(_FakeEvent(), QPointF(10.0, 10.0))
    tm.handle_mouse_move(_FakeEvent(), QPointF(53.0, 4.0))
    assert tm._draw_anchor_hint is not None

    tm.handle_mouse_release(_FakeEvent(), QPointF(53.0, 4.0))
    assert tm._draw_anchor_hint is None


def test_anchor_hint_overlay_ignores_mouse_buttons(window: Any) -> None:
    """マウスイベントを奪うと作図ドラッグ自体が壊れるため、両アイテムとも
    `NoButton` であること。
    """
    _add_rect(window, 0.0, 0.0, 100.0, 80.0)
    tm = window.tool_manager

    tm.set_tool("line")
    tm.handle_mouse_press(_FakeEvent(), QPointF(10.0, 10.0))
    tm.handle_mouse_move(_FakeEvent(), QPointF(53.0, 4.0))
    overlay = tm._draw_anchor_hint
    assert overlay is not None
    assert overlay.acceptedMouseButtons() == Qt.MouseButton.NoButton

    tm.handle_mouse_release(_FakeEvent(), QPointF(53.0, 4.0))


def test_shift_at_press_disables_p1_anchor_snap(window: Any) -> None:
    _add_rect(window, 0.0, 0.0, 100.0, 80.0)  # top アンカー = (50, 0)
    tm = window.tool_manager

    tm.set_tool("line")
    shift_event = _FakeEvent(modifiers=Qt.KeyboardModifier.ShiftModifier)
    tm.handle_mouse_press(shift_event, QPointF(53.0, 4.0))
    assert tm._draw_p1_bind is None

    tm.handle_mouse_move(shift_event, QPointF(500.0, 500.0))
    tm.handle_mouse_release(shift_event, QPointF(500.0, 500.0))

    obj = window.scene.document.objects[-1]
    assert obj.p1_id is None
    assert obj.p1 == pytest.approx([53.0, 4.0])


def test_shift_at_move_disables_p2_anchor_snap_and_hint(window: Any) -> None:
    _add_rect(window, 0.0, 0.0, 100.0, 80.0)  # top アンカー = (50, 0)
    tm = window.tool_manager

    tm.set_tool("line")
    tm.handle_mouse_press(_FakeEvent(), QPointF(10.0, 10.0))
    shift_event = _FakeEvent(modifiers=Qt.KeyboardModifier.ShiftModifier)
    tm.handle_mouse_move(shift_event, QPointF(53.0, 4.0))
    assert tm._draw_anchor_hint is None, "Shift中はヒントも出さない"

    tm.handle_mouse_release(shift_event, QPointF(53.0, 4.0))

    obj = window.scene.document.objects[-1]
    assert obj.p2_id is None, "Shift中は磁石吸着しない"


def test_freshly_drawn_line_follows_target_after_move(window: Any) -> None:
    """作図直後に接続先を動かすと追従する（`rebind_connectors` の連鎖）。"""
    rect = _add_rect(window, 0.0, 0.0, 100.0, 80.0)  # top アンカー = (50, 0)
    tm = window.tool_manager
    stack = window.undo_stack

    tm.set_tool("line")
    tm.handle_mouse_press(_FakeEvent(), QPointF(53.0, 4.0))
    tm.handle_mouse_move(_FakeEvent(), QPointF(500.0, 500.0))
    tm.handle_mouse_release(_FakeEvent(), QPointF(500.0, 500.0))

    line_obj = window.scene.document.objects[-1]
    assert line_obj.p1_id == rect.id

    old_geom = {"x": rect.x, "y": rect.y}
    new_geom = {"x": rect.x + 20.0, "y": rect.y + 30.0}
    stack.push(SetGeometryCommand(window.scene.document, rect, new_geom, old_geom))

    line_item = window.scene.item_for(line_obj)
    assert line_item.live_geometry()["p1"] == pytest.approx([70.0, 30.0])
