"""選択ハンドル上の右クリックが、ハンドルの持ち主ではなく重なった別オブジェクト
へ横取りされる回帰テスト（2026-09-25 レビュー4巡目 finding #1）。

**原因**: `MainWindow._topmost_object_at` は `topmost_item_at(...,
button=Qt.MouseButton.RightButton)`（レビュー3巡目 finding #1 で導入）を使って
いたが、`_HandleItem`（リサイズ/回転ハンドル、`app/scene/handles.py`）は
`setAcceptedMouseButtons(Qt.MouseButton.LeftButton)` のみで、`RightButton` を渡すと
ハンドル自体が `_accepts_button` のフィルタで除外されてしまう。その結果
`parentItem()` を辿ってオーナーへ戻る前に、ハンドルの下に重なった**別オブジェクト**
へフォールスルーし、右クリックメニューがそのオブジェクトに効いてしまっていた
（選択中のオブジェクトに削除等の操作をしたつもりが、隣接する別オブジェクトが
削除される）。

修正は `button=None`（`_accepts_button` は None のとき `NoButton` の装飾専用
item だけを除外し、ハンドルはヒット候補に残る）。
"""

from __future__ import annotations

from typing import Any

import pytest
import shiboken6
from PySide6.QtCore import QPoint
from PySide6.QtWidgets import QMenu

from app.commands.commands import AddObjectCommand
from app.model.objects import RectObject
from app.ui.main_window import MainWindow


@pytest.fixture
def window(qapp: Any) -> Any:
    w = MainWindow()
    w.resize(900, 700)
    w.show()
    yield w
    if shiboken6.isValid(w):
        w.close()


def _add_rect(window: Any, x: float, y: float, w: float = 200.0, h: float = 140.0) -> RectObject:
    # fill 既定(DEFAULT_SHAPE_FILL)のまま=塗りありなので内部クリックで掴める。
    scene = window.scene
    stack = window.undo_stack
    rect = RectObject(id=scene.document.new_id(), x=x, y=y, width=w, height=h)
    stack.push(AddObjectCommand(scene.document, rect))
    return rect


def _spy_menu(window: Any, monkeypatch: pytest.MonkeyPatch) -> list[QMenu]:
    captured: list[QMenu] = []
    original_build = window._build_canvas_context_menu

    def spy_build() -> QMenu:
        menu = original_build()
        monkeypatch.setattr(menu, "exec", lambda *args, **kwargs: None)
        captured.append(menu)
        return menu

    monkeypatch.setattr(window, "_build_canvas_context_menu", spy_build)
    return captured


def test_right_click_on_rotate_handle_keeps_owner_selected(
    window: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    """選択中Aの回転ハンドルに重なる位置にBを置き、そのハンドルを右クリックしても
    選択がAのままであること（Bへ切り替わらないこと）。"""
    w = window
    w.scene.set_snap_enabled(False)
    b = _add_rect(w, 200.0, 150.0)  # A の回転ハンドル（上端の少し上）の真下に来る位置
    a = _add_rect(w, 200.0, 300.0)

    w.scene.clearSelection()
    w.scene.item_for(a).setSelected(True)
    assert {o.id for o in w.scene.selected_objects()} == {a.id}

    rotate_handle = next(
        c for c in w.scene.item_for(a).childItems() if getattr(c, "role", None) == "rotate"
    )
    scene_pos = rotate_handle.scenePos()

    captured = _spy_menu(w, monkeypatch)
    w._show_canvas_context_menu(scene_pos, QPoint(0, 0))

    assert {o.id for o in w.scene.selected_objects()} == {
        a.id
    }, "回転ハンドル上の右クリックはハンドルの持ち主(A)を選択したままにすること"

    delete_action = next(x for x in captured[0].actions() if x.text() == "削除")
    delete_action.trigger()

    remaining = {o.id for o in w.scene.document.objects}
    assert remaining == {b.id}, "削除は選択中のA(ハンドルの持ち主)に効き、Bが残ること"


def test_right_click_on_resize_handle_outer_half_keeps_owner_selected(
    window: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    """低ズームでリサイズハンドルの外側半分が隣接オブジェクトへはみ出す配置でも、
    そのハンドルへの右クリックは持ち主を選択したままであること。"""
    w = window
    w.scene.set_snap_enabled(False)
    _add_rect(w, 300.0, 200.0)  # A の右に接する（低ズームで `mr` ハンドルの外側がBへ掛かる）
    a = _add_rect(w, 100.0, 200.0)

    w.view.resetTransform()
    w.view.scale(0.5, 0.5)

    w.scene.clearSelection()
    w.scene.item_for(a).setSelected(True)
    assert {o.id for o in w.scene.selected_objects()} == {a.id}

    mr_handle = next(
        c for c in w.scene.item_for(a).childItems() if getattr(c, "role", None) == "mr"
    )
    # ハンドルの中心からデバイスpxで数px外側(B側)へ寄せた点。
    vp_pt = w.view.mapFromScene(mr_handle.scenePos()) + QPoint(3, 0)
    scene_pos = w.view.mapToScene(vp_pt)

    _spy_menu(w, monkeypatch)
    w._show_canvas_context_menu(scene_pos, QPoint(0, 0))

    assert {o.id for o in w.scene.selected_objects()} == {
        a.id
    }, "リサイズハンドルの外側半分への右クリックも持ち主(A)を選択したままにすること"
