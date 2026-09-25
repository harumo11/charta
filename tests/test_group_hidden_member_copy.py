"""グループに非表示メンバーがいる場合のコピー/貼付・複製の回帰テスト。

2026-09-25 レビュー2巡目 finding #5（area X）: `_strip_partial_group_ids` の
「全体か一部か」判定が `not o.locked` だけを見ており、`Document.
selectable_group_members`（not locked **かつ** visible）と食い違っていた。
Qt の `QGraphicsItem.setSelected` は非表示アイテムに対して何もしないため、
「選択できるメンバー全員」をコピー/複製しても非表示メンバーの分だけ足りず、
常に「一部だけの複製」と判定されてグループ化が外れる回帰が起きていた
（round-1 finding #7 が `selectable_group_members` を導入し 4 箇所を揃えたが、
`_strip_partial_group_ids` は取り残されていた）。

2026-09-25 追加決定（主セッション、Option A: PowerPoint 式）: 上記の
「全体か一部か」の判定を揃えるだけでは、非表示メンバー自身はコピー対象
（`copy_selection`/`duplicate_selection` が読む `scene.selected_objects()`）
に一度も入らないため、貼付/複製のたびに新しいグループから非表示メンバーが
1 個ずつ欠け続ける（グループが「可視メンバーだけの剛体」に痩せていく）。
`EditController.copy_selection`/`duplicate_selection` は `CanvasScene.
rigid_group_targets` で選択を非表示メンバーまで展開してからコピーするように
なったため、本ファイルの一部の期待値（貼付/複製されるオブジェクト数）を
「可視メンバーの数」から「非表示メンバーを含むグループ全体の数」へ更新した。

`test_group_member_edit.py` は他エージェントが並行編集中のため触らず、
同じフィクスチャ流儀（`window`・`_add_rect`・`_group`）を踏襲した別ファイルに置く。
"""

from __future__ import annotations

from typing import Any

import pytest
import shiboken6

from app.agent.api import AgentAPI
from app.commands.commands import AddObjectCommand, GroupCommand, SetPropertyCommand
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
    # fill 既定（DEFAULT_SHAPE_FILL）のまま=塗りあり=内部クリックで掴める。
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
    # モデル変更は QUndoCommand 経由（CLAUDE.md §13）。
    # `tests/test_group_hidden_member_selection.py` の `_hide` と同じ流儀。
    window.undo_stack.push(SetPropertyCommand(window.scene.document, obj, "visible", False, True))


def test_copy_paste_of_selectable_members_keeps_grouping_with_hidden_member(
    window: Any,
) -> None:
    """{A, C, 非表示 B} のグループで、見える A/C だけを選んでコピー/貼付すると、
    非表示 B も剛体の一部として一緒に複製され、3 個とも新しい共有 group_id を
    持つべき（要望10 追加決定 Option A。`rigid_group_targets` が選択を展開する
    ため、貼付されるのは選択した 2 個ではなく非表示メンバーを含む 3 個）。
    """
    scene = window.scene
    document = scene.document
    a = _add_rect(window, 0.0, 0.0)
    c = _add_rect(window, 100.0, 0.0)
    b = _add_rect(window, 200.0, 0.0)
    gid = _group(window, a, c, b)
    _hide(window, b)

    scene.clearSelection()
    scene.item_for(a).setSelected(True)  # 展開で選択できる全メンバー(A,C)が選ばれる
    assert {o.id for o in scene.selected_objects()} == {a.id, c.id}

    window.copy_selection()
    window.paste_clipboard()

    new_objs = document.objects[-3:]
    assert new_objs[0].group_id is not None
    assert new_objs[0].group_id == new_objs[1].group_id == new_objs[2].group_id
    assert new_objs[0].group_id != gid
    # 非表示メンバーの複製も一緒に作られ、非表示のまま（Option A: 剛体の一部）。
    hidden_copies = [o for o in new_objs if not o.visible]
    assert len(hidden_copies) == 1
    assert hidden_copies[0].x == b.x + 20.0  # _CLONE_OFFSET
    # 元のグループ（非表示メンバー込み）は無傷。
    assert a.group_id == gid
    assert c.group_id == gid
    assert b.group_id == gid


def test_duplicate_of_selectable_members_keeps_grouping_with_hidden_member(
    window: Any,
) -> None:
    """`copy_selection` と同じ理由（Option A）で、複製されるのも非表示 B を
    含む 3 個になる。
    """
    scene = window.scene
    document = scene.document
    a = _add_rect(window, 0.0, 0.0)
    c = _add_rect(window, 100.0, 0.0)
    b = _add_rect(window, 200.0, 0.0)
    gid = _group(window, a, c, b)
    _hide(window, b)

    scene.clearSelection()
    scene.item_for(a).setSelected(True)
    assert {o.id for o in scene.selected_objects()} == {a.id, c.id}

    window.duplicate_selection()

    new_objs = document.objects[-3:]
    assert new_objs[0].group_id is not None
    assert new_objs[0].group_id == new_objs[1].group_id == new_objs[2].group_id
    assert new_objs[0].group_id != gid
    hidden_copies = [o for o in new_objs if not o.visible]
    assert len(hidden_copies) == 1


def test_copy_of_single_visible_member_without_hidden_sibling_stays_ungrouped(
    window: Any,
) -> None:
    """対照実験: 非表示メンバーが絡まない通常の部分コピーは、従来どおり
    group_id が外れる（この fix が「常に group_id を維持する」に緩んでいないこと）。
    """
    scene = window.scene
    document = scene.document
    a = _add_rect(window, 0.0, 0.0)
    c = _add_rect(window, 100.0, 0.0)
    e = _add_rect(window, 200.0, 0.0)
    _group(window, a, c, e)  # 3 個とも可視

    scene.select_exactly([a])  # 1 個だけ選んでグループへ「入る」

    window.copy_selection()
    window.paste_clipboard()

    new_obj = document.objects[-1]
    assert new_obj.group_id is None


def test_agent_duplicate_with_explicit_hidden_id_keeps_grouping(window: Any) -> None:
    """finding #5 の付言: `AgentAPI.duplicate_objects` に非表示メンバーの id を
    明示的に含めて渡す経路は、そもそも copied_ids が selectable_ids の
    上位集合になるため今回の fix 以前から成立していたはずだが、それを
    テストで固定していなかった。ここで固定する。
    """
    scene = window.scene
    document = scene.document
    a = _add_rect(window, 0.0, 0.0)
    c = _add_rect(window, 100.0, 0.0)
    b = _add_rect(window, 200.0, 0.0)
    gid = _group(window, a, c, b)
    _hide(window, b)

    api = AgentAPI(window)
    result = api.duplicate_objects([a.id, c.id, b.id])

    created_ids = {item["id"] for item in result["created"]}
    assert len(created_ids) == 3
    new_group_ids = {document.object_by_id(i).group_id for i in created_ids}
    assert len(new_group_ids) == 1
    (new_gid,) = new_group_ids
    assert new_gid is not None and new_gid != gid
    # 非表示メンバーの複製も非表示のまま。
    hidden_copy_ids = [i for i in created_ids if document.object_by_id(i).x == b.x + 20.0]
    assert any(not document.object_by_id(i).visible for i in hidden_copy_ids)
