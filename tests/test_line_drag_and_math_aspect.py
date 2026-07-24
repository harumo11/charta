"""回帰テスト: 直線/矢印のライブ追従ドラッグ、数式(math)のアスペクト固定リサイズ・
再編集時の非歪み挙動（統合検証担当、offscreen）。

`conftest.py` の `qapp` フィクスチャ（offscreen QApplication）を再利用する。

- #1 直線/矢印のライブ追従: select ツールで press → move の時点(release 前)で
  `LineItem.live_geometry()`（`_p1`/`_p2`）がマウス移動量ぶんライブ移動していること
  （モデルはまだ変わらない）。release で確定し model の p1/p2 も一致する。
  box(rect) と混在選択でドラッグしても box は Qt 自身の移動・line はツール側の
  ライブ追従、両方とも release で 1 コマンド（マクロ）として確定する。
- #2 数式のアスペクト固定: `BoxHandleSet.begin_drag("br")` → `drag_to("br", ...)` で
  意図的にアスペクトを崩す点へドラッグしても、結果の width/height は自然アスペクト
  （`aspect_ratio()`）を保つ。`end_drag` で確定。
- #2 編集時非歪み: アスペクト固定で拡大済みの box に対し `commit_latex()` で式を
  差し替えても、新しい box は新式の自然アスペクトに一致し（歪まない）、拡大前との
  表示倍率（≈2倍）も維持される。拡大していないケースでも新式の自然アスペクトに
  一致する。`undo` で旧 latex・旧サイズに復元する（`commit_latex` は1マクロ）。
- paint 非クラッシュ（QImage へ render）。
"""

from __future__ import annotations

from typing import Any

import pytest
import shiboken6
from PySide6.QtCore import QPointF, Qt
from PySide6.QtGui import QImage, QPainter
from PySide6.QtWidgets import QStyleOptionGraphicsItem

from app.commands.commands import AddObjectCommand, SetGeometryCommand
from app.model.objects import LineObject, MathObject, RectObject
from app.scene.handles import BoxHandleSet
from app.scene.items.math_item import MathItem
from app.scene.items.shape_item import LineItem
from app.ui.main_window import MainWindow


class _FakeEvent:
    """`ToolManager.handle_mouse_*` に渡す最小限のマウスイベント代替(M1流用)。"""

    def __init__(self, button: Qt.MouseButton = Qt.MouseButton.LeftButton) -> None:
        self._button = button

    def button(self) -> Qt.MouseButton:
        return self._button


@pytest.fixture
def window(qapp: Any) -> Any:
    """既定ドキュメントの `MainWindow`。テスト終了時に安全に破棄する。"""
    w = MainWindow()
    w.resize(900, 700)
    w.show()
    yield w
    if shiboken6.isValid(w):
        w.close()


def _add_line(window: Any, p1: list[float], p2: list[float], obj_type: str) -> tuple[Any, Any]:
    scene = window.scene
    stack = window.undo_stack
    obj = LineObject(id=scene.document.new_id(), type=obj_type, p1=list(p1), p2=list(p2))
    stack.push(AddObjectCommand(scene.document, obj))
    item = scene.item_for(obj)
    assert isinstance(item, LineItem)
    return obj, item


# --------------------------------------------------------------------------
# #1 直線/矢印: 本体ドラッグのライブ追従(release前) + release で確定
# --------------------------------------------------------------------------


@pytest.mark.parametrize("obj_type", ["line", "arrow"])
def test_line_body_drag_live_follows_before_release_then_commits(
    window: Any, qapp: Any, obj_type: str
) -> None:
    scene = window.scene
    stack = window.undo_stack
    tm = window.tool_manager

    p1 = [100.0, 100.0]
    p2 = [200.0, 150.0]
    obj, item = _add_line(window, p1, p2, obj_type)
    scene.clearSelection()
    item.setSelected(True)
    qapp.processEvents()

    tm.set_tool("select")
    press_pos = QPointF((p1[0] + p2[0]) / 2.0, (p1[1] + p2[1]) / 2.0)  # 線の中点(本体上)
    dx, dy = 40.0, 30.0
    move_pos = QPointF(press_pos.x() + dx, press_pos.y() + dy)

    idx_before = stack.count()
    tm.handle_mouse_press(_FakeEvent(), press_pos)
    tm.handle_mouse_move(_FakeEvent(), move_pos)

    # release 前: item はライブ追従しているが、モデルはまだ変わらない。
    live = item.live_geometry()
    assert live["p1"] == pytest.approx([p1[0] + dx, p1[1] + dy])
    assert live["p2"] == pytest.approx([p2[0] + dx, p2[1] + dy])
    assert obj.p1 == p1
    assert obj.p2 == p2
    assert stack.count() == idx_before

    tm.handle_mouse_release(_FakeEvent(), move_pos)

    assert obj.p1 == pytest.approx([p1[0] + dx, p1[1] + dy])
    assert obj.p2 == pytest.approx([p2[0] + dx, p2[1] + dy])
    assert stack.count() == idx_before + 1

    stack.undo()
    assert obj.p1 == pytest.approx(p1)
    assert obj.p2 == pytest.approx(p2)


def test_mixed_selection_box_moves_via_qt_line_follows_via_tool_both_commit_on_release(
    window: Any, qapp: Any
) -> None:
    scene = window.scene
    stack = window.undo_stack
    tm = window.tool_manager

    rect = RectObject(id=scene.document.new_id(), x=300.0, y=300.0, width=100.0, height=80.0)
    stack.push(AddObjectCommand(scene.document, rect))
    rect_item = scene.item_for(rect)
    assert rect_item is not None

    p1 = [10.0, 10.0]
    p2 = [80.0, 60.0]
    line, line_item = _add_line(window, p1, p2, "line")

    scene.clearSelection()
    rect_item.setSelected(True)
    line_item.setSelected(True)
    qapp.processEvents()
    assert len(scene.selected_objects()) == 2

    tm.set_tool("select")
    press_pos = QPointF(350.0, 340.0)  # rect 本体上(line とは無関係な位置)
    dx, dy = 25.0, -15.0
    move_pos = QPointF(press_pos.x() + dx, press_pos.y() + dy)

    idx_before = stack.count()
    tm.handle_mouse_press(_FakeEvent(), press_pos)
    assert len(tm._select_start) == 2, "多重選択の両オブジェクトが移動アーム対象になる"

    # box は Qt 自身が ItemIsMovable でドラッグ中に pos を実際に動かす(この経路は
    # 実際のマウスイベント配送が無いテストでは模擬する。test_interaction.py 同様)。
    rect_item.setPos(rect.x + dx, rect.y + dy)
    tm.handle_mouse_move(_FakeEvent(), move_pos)

    # release 前: line はライブ追従、box(モデル)はまだ書き換わっていない。
    live_line = line_item.live_geometry()
    assert live_line["p1"] == pytest.approx([p1[0] + dx, p1[1] + dy])
    assert live_line["p2"] == pytest.approx([p2[0] + dx, p2[1] + dy])
    assert rect.x == 300.0 and rect.y == 300.0
    assert line.p1 == p1 and line.p2 == p2

    tm.handle_mouse_release(_FakeEvent(), move_pos)

    assert rect.x == pytest.approx(300.0 + dx)
    assert rect.y == pytest.approx(300.0 + dy)
    assert line.p1 == pytest.approx([p1[0] + dx, p1[1] + dy])
    assert line.p2 == pytest.approx([p2[0] + dx, p2[1] + dy])
    assert stack.count() == idx_before + 1, "box+line の同時ドラッグは1マクロとして確定する"

    stack.undo()
    assert rect.x == 300.0 and rect.y == 300.0
    assert line.p1 == p1 and line.p2 == p2


# --------------------------------------------------------------------------
# #2 数式: BoxHandleSet の br ドラッグは自然アスペクトを保つ
# --------------------------------------------------------------------------


def _add_math(window: Any, x: float, y: float, w: float, h: float, latex: str) -> tuple[Any, Any]:
    scene = window.scene
    stack = window.undo_stack
    obj = MathObject(id=scene.document.new_id(), x=x, y=y, width=w, height=h, latex=latex)
    stack.push(AddObjectCommand(scene.document, obj))
    item = scene.item_for(obj)
    assert isinstance(item, MathItem)
    return obj, item


def _push_geometry(stack: Any, scene: Any, obj: Any, width: float, height: float) -> None:
    """`obj` の width/height を差分適用する `SetGeometryCommand` を push するヘルパ。"""
    stack.push(
        SetGeometryCommand(
            scene.document,
            obj,
            {"width": width, "height": height},
            {"width": obj.width, "height": obj.height},
        )
    )


def test_math_box_handle_br_drag_preserves_natural_aspect_even_when_distorted(
    window: Any, qapp: Any
) -> None:
    stack = window.undo_stack

    obj, item = _add_math(window, x=50.0, y=50.0, w=100.0, h=100.0, latex="E = mc^2")
    aspect = item.aspect_ratio()
    assert aspect is not None and aspect > 0.0

    handles = BoxHandleSet(item)
    try:
        handles.begin_drag("br", item.mapToScene(QPointF(item._w, item._h)))
        # 意図的にアスペクトを崩す点(正方形)へドラッグする。
        distorted_local = QPointF(300.0, 300.0)
        handles.drag_to("br", item.mapToScene(distorted_local))

        live = item.live_geometry()
        assert live["width"] > 0.0 and live["height"] > 0.0
        assert live["width"] / live["height"] == pytest.approx(
            aspect, rel=1e-6
        ), "アスペクトロック対象のリサイズは、崩す方向にドラッグしても自然比を保つこと"

        idx_before = stack.count()
        handles.end_drag("br")
    finally:
        handles.destroy()

    assert stack.count() == idx_before + 1, "end_drag で SetGeometryCommand が1件確定すること"
    assert obj.width / obj.height == pytest.approx(aspect, rel=1e-6)


# --------------------------------------------------------------------------
# #2 編集時非歪み: commit_latex は新式の自然アスペクトに合わせ、表示倍率を維持する
# --------------------------------------------------------------------------


def test_math_commit_latex_after_aspect_scaled_resize_keeps_aspect_and_scale_undoable(
    window: Any, qapp: Any
) -> None:
    scene = window.scene
    stack = window.undo_stack

    latex_a = "E = mc^2"
    latex_b = r"\frac{a}{b}"

    obj, item = _add_math(window, x=40.0, y=40.0, w=10.0, h=10.0, latex=latex_a)
    natural_w_a, natural_h_a = item.default_size()

    # box を式Aの自然サイズ(=自然アスペクト)に合わせておく。
    _push_geometry(stack, scene, obj, natural_w_a, natural_h_a)
    aspect_a = item.aspect_ratio()
    assert aspect_a is not None
    assert obj.width / obj.height == pytest.approx(aspect_a, rel=1e-6)

    # BoxHandleSet の br ドラッグでアスペクトを保ったまま概ね2倍に拡大する。
    handles = BoxHandleSet(item)
    try:
        handles.begin_drag("br", item.mapToScene(QPointF(item._w, item._h)))
        handles.drag_to("br", item.mapToScene(QPointF(natural_w_a * 2.0, natural_h_a * 2.0)))
        handles.end_drag("br")
    finally:
        handles.destroy()

    assert obj.width == pytest.approx(natural_w_a * 2.0, rel=1e-3)
    assert obj.height == pytest.approx(natural_h_a * 2.0, rel=1e-3)
    scale_before_commit = obj.height / natural_h_a  # ≈2.0

    old_latex, old_w, old_h = obj.latex, obj.width, obj.height
    ok = item.commit_latex(latex_b)
    assert ok is True

    natural_w_b, natural_h_b = item.default_size()  # 差し替え後(latex_b)の自然サイズ
    aspect_b = natural_w_b / natural_h_b

    assert obj.width / obj.height == pytest.approx(
        aspect_b, rel=1e-6
    ), "編集後の box は新式の自然アスペクトに一致し、歪まないこと"
    assert obj.height / natural_h_b == pytest.approx(
        scale_before_commit, rel=1e-2
    ), "編集前の表示倍率(≈2倍)が維持されること"

    # commit_latex は1マクロ(latex + resize)のため、undo 1回で両方旧状態に戻る。
    stack.undo()
    assert obj.latex == old_latex
    assert obj.width == pytest.approx(old_w, rel=1e-6)
    assert obj.height == pytest.approx(old_h, rel=1e-6)
    stack.redo()
    assert obj.latex == latex_b


def test_math_commit_latex_without_prior_resize_matches_new_natural_aspect(
    window: Any, qapp: Any
) -> None:
    stack = window.undo_stack

    latex_a = "E = mc^2"
    latex_b = r"\sum_{i=1}^n x_i"

    obj, item = _add_math(window, x=10.0, y=10.0, w=10.0, h=10.0, latex=latex_a)
    natural_w_a, natural_h_a = item.default_size()
    _push_geometry(stack, window.scene, obj, natural_w_a, natural_h_a)

    ok = item.commit_latex(latex_b)
    assert ok is True

    natural_w_b, natural_h_b = item.default_size()
    aspect_b = natural_w_b / natural_h_b
    assert obj.width / obj.height == pytest.approx(aspect_b, rel=1e-6)
    assert obj.height == pytest.approx(
        natural_h_b, rel=1e-3
    ), "拡大していなければ倍率≈1倍で新式自然サイズに一致すること"


# --------------------------------------------------------------------------
# paint 非クラッシュ
# --------------------------------------------------------------------------


def test_math_item_paint_after_aspect_resize_does_not_crash(window: Any, qapp: Any) -> None:
    scene = window.scene
    obj, item = _add_math(window, x=0.0, y=0.0, w=100.0, h=100.0, latex="E = mc^2")

    handles = BoxHandleSet(item)
    try:
        handles.begin_drag("br", item.mapToScene(QPointF(item._w, item._h)))
        handles.drag_to("br", item.mapToScene(QPointF(300.0, 300.0)))
        handles.end_drag("br")
    finally:
        handles.destroy()

    image = QImage(300, 300, QImage.Format.Format_ARGB32)
    image.fill(0)
    painter = QPainter(image)
    try:
        item.paint(painter, QStyleOptionGraphicsItem())
    finally:
        painter.end()

    assert scene.item_for(obj) is item
