"""削除・グループ化・z順操作の Option A 回帰テスト
（2026-09-25 レビュー3巡目 findings #5/#6/#10/#14）。

`EditController.copy_selection`/`duplicate_selection` は要望10 追加決定
Option A（PowerPoint 式: ロックされていない非表示メンバーもグループの剛体
構成要素）にもとづき `CanvasScene.rigid_group_targets` で選択を非表示メンバー
まで展開していたが、`delete_selected`/`group_selected`/`bring_to_front`/
`send_to_back`/`bring_forward`/`send_backward` はそれをしておらず、
`scene.selected_objects()`（非表示メンバーを絶対に含まない）を生のまま
中核 API へ渡していた。結果:

- 削除: 可視メンバー全員を削除すると、残った非表示メンバー1個が「単独
  メンバーのグループを作らない」既存の後始末で無言でグループ解除され、
  孤立した非表示オブジェクトとして残っていた。
- グループ化(Ctrl+G): 既存グループの外から別オブジェクトを足して再編成する
  と、非表示メンバーは新グループへ連れて行かれず古いグループの単独残留に
  なって解除されていた。
- z順操作: グループ全体を前面化/背面化しても非表示メンバーだけ古い z の
  まま取り残されていた。

`EditController._selected_rigid()`（`rigid_group_targets` 経由）に一本化した
ことで、この4系統がコピー/複製と同じ規則になることを固定する。
"""

from __future__ import annotations

from typing import Any

import pytest
import shiboken6

from app.commands.commands import (
    AddObjectCommand,
    GroupCommand,
    SetPropertyCommand,
    TranslateGroupCommand,
)
from app.model.geometry import translate_geom
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


# 選択は `scene.item_for(obj).setSelected(True)` を直接使う（グループの一員
# なら `CanvasScene._expand_group_selection` が可視メンバー全員へ自動展開
# する。展開そのものの経路は F 担当の管轄なので、ここでは展開後の
# `scene.selected_objects()` を消費する側の4系統だけを検証する）。


# --------------------------------------------------------------------------
# D1: 削除でグループ全体(非表示メンバー含む)が消え、1 undo で全員戻る
# --------------------------------------------------------------------------


def test_delete_whole_group_removes_hidden_member_too_with_one_undo(window: Any) -> None:
    scene = window.scene
    stack = window.undo_stack
    document = scene.document
    a = _add_rect(window, 0.0, 0.0)
    c = _add_rect(window, 100.0, 0.0)
    b = _add_rect(window, 200.0, 0.0)  # 非表示メンバー
    group_id = _group(window, a, c, b)
    _hide(window, b)

    scene.clearSelection()
    scene.item_for(a).setSelected(True)  # 可視メンバー全員へ自動展開
    assert {o.id for o in scene.selected_objects()} == {a.id, c.id}

    idx_before = stack.index()
    window._edit.delete_selected()

    assert document.object_by_id(a.id) is None
    assert document.object_by_id(c.id) is None
    assert document.object_by_id(b.id) is None, "非表示メンバーも一緒に削除されるはず(Option A)"
    assert stack.index() == idx_before + 1, "1 undo にまとまること"

    stack.undo()
    assert document.object_by_id(a.id) is not None
    assert document.object_by_id(c.id) is not None
    restored_b = document.object_by_id(b.id)
    assert restored_b is not None
    assert restored_b.visible is False
    assert restored_b.group_id == group_id, "元の group_id を保って復元されること"


def test_delete_inside_entered_group_removes_only_that_member(window: Any) -> None:
    """負例: グループへ「入って」いる間の削除は、そのメンバー1個だけを消す
    （非表示の兄弟の group_id はそのまま）。"""
    scene = window.scene
    document = scene.document
    a = _add_rect(window, 0.0, 0.0)
    c = _add_rect(window, 100.0, 0.0)
    b = _add_rect(window, 200.0, 0.0)
    _group(window, a, c, b)
    _hide(window, b)

    scene.select_exactly([a])  # 「2回目のクリック」相当: A に入る
    assert scene.entered_group_id() is not None
    assert {o.id for o in scene.selected_objects()} == {a.id}

    window._edit.delete_selected()

    assert document.object_by_id(a.id) is None
    remaining_c = document.object_by_id(c.id)
    remaining_b = document.object_by_id(b.id)
    assert remaining_c is not None and remaining_c.group_id is not None
    assert remaining_b is not None and remaining_b.group_id is not None


# --------------------------------------------------------------------------
# G1/G2: Ctrl+G(グループ化)でも非表示メンバーが道連れになる
# --------------------------------------------------------------------------


def test_group_selected_on_whole_group_keeps_hidden_member_grouped(window: Any) -> None:
    """既存グループ全体を選んで Ctrl+G しても(再グループ化)、非表示メンバーは
    単独残留してグループ解除されないこと。
    """
    scene = window.scene
    a = _add_rect(window, 0.0, 0.0)
    c = _add_rect(window, 100.0, 0.0)
    b = _add_rect(window, 200.0, 0.0)
    _group(window, a, c, b)
    _hide(window, b)

    scene.clearSelection()
    scene.item_for(a).setSelected(True)
    assert {o.id for o in scene.selected_objects()} == {a.id, c.id}

    window._edit.group_selected()

    new_group_id = a.group_id
    assert c.group_id == new_group_id
    assert b.group_id == new_group_id, "非表示メンバーも新しいグループへ連れて行かれるはず"
    assert b.visible is False


def test_group_selected_with_outside_object_moves_hidden_member_to_new_group(window: Any) -> None:
    scene = window.scene
    document = scene.document
    a = _add_rect(window, 0.0, 0.0)
    c = _add_rect(window, 100.0, 0.0)
    b = _add_rect(window, 200.0, 0.0)
    d = _add_rect(window, 400.0, 0.0)
    _group(window, a, c, b)
    _hide(window, b)

    scene.clearSelection()
    scene.item_for(a).setSelected(True)  # {a, c} へ展開
    scene.item_for(d).setSelected(True)  # d を追加

    window._edit.group_selected()

    new_group_id = a.group_id
    assert c.group_id == new_group_id
    assert d.group_id == new_group_id
    assert b.group_id == new_group_id, "非表示メンバーも新しいグループへ連れて行かれるはず"

    # 新グループをドラッグすると b も同じ差分だけ動くこと(Option A の一貫性)。
    targets = document.movable_group_members(new_group_id)
    entries = []
    for o in targets:
        old_geom, new_geom = translate_geom(o, 10.0, 0.0)
        entries.append((o, new_geom, old_geom))
    window.undo_stack.push(TranslateGroupCommand(document, entries, "テスト移動"))
    assert b.x == pytest.approx(210.0)


# --------------------------------------------------------------------------
# finding #6: z順操作(前面化/背面化/一つ前/一つ後ろ)も非表示メンバーを連れて行く
# --------------------------------------------------------------------------


def test_bring_to_front_on_whole_group_brings_hidden_member_too(window: Any) -> None:
    r_a = _add_rect(window, 0.0, 0.0)  # index 0
    r_b = _add_rect(window, 5.0, 5.0)  # index 1(非表示メンバー)
    r_c = _add_rect(window, 100.0, 0.0)  # index 2
    r_x = _add_rect(window, 400.0, 0.0)  # index 3(グループ外)
    _group(window, r_a, r_b, r_c)
    _hide(window, r_b)

    scene = window.scene
    document = scene.document
    scene.clearSelection()
    scene.item_for(r_a).setSelected(True)
    assert {o.id for o in scene.selected_objects()} == {r_a.id, r_c.id}

    idx_before = window.undo_stack.index()
    window._edit.bring_to_front()

    order = [o.id for o in document.objects]
    # r_x(グループ外)より前に出て、グループの3人はまとまって最後尾(最前面)に来る。
    assert order.index(r_x.id) < order.index(r_a.id)
    assert order.index(r_x.id) < order.index(r_b.id)
    assert order.index(r_x.id) < order.index(r_c.id)
    assert window.undo_stack.index() == idx_before + 1


def test_send_to_back_on_whole_group_sends_hidden_member_too(window: Any) -> None:
    r_x = _add_rect(window, 400.0, 0.0)  # index 0(グループ外)
    r_a = _add_rect(window, 0.0, 0.0)
    r_b = _add_rect(window, 5.0, 5.0)  # 非表示メンバー
    r_c = _add_rect(window, 100.0, 0.0)
    _group(window, r_a, r_b, r_c)
    _hide(window, r_b)

    scene = window.scene
    document = scene.document
    scene.clearSelection()
    scene.item_for(r_a).setSelected(True)

    window._edit.send_to_back()

    order = [o.id for o in document.objects]
    assert order.index(r_x.id) > order.index(r_a.id)
    assert order.index(r_x.id) > order.index(r_b.id)
    assert order.index(r_x.id) > order.index(r_c.id)


def test_zorder_ops_inside_entered_group_do_not_move_siblings(window: Any) -> None:
    """負例: グループへ「入って」いる間の z順操作は、選択中のメンバー単体だけに効く。"""
    r_a = _add_rect(window, 0.0, 0.0)
    r_b = _add_rect(window, 5.0, 5.0)
    r_c = _add_rect(window, 100.0, 0.0)
    _group(window, r_a, r_b, r_c)
    _hide(window, r_b)

    scene = window.scene
    document = scene.document
    scene.select_exactly([r_a])
    assert scene.entered_group_id() is not None

    window._edit.bring_to_front()

    order = [o.id for o in document.objects]
    assert order[-1] == r_a.id, "入っている間は選択中のメンバー単体だけが前面化される"
    assert order.index(r_b.id) < order.index(r_a.id), "非表示の兄弟は動かないはず"
