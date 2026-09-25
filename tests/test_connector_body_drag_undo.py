"""半接着コネクタの本体ドラッグが連続しても undo エントリを吸収しないことの
回帰テスト（2026-09-25 レビュー3巡目 finding #9）。

`ToolManager._select_release` は、動いたコマンドが1個だけ（`len(commands)
== 1`）のときマクロを介さずそのまま `undo_stack.push()` する。`source_point`/
`target_point` の `SetPropertyCommand` が既定の `mergeable=True` のままだと、
2回の別々の本体ドラッグが同一 (obj.id, key) への連続 push とみなされ
`mergeWith` で1エントリへ吸収されてしまう
（`tests/test_line_anchor_snap.py::test_two_consecutive_free_endpoint_drags_are_two_undo_entries`
がハンドルドラッグ側で既に固定している同じ穴が、connector の本体ドラッグ側に
残っていた）。
"""

from __future__ import annotations

from typing import Any

import pytest
import shiboken6
from PySide6.QtCore import QPointF, Qt

from app.commands.commands import AddObjectCommand
from app.model.objects import ConnectorObject, RectObject
from app.ui.main_window import MainWindow


@pytest.fixture
def window(qapp: Any) -> Any:
    w = MainWindow()
    w.resize(800, 600)
    w.show()
    yield w
    if shiboken6.isValid(w):
        w.close()


class _FakeEvent:
    def __init__(self, button: Qt.MouseButton = Qt.MouseButton.LeftButton) -> None:
        self._button = button

    def button(self) -> Qt.MouseButton:
        return self._button


def test_two_consecutive_half_bound_connector_body_drags_are_two_undo_entries(
    window: Any,
) -> None:
    scene = window.scene
    stack = window.undo_stack
    document = scene.document
    tm = window.tool_manager

    anchor = RectObject(id=document.new_id(), x=0.0, y=0.0, width=20.0, height=20.0, fill="#DDDDDD")
    stack.push(AddObjectCommand(document, anchor))
    conn = ConnectorObject(
        id=document.new_id(),
        source_id=anchor.id,
        source_anchor="right",
        target_point=[100.0, 10.0],
        routing="straight",
    )
    stack.push(AddObjectCommand(document, conn))

    scene.clearSelection()
    scene.item_for(conn).setSelected(True)
    scene.set_snap_enabled(False)
    tm.set_tool("select")

    idx_before = stack.index()

    # 1回目のドラッグ。
    press_pos = QPointF(60.0, 10.0)
    tm.handle_mouse_press(_FakeEvent(), press_pos)
    move_pos1 = QPointF(press_pos.x() + 10.0, press_pos.y())
    tm.handle_mouse_move(_FakeEvent(), move_pos1)
    tm.handle_mouse_release(_FakeEvent(), move_pos1)
    assert stack.index() == idx_before + 1
    after_first = list(conn.target_point)

    # 2回目の別のドラッグ(同じ target_point キーへの連続編集)。
    press_pos2 = QPointF(move_pos1.x(), move_pos1.y())
    tm.handle_mouse_press(_FakeEvent(), press_pos2)
    move_pos2 = QPointF(press_pos2.x() + 15.0, press_pos2.y())
    tm.handle_mouse_move(_FakeEvent(), move_pos2)
    tm.handle_mouse_release(_FakeEvent(), move_pos2)

    assert (
        stack.index() == idx_before + 2
    ), "2回の別々の本体ドラッグは2個の別々の undo エントリのはず(mergeで1個に潰れない)"

    stack.undo()
    assert conn.target_point == pytest.approx(after_first), "1回目のドラッグ後の位置まで戻るはず"
    stack.undo()
    assert conn.target_point == pytest.approx([100.0, 10.0])
