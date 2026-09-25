"""レビュー finding #7: 非表示メンバーを含むグループでも「全体が選択されている」
判定が壊れないことの回帰テスト（担当 X）。

`CanvasScene.select_exactly` / `ToolManager._select_press` / `PropertyPanel.
_whole_group_selection` / `CanvasView._handle_group_entry_key` の 4 箇所が使う
「そのグループの選択され得るメンバー」判定を `Document.selectable_group_members`
（not locked **かつ** visible）に揃えた。Qt は非表示アイテムを選択できないため、
`not locked` だけの判定では非表示メンバーを持つグループが永久に「全体選択」へ
到達できず、グループ内個別編集にも入れず、複数選択の X/Y フォームが絶対座標へ
崩壊していた（このファイルは D2 所有 `tests/test_panel_sections.py` 系や F 所有
`tests/test_group_member_edit.py` とは別ファイルとして新規追加する）。
"""

from __future__ import annotations

from typing import Any

import pytest
import shiboken6
from PySide6.QtCore import QPointF, Qt
from PySide6.QtTest import QTest

from app.commands.commands import AddObjectCommand, GroupCommand, SetPropertyCommand
from app.model.document import Document
from app.model.objects import RectObject
from app.ui.main_window import MainWindow


@pytest.fixture
def window(qapp: Any) -> Any:
    w = MainWindow()
    w.resize(800, 600)
    w.show()
    yield w
    if shiboken6.isValid(w):
        w.close()


def _add_rect(window: Any, x: float, y: float, w: float = 50.0, h: float = 50.0) -> RectObject:
    scene = window.scene
    stack = window.undo_stack
    rect = RectObject(id=scene.document.new_id(), x=x, y=y, width=w, height=h)
    stack.push(AddObjectCommand(scene.document, rect))
    return rect


def _group(window: Any, *objs: Any) -> int:
    group_id = window.scene.document.new_id()
    window.undo_stack.push(GroupCommand(window.scene.document, list(objs), group_id))
    return group_id


def _hide(window: Any, obj: Any) -> None:
    window.undo_stack.push(SetPropertyCommand(window.scene.document, obj, "visible", False, True))


def _real_click(window: Any, x: float, y: float) -> None:
    viewport = window.view.viewport()
    pos = window.view.mapFromScene(QPointF(x, y))
    QTest.mousePress(viewport, Qt.MouseButton.LeftButton, Qt.KeyboardModifier.NoModifier, pos)
    QTest.mouseRelease(viewport, Qt.MouseButton.LeftButton, Qt.KeyboardModifier.NoModifier, pos)


def test_document_helper_excludes_hidden_and_locked() -> None:
    document = Document()
    a = RectObject(id=1, x=0, y=0, width=10, height=10, group_id=9)
    b = RectObject(id=2, x=20, y=0, width=10, height=10, group_id=9, visible=False)
    c = RectObject(id=3, x=40, y=0, width=10, height=10, group_id=9, locked=True)
    d = RectObject(id=4, x=60, y=0, width=10, height=10, group_id=None)
    for obj in (a, b, c, d):
        document.add_object(obj)
    assert [o.id for o in document.selectable_group_members(9)] == [1]


def test_select_exactly_of_visible_members_enters_group_when_one_member_is_hidden(
    window: Any,
) -> None:
    """visible な A・B だけをちょうど選ぶと、非表示 C があってもグループへ入る
    （C は選択不可能な集合から最初から除かれているため、A・B が「全体」になる）。
    """
    scene = window.scene
    a = _add_rect(window, 0.0, 0.0)
    b = _add_rect(window, 100.0, 0.0)
    c = _add_rect(window, 200.0, 0.0)
    _group(window, a, b, c)
    _hide(window, c)

    scene.select_exactly([a, b])

    assert {o.id for o in scene.selected_objects()} == {a.id, b.id}


def test_property_panel_shows_group_xy_form_with_hidden_member(window: Any) -> None:
    """非表示メンバーを持つグループでも、可視メンバーちょうどを選べば複数選択の
    X/Y は「グループ全体の平行移動」フォームになる（width/height/rotation は
    出ない）。以前は `_whole_group_selection` が常に None を返し、絶対座標の
    通常複数選択フォームへ崩壊していた（finding #7 の症状そのもの）。
    """
    scene = window.scene
    a = _add_rect(window, 0.0, 0.0)
    b = _add_rect(window, 100.0, 0.0)
    c = _add_rect(window, 200.0, 0.0)
    _group(window, a, b, c)
    _hide(window, c)

    scene.select_exactly([a, b])
    keys = window.property_panel.keys_in_form()
    assert "width" not in keys, f"グループ全体フォームに落ちていない: {keys}"
    assert "x" in keys and "y" in keys


def test_second_real_click_enters_group_with_hidden_member(window: Any) -> None:
    """PowerPoint 式のもう一度クリックでの個別選択（要望10）が、非表示メンバーの
    存在で永久に不可能にならないことを実クリックで確認する。
    """
    scene = window.scene
    a = _add_rect(window, 0.0, 0.0)
    b = _add_rect(window, 100.0, 0.0)
    c = _add_rect(window, 200.0, 0.0)
    _group(window, a, b, c)
    _hide(window, c)
    window.tool_manager.set_tool("select")

    _real_click(window, 25.0, 25.0)
    assert {o.id for o in scene.selected_objects()} == {a.id, b.id}
    assert scene.entered_group_id() is None

    _real_click(window, 25.0, 25.0)
    assert {o.id for o in scene.selected_objects()} == {a.id}
    assert scene.entered_group_id() == a.group_id


def test_esc_returns_to_visible_whole_group_selection_with_hidden_member(window: Any) -> None:
    scene = window.scene
    a = _add_rect(window, 0.0, 0.0)
    b = _add_rect(window, 100.0, 0.0)
    c = _add_rect(window, 200.0, 0.0)
    _group(window, a, b, c)
    _hide(window, c)
    window.tool_manager.set_tool("select")

    _real_click(window, 25.0, 25.0)
    _real_click(window, 25.0, 25.0)
    assert scene.entered_group_id() == a.group_id

    QTest.keyClick(window.view, Qt.Key.Key_Escape)
    assert scene.entered_group_id() is None
    assert {o.id for o in scene.selected_objects()} == {a.id, b.id}
