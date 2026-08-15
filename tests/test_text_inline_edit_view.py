"""text のインプレース編集モードの scene/view 側テスト（inline_text_contract.md §D、担当B）。

`tests/test_curve_node_edit.py`（キー配送・外側クリック・busy ゲートの流儀）・
`tests/test_mask_edit_mode.py`（QGraphicsSceneMouseEvent 合成の流儀）に倣う。
本ファイルは `app/scene/canvas_scene.py` / `app/scene/canvas_view.py`（担当B）の
実装を対象にし、担当A（`TextItem`/`TextEditorItem`）・担当C（`tool_manager.py`/
`main_window.py`/`host.py`）の実装が前提だが、それらは直接 import しない
（`ToolManager`/`MainWindow` を使うテストのみ例外的に import する）。
"""

from __future__ import annotations

from typing import Any

import pytest
from PySide6.QtCore import QEvent, QPointF, Qt
from PySide6.QtGui import QContextMenuEvent, QKeyEvent, QMouseEvent, QTextCursor, QUndoStack
from PySide6.QtWidgets import QGraphicsItem, QGraphicsSceneMouseEvent

from app.commands.commands import AddObjectCommand, RemoveObjectCommand
from app.model.document import Document
from app.model.objects import TextObject
from app.scene.canvas_scene import CanvasScene
from app.scene.canvas_view import CanvasView

_MOVABLE = QGraphicsItem.GraphicsItemFlag.ItemIsMovable


# --------------------------------------------------------------------------
# フィクスチャ・ヘルパ
# --------------------------------------------------------------------------


def _scene_with_text(
    *,
    x: float = 100.0,
    y: float = 100.0,
    width: float = 120.0,
    height: float = 60.0,
    text: str = "hello",
) -> tuple[CanvasScene, QUndoStack, TextObject, Any]:
    """指定ジオメトリの text 1 個を持つ (scene, undo_stack, obj, item) を作る。"""
    doc = Document()
    obj = TextObject(id=doc.new_id(), x=x, y=y, width=width, height=height, text=text)
    scene = CanvasScene(doc)
    stack = QUndoStack()
    scene.set_undo_stack(stack)
    stack.push(AddObjectCommand(scene.document, obj))
    item = scene.item_for(obj)
    assert item is not None
    return scene, stack, obj, item


def _key_event(
    key: Any, modifiers: Qt.KeyboardModifier = Qt.KeyboardModifier.NoModifier, text: str = ""
) -> QKeyEvent:
    return QKeyEvent(QEvent.Type.KeyPress, key, modifiers, text)


def _shortcut_override_event(
    key: Any, modifiers: Qt.KeyboardModifier = Qt.KeyboardModifier.NoModifier, text: str = ""
) -> QKeyEvent:
    return QKeyEvent(QEvent.Type.ShortcutOverride, key, modifiers, text)


def _press_event_at(view: CanvasView, scene_x: float, scene_y: float) -> QMouseEvent:
    local = QPointF(view.mapFromScene(QPointF(scene_x, scene_y)))
    return QMouseEvent(
        QEvent.Type.MouseButtonPress,
        local,
        local,
        Qt.MouseButton.LeftButton,
        Qt.MouseButton.LeftButton,
        Qt.KeyboardModifier.NoModifier,
    )


# --------------------------------------------------------------------------
# 1. ダブルクリックでモード開始
# --------------------------------------------------------------------------


def test_double_click_enters_text_edit_mode(qapp: Any) -> None:
    """ダブルクリック → `active_text_edit_item` is item・`text_edit_mode_changed(True)`・
    ハンドル非表示・movable False（`test_curve_node_edit.py` の 1 と同型）。
    """
    scene, _stack, _obj, item = _scene_with_text()
    captured: list[bool] = []
    scene.text_edit_mode_changed.connect(captured.append)

    item.setSelected(True)
    assert item._handles is not None, "選択直後はハンドルが表示されていること"

    dbl_click = QGraphicsSceneMouseEvent(QEvent.Type.GraphicsSceneMouseDoubleClick)
    center_scene = item.mapToScene(QPointF(item._w / 2.0, item._h / 2.0))
    dbl_click.setScenePos(center_scene)
    dbl_click.setPos(item.mapFromScene(center_scene))
    dbl_click.setButton(Qt.MouseButton.LeftButton)
    dbl_click.setButtons(Qt.MouseButton.LeftButton)
    qapp.sendEvent(scene, dbl_click)

    assert scene.active_text_edit_item() is item
    assert captured == [True]
    assert item._handles is None, "テキスト編集モード中はハンドルが非表示になること"
    assert not (item.flags() & _MOVABLE), "テキスト編集モード中は移動禁止になること"

    item.cancel_text_edit()
    assert scene.active_text_edit_item() is None
    assert item.flags() & _MOVABLE, "終了後は移動可能に復元されること"
    scene.close()


# --------------------------------------------------------------------------
# 2. Esc キャンセル（view.keyPressEvent 経由）
# --------------------------------------------------------------------------


def test_escape_cancels_text_edit_without_model_change() -> None:
    scene, _stack, obj, item = _scene_with_text(text="original")
    view = CanvasView(scene)
    view.resize(400, 300)
    old_text = obj.text

    item.begin_text_edit()
    editor = item._editor
    assert editor is not None
    editor.setPlainText("changed but not committed")

    view.keyPressEvent(_key_event(Qt.Key.Key_Escape))

    assert obj.text == old_text, "キャンセルではモデルを変更しないこと"
    assert scene.active_text_edit_item() is None
    scene.close()


# --------------------------------------------------------------------------
# 3. Ctrl+Enter で確定
# --------------------------------------------------------------------------


def test_ctrl_enter_commits_text_edit() -> None:
    scene, stack, obj, item = _scene_with_text(text="before")
    view = CanvasView(scene)
    view.resize(400, 300)

    item.begin_text_edit()
    editor = item._editor
    assert editor is not None
    editor.setPlainText("after")

    count_before = stack.count()
    view.keyPressEvent(_key_event(Qt.Key.Key_Return, Qt.KeyboardModifier.ControlModifier))

    assert scene.active_text_edit_item() is None
    assert obj.text == "after"
    assert stack.count() == count_before + 1
    scene.close()


# --------------------------------------------------------------------------
# 4. Enter 単独 → 改行が入り、モードは継続
# --------------------------------------------------------------------------


def test_plain_enter_inserts_newline_and_stays_in_edit_mode() -> None:
    scene, _stack, obj, item = _scene_with_text(text="line1")
    view = CanvasView(scene)
    view.resize(400, 300)

    item.begin_text_edit()
    editor = item._editor
    assert editor is not None

    view.keyPressEvent(_key_event(Qt.Key.Key_Return))

    assert scene.active_text_edit_item() is item, "Enter 単独では確定しないこと"
    assert "\n" in editor.current_text(), "改行がエディタに入力されること"
    assert obj.text == "line1", "確定前なのでモデルは変わらないこと"

    item.cancel_text_edit()
    scene.close()


# --------------------------------------------------------------------------
# 5. Space はパンを起動せず、空白が入力される（回帰の要）
# --------------------------------------------------------------------------


def test_space_does_not_pan_and_inserts_space_during_text_edit() -> None:
    scene, _stack, _obj, item = _scene_with_text(text="ab")
    view = CanvasView(scene)
    view.resize(400, 300)

    item.begin_text_edit()
    editor = item._editor
    assert editor is not None

    view.keyPressEvent(_key_event(Qt.Key.Key_Space, text=" "))

    assert not view._space_panning, "編集中は Space がパンを起動しないこと"
    assert " " in editor.current_text(), "Space がエディタへ空白として入力されること"

    item.cancel_text_edit()
    scene.close()


# --------------------------------------------------------------------------
# 6. 外側クリックで確定
# --------------------------------------------------------------------------


def test_outside_click_commits_text_edit() -> None:
    scene, stack, obj, item = _scene_with_text(x=100.0, y=100.0, width=120.0, height=60.0)
    view = CanvasView(scene)
    view.resize(400, 300)

    item.begin_text_edit()
    editor = item._editor
    assert editor is not None
    editor.setPlainText("committed via outside click")

    inside_scene = item.mapToScene(QPointF(item._w * 0.5, item._h * 0.5))
    handled_inside = view._commit_text_edit_on_outside_press(
        _press_event_at(view, inside_scene.x(), inside_scene.y())
    )
    assert handled_inside is False
    assert scene.active_text_edit_item() is item

    count_before = stack.count()
    handled_outside = view._commit_text_edit_on_outside_press(_press_event_at(view, 700.0, 500.0))
    assert handled_outside is True
    assert scene.active_text_edit_item() is None
    assert obj.text == "committed via outside click"
    assert stack.count() == count_before + 1
    scene.close()


# --------------------------------------------------------------------------
# 7. 編集中の contextMenuEvent は抑止される
# --------------------------------------------------------------------------


def test_context_menu_suppressed_during_text_edit() -> None:
    scene, _stack, _obj, item = _scene_with_text()
    view = CanvasView(scene)
    view.resize(400, 300)

    item.begin_text_edit()
    captured: list[Any] = []
    view.context_menu_requested.connect(lambda *args: captured.append(args))

    local = view.mapFromScene(item.mapToScene(QPointF(item._w * 0.5, item._h * 0.5)))
    global_pos = view.mapToGlobal(local)
    event = QContextMenuEvent(QContextMenuEvent.Reason.Mouse, local, global_pos)
    view.contextMenuEvent(event)

    assert captured == [], "テキスト編集中は context_menu_requested を emit しないこと"

    item.cancel_text_edit()
    scene.close()


# --------------------------------------------------------------------------
# 8. ShortcutOverride ガード
# --------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("key", "modifiers", "text"),
    [
        (Qt.Key.Key_R, Qt.KeyboardModifier.NoModifier, "r"),
        (Qt.Key.Key_1, Qt.KeyboardModifier.NoModifier, "1"),
        (Qt.Key.Key_Space, Qt.KeyboardModifier.NoModifier, " "),
        (Qt.Key.Key_Delete, Qt.KeyboardModifier.NoModifier, ""),
        (Qt.Key.Key_Z, Qt.KeyboardModifier.ControlModifier, ""),
    ],
)
def test_shortcut_override_accepted_during_text_edit(
    key: Any, modifiers: Qt.KeyboardModifier, text: str
) -> None:
    scene, _stack, _obj, item = _scene_with_text()
    view = CanvasView(scene)
    view.resize(400, 300)

    item.begin_text_edit()
    event = _shortcut_override_event(key, modifiers, text)
    view.event(event)
    assert event.isAccepted(), f"編集中は {key!r} が accept されること"

    item.cancel_text_edit()
    scene.close()


def test_shortcut_override_not_accepted_when_not_editing() -> None:
    """既存の A-Z ガードの回帰: 編集中でなければ従来どおり素通しする。"""
    scene, _stack, _obj, _item = _scene_with_text()
    view = CanvasView(scene)
    view.resize(400, 300)

    event = _shortcut_override_event(Qt.Key.Key_R, Qt.KeyboardModifier.NoModifier, "r")
    view.event(event)
    assert not event.isAccepted(), "編集中でなければ ShortcutOverride を奪わないこと"
    scene.close()


# --------------------------------------------------------------------------
# 9. 編集中に対象が削除された場合の防御
# --------------------------------------------------------------------------


def test_removing_target_object_during_text_edit_clears_active_item() -> None:
    scene, stack, obj, item = _scene_with_text()
    item.begin_text_edit()
    assert scene.active_text_edit_item() is item

    stack.push(RemoveObjectCommand(scene.document, obj))

    assert scene.active_text_edit_item() is None
    scene.close()


def test_cancel_active_text_edit_emits_end_signal_even_if_cancel_raises() -> None:
    """`_cancel_active_text_edit` は `cancel_text_edit()` が例外を投げる失敗経路でも
    `text_edit_mode_changed(False)` を必ず emit する（`_cancel_active_node_edit` と同型。
    直接代入だとシグナルが飛ばずステータスバーの案内が残り続ける）。
    """
    scene, _stack, _obj, item = _scene_with_text()
    item.begin_text_edit()
    assert scene.active_text_edit_item() is item

    captured: list[bool] = []
    scene.text_edit_mode_changed.connect(captured.append)

    def _boom() -> None:
        raise RuntimeError("boom")

    item.cancel_text_edit = _boom  # type: ignore[method-assign]

    scene._cancel_active_text_edit()

    assert scene.active_text_edit_item() is None
    assert captured == [False], "cancel が例外を投げても終了シグナルが飛ぶこと"
    scene.close()


# --------------------------------------------------------------------------
# 10. ShortcutOverride: Ctrl 修飾のホワイトリスト外は奪わない
# --------------------------------------------------------------------------


def test_shortcut_override_does_not_capture_unlisted_ctrl_combo_during_text_edit() -> None:
    """Ctrl+S 等ホワイトリスト外の Ctrl 組み合わせは、X11 で `event.text()` が制御文字
    （非空）を持つ場合でも編集中に奪ってはならない（`printable` 判定は無修飾のみ対象）。
    """
    scene, _stack, _obj, item = _scene_with_text()
    view = CanvasView(scene)
    view.resize(400, 300)

    item.begin_text_edit()
    # 実機の X11 では Ctrl+S の event.text() が "\x13" のような制御文字になりうる
    # ことを模して非空 text を明示的に与える。
    event = _shortcut_override_event(Qt.Key.Key_S, Qt.KeyboardModifier.ControlModifier, "\x13")
    view.event(event)
    assert not event.isAccepted(), "ホワイトリスト外の Ctrl 組み合わせは奪わないこと"

    item.cancel_text_edit()
    scene.close()


# --------------------------------------------------------------------------
# 11. review2 所見1: 箱の余白クリックでフォーカスが失われてもエディタへ戻る
# --------------------------------------------------------------------------


def test_margin_click_restores_editor_focus_and_moves_caret(qapp: Any) -> None:
    """`TextEditorItem` の bounding rect はテキストブロック高さ分しかないため、
    箱の残り領域（余白）は `TextItem`（`ItemIsFocusable` を持たない）が受ける。
    Qt はフォーカス不能アイテムへの press 配送前にシーンのフォーカスをクリアする
    ため、対策が無いと `scene.focusItem()` が None になり以後のキー入力が
    どこにも届かなくなる（review2 所見1）。`clearFocus()` で欠落状態を再現し、
    余白への press で `TextItem.mousePressEvent` がエディタへフォーカスを
    戻すこと、およびクリック位置に応じてキャレットが移動することを固定する。

    `clearFocus()` は `scene.hasFocus()` が True（＝scene が view 経由で widget
    フォーカスを持っている）でなければ `scene.focusItem()` の「保留中のフォーカス
    先」記録を消さない（offscreen platform で実測確認済み）。`activateWindow()`
    のような実ウィンドウのアクティブ化までは不要だが、`view.show()` +
    `processEvents()` で scene に widget フォーカスを持たせる必要はある。
    """
    scene, _stack, _obj, item = _scene_with_text(
        text="line one\nline two", width=200.0, height=200.0
    )
    view = CanvasView(scene)
    view.resize(400, 300)
    view.show()
    qapp.processEvents()

    item.begin_text_edit()
    editor = item._editor
    assert editor is not None
    cursor = editor.textCursor()
    cursor.movePosition(QTextCursor.MoveOperation.Start)
    editor.setTextCursor(cursor)
    editor.clearFocus()
    assert scene.focusItem() is None, "前提: フォーカスが失われた状態を再現する"

    # エディタの実高さ（数行分）より下、箱内の余白をクリックする。
    margin_scene_pos = item.mapToScene(QPointF(item._w * 0.5, item._h * 0.9))
    press = QGraphicsSceneMouseEvent(QEvent.Type.GraphicsSceneMousePress)
    press.setScenePos(margin_scene_pos)
    press.setPos(item.mapFromScene(margin_scene_pos))
    press.setButton(Qt.MouseButton.LeftButton)
    press.setButtons(Qt.MouseButton.LeftButton)
    qapp.sendEvent(scene, press)

    assert scene.focusItem() is editor, "箱の余白クリックでエディタへフォーカスが戻ること"
    assert editor.textCursor().position() > 0, "クリック位置に応じてキャレットが移動すること"

    item.cancel_text_edit()
    scene.close()


# --------------------------------------------------------------------------
# 12. review2 所見3: ガードは focusItem() ではなく active_text_edit_item() を見る
# --------------------------------------------------------------------------


def test_shortcut_override_accepted_even_if_scene_focus_is_lost(qapp: Any) -> None:
    """ガードの判定条件を `scene.focusItem() is not None` のままにしていた場合、
    フォーカスが失われた瞬間（review2 所見1）に "r" 等の1文字ショートカットが
    QAction へ抜けてツールが切り替わってしまう。`active_text_edit_item()` に
    揃えたことで、フォーカスの生死に関わらず編集モード中は奪い続けることを固定する
    （`test_margin_click_restores_editor_focus_and_moves_caret` がフォーカスは
    同期的に復帰することも別途固定しているが、本テストはその復帰に依存しない
    防御であることを検証する）。
    """
    scene, _stack, _obj, item = _scene_with_text()
    view = CanvasView(scene)
    view.resize(400, 300)
    view.show()
    qapp.processEvents()

    item.begin_text_edit()
    item._editor.clearFocus()
    assert scene.focusItem() is None, "前提: フォーカスが失われた状態を再現する"

    event = _shortcut_override_event(Qt.Key.Key_R, Qt.KeyboardModifier.NoModifier, "r")
    view.event(event)
    assert event.isAccepted(), "フォーカスが失われていても編集中は 'r' を奪うこと"

    item.cancel_text_edit()
    scene.close()
