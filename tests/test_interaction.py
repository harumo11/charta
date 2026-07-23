"""M1 回帰テスト固定。

`scratchpad/adversarial_m1.py` が検出した回帰シナリオ（空白ドラッグ/本体ドラッグ/ハンドル/
Space パン/to_dict 深いコピー/undo 再同期/通常運転時の握りつぶし無し/teardown 安全性の8個）を
pytest 形式に移植したもの。加えて、arrow 選択時の PropertyPanel、from_dict 後の z 再正規化、
SetGeometryCommand の mergeable 挙動を検証するテストを追加する。
"""

from __future__ import annotations

import gc
from typing import Any

import pytest
import shiboken6
from PySide6.QtCore import QEvent, QPointF, Qt
from PySide6.QtGui import QKeyEvent
from PySide6.QtWidgets import QGraphicsView

from app.commands.commands import AddObjectCommand, SetGeometryCommand, SetPropertyCommand
from app.model.document import Document
from app.model.objects import LineObject, RectObject, new_object
from app.model.properties import PROPERTIES
from app.ui.main_window import MainWindow


class _FakeEvent:
    """`ToolManager.handle_mouse_*` に渡す最小限のマウスイベント代替。"""

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


@pytest.fixture
def rect_env(window: Any, qapp: Any) -> dict[str, Any]:
    """選択済み rect(100,100,120x90) を持つ環境を用意する。"""
    scene = window.scene
    stack = window.undo_stack
    tm = window.tool_manager

    rect = RectObject(
        id=scene.document.new_id(), x=100, y=100, width=120, height=90, fill="#FF0000"
    )
    stack.push(AddObjectCommand(scene, rect))
    item = scene.item_for(rect)
    assert item is not None
    scene.clearSelection()
    item.setSelected(True)
    qapp.processEvents()

    return {
        "window": window,
        "scene": scene,
        "stack": stack,
        "tm": tm,
        "item": item,
        "rect": rect,
        "app": qapp,
    }


# --------------------------------------------------------------------------
# シナリオ1: 選択中に空白部をドラッグ（ラバーバンド）しても移動しない
# --------------------------------------------------------------------------


def test_empty_area_drag_does_not_move(rect_env: dict[str, Any]) -> None:
    stack = rect_env["stack"]
    tm = rect_env["tm"]
    rect = rect_env["rect"]

    idx_before = stack.index()
    empty = QPointF(600, 500)  # rect(100..220, 100..190) の外
    tm.set_tool("select")
    tm.handle_mouse_press(_FakeEvent(), empty)
    tm.handle_mouse_move(_FakeEvent(), QPointF(640, 560))
    tm.handle_mouse_release(_FakeEvent(), QPointF(640, 560))

    assert tm.current_tool() == "select"
    assert stack.index() == idx_before
    assert rect.x == 100 and rect.y == 100


# --------------------------------------------------------------------------
# シナリオ2: 本体ドラッグ = ちょうど1コマンド。undo で model/item とも復元
# --------------------------------------------------------------------------


def test_body_drag_creates_single_command_and_undo_restores(rect_env: dict[str, Any]) -> None:
    stack = rect_env["stack"]
    tm = rect_env["tm"]
    item = rect_env["item"]
    rect = rect_env["rect"]

    idx_before = stack.index()
    tm.handle_mouse_press(_FakeEvent(), QPointF(150, 140))  # 本体内
    item.setPos(140, 160)  # ItemIsMovable が動かした状態を模擬（+40, +60）
    tm.handle_mouse_release(_FakeEvent(), QPointF(190, 200))

    assert stack.index() == idx_before + 1
    assert rect.x == 140 and rect.y == 160

    stack.undo()
    assert rect.x == 100 and rect.y == 100
    assert abs(item.pos().x() - 100) < 1e-6
    assert abs(item.pos().y() - 100) < 1e-6


# --------------------------------------------------------------------------
# シナリオ3: ハンドルを掴んだ場合は移動をアームしない
# --------------------------------------------------------------------------


def test_handle_drag_does_not_arm_move(rect_env: dict[str, Any]) -> None:
    scene = rect_env["scene"]
    stack = rect_env["stack"]
    tm = rect_env["tm"]
    item = rect_env["item"]
    app = rect_env["app"]

    scene.clearSelection()
    item.setSelected(True)
    app.processEvents()

    handles = [c for c in item.childItems() if getattr(c, "obj", None) is None]
    assert len(handles) > 0, "選択時にハンドル子アイテムが生成される"

    h = handles[0]
    hpos = h.mapToScene(h.boundingRect().center())
    top = tm._topmost_item_at(hpos)
    assert getattr(top, "obj", None) is None, "ハンドル位置の最前面アイテムは .obj を持たない"

    idx_before = stack.index()
    tm.handle_mouse_press(_FakeEvent(), hpos)
    assert len(tm._select_start) == 0, "ハンドル押下で移動がアームされない"
    tm.handle_mouse_release(_FakeEvent(), hpos)
    assert stack.index() == idx_before, "ハンドル押下離しで move コマンドが積まれない"

    scene.clearSelection()
    app.processEvents()
    remaining = [c for c in item.childItems() if getattr(c, "obj", None) is None]
    assert len(remaining) == 0, "非選択でハンドルが破棄される"


# --------------------------------------------------------------------------
# シナリオ4: Space 押下で ScrollHandDrag、離すと元の DragMode に戻る
# --------------------------------------------------------------------------


def test_space_pan_toggles_scroll_hand_drag(window: Any) -> None:
    view = window.view
    window.tool_manager.set_tool("select")
    before_mode = view.dragMode()

    kp = QKeyEvent(QEvent.Type.KeyPress, Qt.Key.Key_Space, Qt.KeyboardModifier.NoModifier)
    view.keyPressEvent(kp)
    assert view.dragMode() == QGraphicsView.DragMode.ScrollHandDrag

    kr = QKeyEvent(QEvent.Type.KeyRelease, Qt.Key.Key_Space, Qt.KeyboardModifier.NoModifier)
    view.keyReleaseEvent(kr)
    assert view.dragMode() == before_mode


# --------------------------------------------------------------------------
# シナリオ5: モデル to_dict() の深いコピー（共有参照汚染が無いこと）
# --------------------------------------------------------------------------


def test_to_dict_deep_copy_isolated() -> None:
    lo = LineObject(id=1000, p1=[1.0, 2.0], p2=[3.0, 4.0])
    d = lo.to_dict()
    d["p1"][0] = 999.0
    assert lo.p1[0] == 1.0, "to_dict 後に dict 側の list を変更しても元オブジェクトは不変"


# --------------------------------------------------------------------------
# シナリオ6: undo/redo でパネル再同期がクラッシュしない
# --------------------------------------------------------------------------


def test_undo_redo_resync_does_not_crash(rect_env: dict[str, Any]) -> None:
    scene = rect_env["scene"]
    stack = rect_env["stack"]
    item = rect_env["item"]
    rect = rect_env["rect"]
    app = rect_env["app"]

    scene.clearSelection()
    item.setSelected(True)
    app.processEvents()

    old_fill = rect.fill
    stack.push(SetPropertyCommand(scene, rect, "fill", "#00FF00", old_fill))
    assert rect.fill == "#00FF00"
    stack.undo()
    assert rect.fill == old_fill
    # indexChanged -> MainWindow._on_undo_index_changed -> パネル再同期。例外が飛べば失敗する。
    app.processEvents()


# --------------------------------------------------------------------------
# シナリオ7（適応版）: 通常運転（有効なパネル）でガードが誤って早期returnしない。
# 旧実装の `_safe_call` による例外握りつぶしは現行実装（shiboken6.isValid の事前ガード）
# に置き換わっているため、「有効時は必ずフォームが再構築される」ことを検証する。
# --------------------------------------------------------------------------


def test_normal_operation_panels_stay_in_sync(rect_env: dict[str, Any]) -> None:
    window = rect_env["window"]
    scene = rect_env["scene"]
    stack = rect_env["stack"]
    rect = rect_env["rect"]
    app = rect_env["app"]

    window.property_panel.on_selection_changed()
    assert window.property_panel._form.rowCount() == len(PROPERTIES["rect"])

    stack.push(SetPropertyCommand(scene, rect, "stroke_width", 5.0, rect.stroke_width))
    app.processEvents()
    assert window.property_panel._form.rowCount() == len(PROPERTIES["rect"])


# --------------------------------------------------------------------------
# シナリオ8: teardown（close/deleteLater/gc）で RuntimeError が出ない
# --------------------------------------------------------------------------


def test_teardown_does_not_raise_runtime_error(window: Any, qapp: Any) -> None:
    scene = window.scene
    property_panel = window.property_panel
    layer_panel = window.layer_panel

    window.close()
    window.deleteLater()
    qapp.processEvents()
    del scene, property_panel, layer_panel
    gc.collect()
    qapp.processEvents()  # 例外が飛べばテストが失敗する


# --------------------------------------------------------------------------
# 追加: arrow 選択時に PropertyPanel.on_selection_changed が KeyError にならない
# --------------------------------------------------------------------------


def test_property_panel_handles_arrow_selection_without_keyerror(
    rect_env: dict[str, Any],
) -> None:
    window = rect_env["window"]
    scene = rect_env["scene"]
    stack = rect_env["stack"]
    app = rect_env["app"]

    arrow = new_object("arrow", id=scene.document.new_id(), p1=[0.0, 0.0], p2=[50.0, 60.0])
    stack.push(AddObjectCommand(scene, arrow))
    item = scene.item_for(arrow)
    scene.clearSelection()
    item.setSelected(True)
    app.processEvents()

    window.property_panel.on_selection_changed()  # KeyError が出ないこと
    assert window.property_panel._form.rowCount() == len(PROPERTIES["arrow"])


# --------------------------------------------------------------------------
# 追加: from_dict 後は z がインデックスに再正規化される
# --------------------------------------------------------------------------


def test_document_from_dict_normalizes_z_to_index() -> None:
    doc = Document()
    a = RectObject(id=doc.new_id())
    b = RectObject(id=doc.new_id())
    doc.add_object(a)
    doc.add_object(b)

    d = doc.to_dict()
    # わざと壊れた z を書き込んでおいても from_dict 後は index に再正規化されること。
    d["objects"][0]["z"] = 99
    d["objects"][1]["z"] = 99

    restored = Document.from_dict(d)
    assert [o.z for o in restored.objects] == [0, 1]


# --------------------------------------------------------------------------
# 追加: SetGeometryCommand の mergeable=True/False 挙動
# --------------------------------------------------------------------------


def test_set_geometry_command_merge_behavior(rect_env: dict[str, Any]) -> None:
    scene = rect_env["scene"]
    stack = rect_env["stack"]
    rect = rect_env["rect"]

    idx_before = stack.index()
    stack.push(SetGeometryCommand(scene, rect, {"x": 105.0}, {"x": 100.0}, mergeable=True))
    stack.push(SetGeometryCommand(scene, rect, {"x": 110.0}, {"x": 105.0}, mergeable=True))
    stack.push(SetGeometryCommand(scene, rect, {"x": 120.0}, {"x": 110.0}, mergeable=True))
    assert stack.index() == idx_before + 1, "mergeable=True の連続編集は1エントリに統合される"
    assert rect.x == 120.0
    stack.undo()
    assert rect.x == 100.0, "統合されたコマンドの undo で最初の old 値に戻る"

    idx_before2 = stack.index()
    stack.push(SetGeometryCommand(scene, rect, {"y": 105.0}, {"y": 100.0}, mergeable=False))
    stack.push(SetGeometryCommand(scene, rect, {"y": 110.0}, {"y": 105.0}, mergeable=False))
    assert stack.index() == idx_before2 + 2, "mergeable=False の連続編集は統合されない"
    assert rect.y == 110.0
