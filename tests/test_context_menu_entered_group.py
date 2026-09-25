"""グループに「入っている」間の右クリックコンテキストメニューの回帰テスト
（findings #7/#11、担当D）。

`MainWindow._show_canvas_context_menu` は「右クリック位置のオブジェクトが未選択
なら、それだけを選択してからメニューを出す」を `scene.clearSelection()` +
`item.setSelected(True)` で実装していた。`clearSelection()` は
`_expand_group_selection` を同期発火し、選択が空になったことで「入っている」
グループ状態を自動解除してしまう。その直後の `setSelected(True)` は「入って」
いない状態からの通常選択なので、グループ全体へ展開される。

結果として、グループに「入っている」間に別メンバーを右クリックすると、左クリック
（そのメンバーだけを選択、契約 §F-2）と違い、グループ全体が選択され「入っている」
状態も解除されてしまい、コンテキストメニューの「削除」「複製」がグループ全体に
効いてしまっていた。

`tests/test_context_menu_right_click.py`（所有者なし・busy ゲート回帰）や
`tests/test_group_member_edit.py`（F 担当）とはテスト対象が重なる部分もあるが、
ファイル所有規約（契約 §0-3）に従い、本ファイルは他担当のテストを import/流用
せず自己完結させる（`_real_click`/`_group`/`_add_rect` 等はこのファイル内で
再実装する）。
"""

from __future__ import annotations

from typing import Any

import pytest
import shiboken6
from PySide6.QtCore import QPoint, QPointF, Qt
from PySide6.QtTest import QTest
from PySide6.QtWidgets import QMenu

from app.commands.commands import AddObjectCommand, GroupCommand
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
    # fill 既定(P0契約: DEFAULT_SHAPE_FILL)のまま=塗りありなので内部クリックで掴める。
    scene = window.scene
    stack = window.undo_stack
    rect = RectObject(id=scene.document.new_id(), x=x, y=y, width=w, height=h)
    stack.push(AddObjectCommand(scene.document, rect))
    return rect


def _group(window: Any, *objs: Any) -> int:
    group_id = window.scene.document.new_id()
    window.undo_stack.push(GroupCommand(window.scene.document, list(objs), group_id))
    return group_id


def _view_pos(window: Any, x: float, y: float) -> QPoint:
    return window.view.mapFromScene(QPointF(x, y))


def _scene_pos(window: Any, x: float, y: float) -> QPointF:
    return QPointF(x, y)


def _real_click(window: Any, x: float, y: float) -> None:
    """実際の Qt イベントとして1クリック（press→release、同位置）を送る。

    `QTest.mouseDClick` は使わない（`tests/test_group_member_edit.py` と同じ理由:
    DblClick イベントだけを送るため、2回連続で呼んでも「2回目の素のクリック」の
    代用にならない）。
    """
    viewport = window.view.viewport()
    pos = _view_pos(window, x, y)
    QTest.mousePress(viewport, Qt.MouseButton.LeftButton, Qt.KeyboardModifier.NoModifier, pos)
    QTest.mouseRelease(viewport, Qt.MouseButton.LeftButton, Qt.KeyboardModifier.NoModifier, pos)


def _stub_menu_exec(window: Any, monkeypatch: pytest.MonkeyPatch) -> list[QMenu]:
    """`_build_canvas_context_menu` が返す実メニューを捕まえつつ `exec` を無効化する
    （`tests/test_context_menu_right_click.py` と同様、ブロックする `exec` を
    そのまま呼ばないようにする）。
    """
    captured: list[QMenu] = []
    original_build = window._build_canvas_context_menu

    def spy_build() -> QMenu:
        menu = original_build()
        monkeypatch.setattr(menu, "exec", lambda *args, **kwargs: None)
        captured.append(menu)
        return menu

    monkeypatch.setattr(window, "_build_canvas_context_menu", spy_build)
    return captured


def _trigger_action(menu: QMenu, text: str) -> None:
    for action in menu.actions():
        if action.text() == text:
            action.trigger()
            return
    raise AssertionError(f"action not found: {text!r} (has: {[a.text() for a in menu.actions()]})")


# --------------------------------------------------------------------------
# finding #7/#11: 入っているグループの別メンバーを右クリック
# --------------------------------------------------------------------------


def test_right_click_sibling_while_entered_selects_only_that_member(
    window: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    scene = window.scene
    r0 = _add_rect(window, 0.0, 0.0)
    r1 = _add_rect(window, 100.0, 0.0)
    r2 = _add_rect(window, 200.0, 0.0)
    group_id = _group(window, r0, r1, r2)
    window.tool_manager.set_tool("select")

    _real_click(window, 25.0, 25.0)  # 1回目: グループ全体
    _real_click(window, 25.0, 25.0)  # 2回目: r0 だけに入る
    assert {o.id for o in scene.selected_objects()} == {r0.id}
    assert scene.entered_group_id() == group_id

    captured = _stub_menu_exec(window, monkeypatch)
    window._show_canvas_context_menu(_scene_pos(window, 125.0, 25.0), QPoint(0, 0))

    assert {o.id for o in scene.selected_objects()} == {
        r1.id
    }, "右クリックも左クリックと同じくそのメンバーだけを選択すること（finding #7/#11）"
    assert scene.entered_group_id() == group_id, "入っている状態を保つこと"
    assert len(captured) == 1


def test_deleting_from_context_menu_after_right_click_on_sibling_removes_only_it(
    window: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    scene, stack = window.scene, window.undo_stack
    r0 = _add_rect(window, 0.0, 0.0)
    r1 = _add_rect(window, 100.0, 0.0)
    r2 = _add_rect(window, 200.0, 0.0)
    _group(window, r0, r1, r2)
    window.tool_manager.set_tool("select")

    _real_click(window, 25.0, 25.0)
    _real_click(window, 25.0, 25.0)  # r0 に入る

    captured = _stub_menu_exec(window, monkeypatch)
    # r2 を右クリック
    window._show_canvas_context_menu(_scene_pos(window, 225.0, 25.0), QPoint(0, 0))

    _trigger_action(captured[-1], "削除")

    remaining_ids = {o.id for o in scene.document.objects}
    assert remaining_ids == {r0.id, r1.id}, "右クリックしたメンバーだけが削除されること"
    stack.undo()
    assert {o.id for o in scene.document.objects} == {r0.id, r1.id, r2.id}


def test_duplicating_from_context_menu_after_right_click_on_sibling_duplicates_only_it(
    window: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    scene = window.scene
    r0 = _add_rect(window, 0.0, 0.0)
    r1 = _add_rect(window, 100.0, 0.0)
    r2 = _add_rect(window, 200.0, 0.0)
    _group(window, r0, r1, r2)
    window.tool_manager.set_tool("select")

    _real_click(window, 25.0, 25.0)
    _real_click(window, 25.0, 25.0)

    before_count = len(scene.document.objects)
    captured = _stub_menu_exec(window, monkeypatch)
    window._show_canvas_context_menu(_scene_pos(window, 225.0, 25.0), QPoint(0, 0))
    _trigger_action(captured[-1], "複製")

    assert len(scene.document.objects) == before_count + 1, "複製もそのメンバー1個だけのこと"


# --------------------------------------------------------------------------
# 制御ケース（壊してはいけない既存の挙動）
# --------------------------------------------------------------------------


def test_right_click_already_selected_member_keeps_entered_state(
    window: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    scene = window.scene
    r0 = _add_rect(window, 0.0, 0.0)
    r1 = _add_rect(window, 100.0, 0.0)
    group_id = _group(window, r0, r1)
    window.tool_manager.set_tool("select")

    _real_click(window, 25.0, 25.0)
    _real_click(window, 25.0, 25.0)
    assert {o.id for o in scene.selected_objects()} == {r0.id}

    _stub_menu_exec(window, monkeypatch)
    window._show_canvas_context_menu(_scene_pos(window, 25.0, 25.0), QPoint(0, 0))

    assert {o.id for o in scene.selected_objects()} == {r0.id}
    assert scene.entered_group_id() == group_id


def test_right_click_sibling_with_whole_group_selected_keeps_whole_selection(
    window: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    scene = window.scene
    r0 = _add_rect(window, 0.0, 0.0)
    r1 = _add_rect(window, 100.0, 0.0)
    _group(window, r0, r1)
    window.tool_manager.set_tool("select")

    _real_click(window, 25.0, 25.0)  # 1回目: グループ全体が選択されたまま
    assert {o.id for o in scene.selected_objects()} == {r0.id, r1.id}
    assert scene.entered_group_id() is None

    _stub_menu_exec(window, monkeypatch)
    window._show_canvas_context_menu(_scene_pos(window, 125.0, 25.0), QPoint(0, 0))

    assert {o.id for o in scene.selected_objects()} == {r0.id, r1.id}
    assert scene.entered_group_id() is None


def test_right_click_outside_object_while_entered_exits_group(
    window: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    scene = window.scene
    r0 = _add_rect(window, 0.0, 0.0)
    r1 = _add_rect(window, 100.0, 0.0)
    d = _add_rect(window, 400.0, 0.0)
    _group(window, r0, r1)
    window.tool_manager.set_tool("select")

    _real_click(window, 25.0, 25.0)
    _real_click(window, 25.0, 25.0)
    assert scene.entered_group_id() is not None

    _stub_menu_exec(window, monkeypatch)
    window._show_canvas_context_menu(_scene_pos(window, 425.0, 25.0), QPoint(0, 0))

    assert {o.id for o in scene.selected_objects()} == {d.id}
    assert scene.entered_group_id() is None
