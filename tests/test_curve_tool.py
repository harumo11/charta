"""curve 作成ツールの統合テスト(curve_contract.md §D、担当C `tool_manager.py`/`canvas_view.py`
の実装が前提)。

`window`/`_FakeEvent` の流儀は `tests/test_shapes_m3.py`・`tests/test_interaction.py` に倣う。
担当B/Cが並行実装中のため本ファイルは執筆時点では未検証(全体緑化は後続の修正フェーズが担う)。
契約が定める正しい挙動を主張しており、実装に合わせて弱めていない。
"""

from __future__ import annotations

from typing import Any

import pytest
import shiboken6
from PySide6.QtCore import QEvent, QPoint, QPointF, Qt
from PySide6.QtGui import QContextMenuEvent, QKeyEvent
from PySide6.QtWidgets import QGraphicsView

from app.ui.main_window import MainWindow


class _FakeEvent:
    """`ToolManager.handle_mouse_*` に渡す最小限のマウスイベント代替(M1 test_interaction 流用)。"""

    def __init__(self, button: Qt.MouseButton = Qt.MouseButton.LeftButton) -> None:
        self._button = button

    def button(self) -> Qt.MouseButton:
        return self._button


def _key_event(key: Any) -> QKeyEvent:
    return QKeyEvent(QEvent.Type.KeyPress, key, Qt.KeyboardModifier.NoModifier)


@pytest.fixture
def window(qapp: Any) -> Any:
    w = MainWindow()
    w.resize(900, 700)
    w.show()
    yield w
    if shiboken6.isValid(w):
        w.close()


# --------------------------------------------------------------------------
# 3クリック + Enter -> 開いた曲線
# --------------------------------------------------------------------------


def test_three_clicks_then_enter_creates_open_curve(window: Any, qapp: Any) -> None:
    scene = window.scene
    stack = window.undo_stack
    tm = window.tool_manager
    view = window.view

    idx_before = stack.index()
    tm.set_tool("curve")
    tm.handle_mouse_press(_FakeEvent(), QPointF(10.0, 10.0))
    tm.handle_mouse_release(_FakeEvent(), QPointF(10.0, 10.0))
    tm.handle_mouse_press(_FakeEvent(), QPointF(60.0, 10.0))
    tm.handle_mouse_release(_FakeEvent(), QPointF(60.0, 10.0))
    tm.handle_mouse_press(_FakeEvent(), QPointF(60.0, 60.0))
    tm.handle_mouse_release(_FakeEvent(), QPointF(60.0, 60.0))

    view.keyPressEvent(_key_event(Qt.Key.Key_Return))

    assert stack.index() == idx_before + 1, "undo が1件積まれること"
    new_obj = scene.document.objects[-1]
    assert new_obj.type == "curve"
    assert new_obj.closed is False
    assert len(new_obj.points) == 3
    assert tm.current_tool() == "select"
    assert scene.selected_objects() == [new_obj], "生成直後に新規オブジェクトが選択されること"


# --------------------------------------------------------------------------
# 始点付近クリック(3点以上)で閉じる/2点では開いたまま
# --------------------------------------------------------------------------


def test_click_near_start_point_closes_curve_with_three_or_more_points(
    window: Any, qapp: Any
) -> None:
    scene = window.scene
    stack = window.undo_stack
    tm = window.tool_manager

    idx_before = stack.index()
    tm.set_tool("curve")
    tm.handle_mouse_press(_FakeEvent(), QPointF(10.0, 10.0))
    tm.handle_mouse_press(_FakeEvent(), QPointF(60.0, 10.0))
    tm.handle_mouse_press(_FakeEvent(), QPointF(60.0, 60.0))
    # 始点(10,10)付近をクリック(3点以上) -> 閉じて確定。
    tm.handle_mouse_press(_FakeEvent(), QPointF(12.0, 11.0))

    assert stack.index() == idx_before + 1
    new_obj = scene.document.objects[-1]
    assert new_obj.type == "curve"
    assert new_obj.closed is True
    assert len(new_obj.points) == 3, "始点クリック自体は新たな点を追加しないこと"


def test_click_near_start_with_two_points_adds_point_instead_of_closing(
    window: Any, qapp: Any
) -> None:
    """2点しか無いときは「始点付近クリックで閉じる」条件(3点以上)を満たさないため、
    通常どおり新しい点として追加される(下書きは開いたまま継続)。
    """
    stack = window.undo_stack
    tm = window.tool_manager

    idx_before = stack.index()
    tm.set_tool("curve")
    tm.handle_mouse_press(_FakeEvent(), QPointF(10.0, 10.0))
    tm.handle_mouse_press(_FakeEvent(), QPointF(60.0, 10.0))
    tm.handle_mouse_press(_FakeEvent(), QPointF(12.0, 11.0))

    assert stack.index() == idx_before, "2点では確定しないこと"
    assert tm.has_curve_draft() is True
    assert len(tm._curve_points) == 3

    tm.cancel_curve_draft()


# --------------------------------------------------------------------------
# Esc キャンセル
# --------------------------------------------------------------------------


def test_escape_cancels_curve_draft_without_commands_or_preview(window: Any, qapp: Any) -> None:
    stack = window.undo_stack
    tm = window.tool_manager
    view = window.view

    idx_before = stack.index()
    tm.set_tool("curve")
    tm.handle_mouse_press(_FakeEvent(), QPointF(10.0, 10.0))
    tm.handle_mouse_press(_FakeEvent(), QPointF(60.0, 10.0))

    handled = view._handle_curve_draft_key(_key_event(Qt.Key.Key_Escape))

    assert handled is True
    assert stack.index() == idx_before, "キャンセルでは undo 履歴が増えないこと"
    assert tm.has_curve_draft() is False
    assert tm._curve_preview is None, "プレビュー残渣が無いこと"
    assert tm._curve_nodes_preview is None


# --------------------------------------------------------------------------
# 1点でEnter -> 生成なし
# --------------------------------------------------------------------------


def test_single_point_then_enter_does_not_create(window: Any, qapp: Any) -> None:
    stack = window.undo_stack
    tm = window.tool_manager
    view = window.view

    idx_before = stack.index()
    tm.set_tool("curve")
    tm.handle_mouse_press(_FakeEvent(), QPointF(10.0, 10.0))

    handled = view._handle_curve_draft_key(_key_event(Qt.Key.Key_Return))

    assert handled is True
    assert stack.index() == idx_before
    assert tm.has_curve_draft() is False


# --------------------------------------------------------------------------
# set_tool(別ツール): 2点以上で確定 / 1点で確定しない / tool_changed 二重発火なし
# --------------------------------------------------------------------------


def test_set_tool_switch_commits_draft_with_two_or_more_points(window: Any, qapp: Any) -> None:
    scene = window.scene
    stack = window.undo_stack
    tm = window.tool_manager

    idx_before = stack.index()
    tm.set_tool("curve")
    tm.handle_mouse_press(_FakeEvent(), QPointF(10.0, 10.0))
    tm.handle_mouse_press(_FakeEvent(), QPointF(60.0, 10.0))

    captured: list[str] = []
    tm.tool_changed.connect(captured.append)
    tm.set_tool("rect")

    assert stack.index() == idx_before + 1, "確定で1件だけ push されること"
    assert tm.current_tool() == "rect"
    assert captured == ["rect"], "tool_changed が二重発火しないこと"
    new_obj = scene.document.objects[-1]
    assert new_obj.type == "curve"
    assert new_obj.closed is False


def test_set_tool_switch_with_single_point_does_not_push(window: Any, qapp: Any) -> None:
    stack = window.undo_stack
    tm = window.tool_manager

    idx_before = stack.index()
    tm.set_tool("curve")
    tm.handle_mouse_press(_FakeEvent(), QPointF(10.0, 10.0))

    tm.set_tool("rect")

    assert stack.index() == idx_before, "1点だけの下書きは確定しないこと"
    assert tm.current_tool() == "rect"


def test_reselecting_curve_tool_keeps_draft(window: Any, qapp: Any) -> None:
    """同一ツール(curve)の再クリックでは、既存の他ツールと同様に下書きが維持される。"""
    tm = window.tool_manager
    tm.set_tool("curve")
    tm.handle_mouse_press(_FakeEvent(), QPointF(10.0, 10.0))
    tm.handle_mouse_press(_FakeEvent(), QPointF(60.0, 10.0))

    tm.set_tool("curve")

    assert tm.has_curve_draft() is True
    assert len(tm._curve_points) == 2
    tm.cancel_curve_draft()


# --------------------------------------------------------------------------
# ダブルクリック確定・末尾重複点の除去
# --------------------------------------------------------------------------


def test_double_click_commits_without_duplicate_tail_point(window: Any, qapp: Any) -> None:
    scene = window.scene
    stack = window.undo_stack
    tm = window.tool_manager

    idx_before = stack.index()
    tm.set_tool("curve")
    tm.handle_mouse_press(_FakeEvent(), QPointF(10.0, 10.0))
    tm.handle_mouse_press(_FakeEvent(), QPointF(60.0, 10.0))
    tm.handle_mouse_press(_FakeEvent(), QPointF(60.0, 60.0))

    # 最後の press とほぼ同座標でダブルクリック -> 重複点は落とされて確定する。
    handled = tm.handle_mouse_double_click(_FakeEvent(), QPointF(60.5, 60.2))

    assert handled is True
    assert stack.index() == idx_before + 1
    new_obj = scene.document.objects[-1]
    assert new_obj.type == "curve"
    assert len(new_obj.points) == 3, "末尾の重複点が落とされていること"
    assert tm.current_tool() == "select"


def test_double_click_with_no_draft_is_consumed_while_curve_tool_active(
    window: Any, qapp: Any
) -> None:
    """curve ツール中は下書きが無くてもダブルクリックを消費する
    (素通しすると QGraphicsView 既定処理で ImageItem.begin_crop が誤爆するため)。
    """
    tm = window.tool_manager
    tm.set_tool("curve")

    handled = tm.handle_mouse_double_click(_FakeEvent(), QPointF(10.0, 10.0))

    assert handled is True


def test_double_click_consumption_reads_the_tool_before_dispatch(window: Any, qapp: Any) -> None:
    """`tool == "curve"` の判定はハンドラ実行「前」の `self._tool` を見ること
    （レビュー所見: 実行後の `self._tool` を読むと、左ダブルクリック確定が
    `set_tool("select")` まで済ませてしまい判定が死ぬ）。

    `_curve_double_click` を「確定して False を返す」スタブに差し替えて、
    ハンドラ実行後に `self._tool` が既に "select" になっている状況を再現する。
    """
    tm = window.tool_manager
    tm.set_tool("curve")

    def _fake_double_click_that_switches_tool_and_returns_false(
        event: Any, scene_pos: QPointF
    ) -> bool:
        tm._tool = "select"
        return False

    tm._handlers["curve"] = tm._handlers["curve"]._replace(
        double=_fake_double_click_that_switches_tool_and_returns_false
    )

    handled = tm.handle_mouse_double_click(_FakeEvent(), QPointF(10.0, 10.0))

    assert handled is True, "ディスパッチ前の tool=='curve' で判定すること"


# --------------------------------------------------------------------------
# is_interacting()
# --------------------------------------------------------------------------


def test_is_interacting_true_during_draft_false_after_commit(window: Any, qapp: Any) -> None:
    tm = window.tool_manager
    view = window.view

    tm.set_tool("curve")
    assert tm.is_interacting() is False

    tm.handle_mouse_press(_FakeEvent(), QPointF(10.0, 10.0))
    assert tm.is_interacting() is True

    tm.handle_mouse_press(_FakeEvent(), QPointF(60.0, 10.0))
    view.keyPressEvent(_key_event(Qt.Key.Key_Return))

    assert tm.is_interacting() is False


# --------------------------------------------------------------------------
# 右クリック確定 -> 直後の合成 contextMenuEvent を抑止(consume_context_menu_suppression)
# --------------------------------------------------------------------------


def test_right_click_commit_suppresses_immediate_context_menu(window: Any, qapp: Any) -> None:
    scene = window.scene
    stack = window.undo_stack
    tm = window.tool_manager
    view = window.view

    captured: list[Any] = []
    view.context_menu_requested.connect(lambda *args: captured.append(args))

    idx_before = stack.index()
    tm.set_tool("curve")
    tm.handle_mouse_press(_FakeEvent(), QPointF(10.0, 10.0))
    tm.handle_mouse_press(_FakeEvent(), QPointF(60.0, 10.0))
    tm.handle_mouse_press(_FakeEvent(), QPointF(60.0, 60.0))

    # 右クリックで開いたまま確定する。
    handled = tm.handle_mouse_press(_FakeEvent(Qt.MouseButton.RightButton), QPointF(60.0, 60.0))
    assert handled is True
    assert stack.index() == idx_before + 1
    new_obj = scene.document.objects[-1]
    assert new_obj.closed is False
    assert tm.current_tool() == "select"

    # Qt が press の直後に合成する QContextMenuEvent を模す。
    event = QContextMenuEvent(QContextMenuEvent.Reason.Mouse, QPoint(60, 60), QPoint(60, 60))
    view.contextMenuEvent(event)

    assert captured == [], "右クリック確定直後の合成 contextMenuEvent で emit されないこと"


def test_suppression_flag_does_not_leak_into_a_later_unrelated_right_click(
    window: Any, qapp: Any
) -> None:
    """合成 QContextMenuEvent が届かなかった場合でも、抑止フラグは次のプレスで
    クリアされ、無関係な後続の正当な右クリックメニューを黙って食わない
    （レビュー所見: ラッチが期限なしで残るとこれが起きる）。
    """
    tm = window.tool_manager
    view = window.view

    tm.set_tool("curve")
    tm.handle_mouse_press(_FakeEvent(), QPointF(10.0, 10.0))
    tm.handle_mouse_press(_FakeEvent(), QPointF(60.0, 10.0))
    tm.handle_mouse_press(_FakeEvent(), QPointF(60.0, 60.0))
    # 右クリックで開いたまま確定する（フラグが立つ）。ここでは合成
    # QContextMenuEvent を一切配送しない = フラグが消費されない失敗経路。
    tm.handle_mouse_press(_FakeEvent(Qt.MouseButton.RightButton), QPointF(60.0, 60.0))

    # `MainWindow` は `context_menu_requested` に実メニューを `exec()` する
    # スロットを繋いでいる。ヘッドレスで実際に emit させると `exec()` が
    # ブロックして固まるため、ここでは切り離してから自前の捕捉に差し替える
    # （`test_right_click_commit_suppresses_immediate_context_menu` は抑止される
    # ため emit されず、この問題に触れずに済んでいた）。
    view.context_menu_requested.disconnect(window._show_canvas_context_menu)
    captured: list[Any] = []
    view.context_menu_requested.connect(lambda *args: captured.append(args))

    # select ツールでの、確定とは無関係な正当な右クリック（press/release で
    # is_interacting() を False に戻してから contextMenuEvent を配送する）。
    tm.handle_mouse_press(_FakeEvent(Qt.MouseButton.RightButton), QPointF(200.0, 200.0))
    tm.handle_mouse_release(_FakeEvent(Qt.MouseButton.RightButton), QPointF(200.0, 200.0))
    event = QContextMenuEvent(QContextMenuEvent.Reason.Mouse, QPoint(200, 200), QPoint(200, 200))
    view.contextMenuEvent(event)

    assert captured != [], "無関係な右クリックのメニューまで食われないこと"


def test_context_menu_ignored_while_curve_draft_in_progress(window: Any, qapp: Any) -> None:
    """下書き中(is_interacting()==True)は通常の busy 判定で contextMenuEvent が無視される。"""
    tm = window.tool_manager
    view = window.view

    captured: list[Any] = []
    view.context_menu_requested.connect(lambda *args: captured.append(args))

    tm.set_tool("curve")
    tm.handle_mouse_press(_FakeEvent(), QPointF(10.0, 10.0))
    tm.handle_mouse_press(_FakeEvent(), QPointF(60.0, 10.0))
    assert tm.is_interacting() is True

    event = QContextMenuEvent(QContextMenuEvent.Reason.Mouse, QPoint(60, 10), QPoint(60, 10))
    view.contextMenuEvent(event)

    assert captured == []
    tm.cancel_curve_draft()


# --------------------------------------------------------------------------
# document 差し替えで下書き消滅
# --------------------------------------------------------------------------


def test_document_replaced_clears_curve_draft(window: Any, qapp: Any) -> None:
    from app.model.document import Document

    tm = window.tool_manager

    tm.set_tool("curve")
    tm.handle_mouse_press(_FakeEvent(), QPointF(10.0, 10.0))
    tm.handle_mouse_press(_FakeEvent(), QPointF(60.0, 10.0))
    assert tm.has_curve_draft() is True

    window.scene.set_document(Document())

    assert tm.has_curve_draft() is False


# --------------------------------------------------------------------------
# select ツールへの drag mode 復帰(既存 _DRAW_TOOLS と同様、curve も NoDrag になること)
# --------------------------------------------------------------------------


def test_curve_tool_disables_rubber_band_drag(window: Any, qapp: Any) -> None:
    view = window.view
    tm = window.tool_manager

    tm.set_tool("curve")
    assert view.dragMode() == QGraphicsView.DragMode.NoDrag

    tm.cancel_curve_draft()
    tm.set_tool("select")
    assert view.dragMode() == QGraphicsView.DragMode.RubberBandDrag
