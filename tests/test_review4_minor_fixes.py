"""レビュー4巡目の minor 所見 3 件の回帰テスト（2026-09-25）。

1. エージェントの `duplicate_objects` の `created` は入力 `ids` の順（`created[i]` が
   `ids[i]` の複製）。複製を z 順に積む修正（3巡目）で戻り値まで z 順になっていた。
2. レイヤーパネルの表示/ロックのトグルは離散操作なので 1 回 1 undo。統合されると
   OFF→ON の 2 回が no-op として消え、次の Ctrl+Z が無関係な直前の操作を取り消す。
3. グループに「入った」状態で可視メンバーを全部選んだとき、パネルの X/Y は選択中の
   メンバーだけを動かす（ドラッグと同じ。非表示メンバーを連れて行かない）。
"""

from __future__ import annotations

from typing import Any

import pytest
import shiboken6
from PySide6.QtCore import Qt
from PySide6.QtTest import QTest

from app.commands.commands import AddObjectCommand, GroupCommand, SetPropertyCommand
from app.model.objects import RectObject, TextObject
from app.ui.main_window import MainWindow


@pytest.fixture
def window(qapp: Any) -> Any:
    w = MainWindow()
    w.resize(900, 700)
    w.show()
    yield w
    if shiboken6.isValid(w):
        w.close()


def _add(window: Any, obj: Any) -> Any:
    window.undo_stack.push(AddObjectCommand(window.scene.document, obj))
    return obj


def _layer_row_widget(window: Any, obj_id: int) -> Any:
    layer_panel = window.layer_panel
    for i in range(layer_panel._list.count()):
        item = layer_panel._list.item(i)
        if item.data(Qt.ItemDataRole.UserRole) == obj_id:
            return layer_panel._list.itemWidget(item)
    return None


def test_agent_duplicate_created_follows_input_id_order(window: Any) -> None:
    from app.agent.api import AgentAPI

    doc = window.scene.document
    rect = _add(window, RectObject(id=doc.new_id(), x=10, y=10, width=80, height=40))
    label = _add(window, TextObject(id=doc.new_id(), text="A", x=20, y=15, width=40, height=30))
    api = AgentAPI(window)
    # z 順は rect → label だが、入力順は label → rect。
    result = api.duplicate_objects(ids=[label.id, rect.id])
    assert result["ok"] is True
    types = [entry["type"] for entry in result["created"]]
    assert types == ["text", "rect"]
    # 複製そのものは z 順に積まれている（ラベルが矩形の上）。
    clones = [doc.object_by_id(entry["id"]) for entry in result["created"]]
    assert doc.index_of(clones[0]) > doc.index_of(clones[1])


@pytest.mark.parametrize("attr", ["_visible_cb", "_locked_cb"])
def test_layer_panel_toggle_twice_gives_two_undo_entries(window: Any, attr: str) -> None:
    doc = window.scene.document
    rect = _add(window, RectObject(id=doc.new_id(), x=10, y=10, width=80, height=40))
    window.layer_panel.refresh()
    before = window.undo_stack.index()
    button = getattr(_layer_row_widget(window, rect.id), attr)
    QTest.mouseClick(button, Qt.MouseButton.LeftButton)
    button = getattr(_layer_row_widget(window, rect.id), attr)
    QTest.mouseClick(button, Qt.MouseButton.LeftButton)
    assert window.undo_stack.index() == before + 2
    # 1 回戻すと 1 回目のトグル後の状態（統合されていれば rect の追加まで戻ってしまう）。
    window.undo_stack.undo()
    assert doc.object_by_id(rect.id) is not None
    key = "visible" if attr == "_visible_cb" else "locked"
    assert getattr(rect, key) is (key == "locked")


def test_entered_group_panel_x_moves_only_selected_members(window: Any) -> None:
    doc = window.scene.document
    a = _add(window, RectObject(id=doc.new_id(), x=0, y=0, width=50, height=50))
    b = _add(window, RectObject(id=doc.new_id(), x=100, y=0, width=50, height=50))
    hidden = _add(window, RectObject(id=doc.new_id(), x=200, y=0, width=50, height=50))
    group_id = doc.new_id()
    window.undo_stack.push(GroupCommand(doc, [a, b, hidden], group_id))
    window.undo_stack.push(SetPropertyCommand(doc, hidden, "visible", False, True))

    scene = window.scene
    scene.clearSelection()
    scene.set_entered_group(group_id)
    for obj in (a, b):
        scene.item_for(obj).setSelected(True)
    QTest.qWait(0)
    assert scene.entered_group_id() == group_id

    panel = window.property_panel
    spin = panel.field_widget_for("x")
    spin.setValue(spin.value() + 30.0)
    assert a.x == pytest.approx(30.0)
    assert b.x == pytest.approx(130.0)
    # 入っている間はドラッグと同じく非表示メンバーを連れて行かない。
    assert hidden.x == pytest.approx(200.0)
