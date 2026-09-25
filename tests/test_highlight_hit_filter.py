"""`highlight_objects` のオーバーレイがヒット判定を横取りしない回帰テスト
（2026-09-25 レビュー3巡目 finding #1）。

`HighlightItem`（`app/agent/highlight.py`）は z=1e6・
`setAcceptedMouseButtons(NoButton)` の一時マーカーだが、既定の `shape()`
（＝padded な `boundingRect()`）のまま`app.scene.hit.topmost_item_at` の
デバイスpx矩形クエリに乗っていたため、ハイライト中の対象を実際にドラッグ
すると画面だけが動きモデルは無音のまま、右クリックメニューは古い選択のまま、
編集中のテキスト内クリックが編集を確定終了させる、という3系統の食い違いが
起きていた。

**罠（finding本文に明記）**: 使い捨ての `AgentAPI(window).highlight_objects(...)`
はオーバーレイが2回目のピックまでにヒットしなくなることが実測されている
（理由未特定）。誤って直ったように見える偽陰性を避けるため、本ファイルの
統合テストはすべて `window._api = AgentAPI(window)` のように常設の参照を
保持してから呼ぶ（実サーバの `AgentHost.api` も同様に持続する参照）。
"""

from __future__ import annotations

from typing import Any

import pytest
import shiboken6
from PySide6.QtCore import QPoint, QPointF, Qt
from PySide6.QtTest import QTest
from PySide6.QtWidgets import QGraphicsRectItem, QGraphicsScene, QMenu

from app.agent.api import AgentAPI
from app.agent.highlight import HighlightItem
from app.commands.commands import AddObjectCommand
from app.model.objects import RectObject, TextObject
from app.scene.hit import topmost_item_at
from app.ui.main_window import MainWindow

NM = Qt.KeyboardModifier.NoModifier
LB = Qt.MouseButton.LeftButton


@pytest.fixture
def window(qapp: Any) -> Any:
    w = MainWindow()
    w.resize(800, 600)
    w.show()
    yield w
    if shiboken6.isValid(w):
        w.close()


def _add_rect(window: Any, x: float, y: float, w: float = 80.0, h: float = 60.0) -> RectObject:
    # fill 既定(DEFAULT_SHAPE_FILL)のまま=塗りありなので内部クリックで掴める。
    scene = window.scene
    stack = window.undo_stack
    rect = RectObject(id=scene.document.new_id(), x=x, y=y, width=w, height=h)
    stack.push(AddObjectCommand(scene.document, rect))
    return rect


# --------------------------------------------------------------------------
# 単体テスト: HighlightItem.shape() / topmost_item_at の button フィルタ
# --------------------------------------------------------------------------


def test_highlight_item_shape_is_empty_but_still_paints(qapp: Any) -> None:
    item = HighlightItem((0.0, 0.0, 40.0, 30.0), label="A")
    assert item.shape().isEmpty(), "装飾専用: どのヒット判定にも参加してはいけない"
    assert not item.boundingRect().isEmpty(), "描画(boundingRect)は従来どおり有効なこと"


def test_topmost_item_at_skips_nobutton_items_no_view_fallback(qapp: Any) -> None:
    """view の無い（scene 単体の）フォールバック経路でも button フィルタが効くこと。

    `qapp` を明示依存にする: `QGraphicsScene`/`QGraphicsItem` は稼働中の
    `QApplication` が無いと offscreen platform で構築時に落ちる(実測)。
    """
    scene = QGraphicsScene()
    base = QGraphicsRectItem(0.0, 0.0, 50.0, 50.0)
    scene.addItem(base)
    overlay = QGraphicsRectItem(0.0, 0.0, 50.0, 50.0)
    overlay.setAcceptedMouseButtons(Qt.MouseButton.NoButton)
    overlay.setZValue(100.0)
    scene.addItem(overlay)

    assert topmost_item_at(scene, QPointF(25.0, 25.0)) is base
    assert topmost_item_at(scene, QPointF(25.0, 25.0), button=Qt.MouseButton.LeftButton) is base
    assert topmost_item_at(scene, QPointF(25.0, 25.0), button=Qt.MouseButton.RightButton) is base


# --------------------------------------------------------------------------
# 統合テスト(a): ハイライト中の実ドラッグ
# --------------------------------------------------------------------------


def test_drag_with_active_highlight_moves_model_and_pushes_one_undo(window: Any) -> None:
    w = window
    rect = _add_rect(w, 100.0, 100.0)
    w.scene.snap_enabled = False  # スナップの介入を排除し、ドラッグ量そのものを検証する。

    w._api = AgentAPI(w)  # 常設参照(モジュール docstring の罠を参照)。
    w._api.highlight_objects([rect.id], duration_ms=30000)

    w.tool_manager.set_tool("select")
    item = w.scene.item_for(rect)
    vp_pt = w.view.mapFromScene(item.mapToScene(item.boundingRect().center()))
    dst = vp_pt + QPoint(30, 20)

    n0 = w.undo_stack.count()
    QTest.mousePress(w.view.viewport(), LB, NM, vp_pt)
    QTest.mouseMove(w.view.viewport(), dst)
    QTest.mouseRelease(w.view.viewport(), LB, NM, dst)

    moved = w.scene.document.object_by_id(rect.id)
    assert moved.x == pytest.approx(
        130.0, abs=1e-6
    ), "ハイライトのオーバーレイに横取りされず本体がドラッグされること"
    assert moved.y == pytest.approx(120.0, abs=1e-6)
    assert w.undo_stack.count() - n0 == 1, "undo エントリは1個だけのこと"
    moved_item = w.scene.item_for(moved)
    assert moved.x == pytest.approx(
        moved_item.pos().x(), abs=1e-6
    ), "モデルと表示位置が一致すること"
    assert moved.y == pytest.approx(moved_item.pos().y(), abs=1e-6)


# --------------------------------------------------------------------------
# 統合テスト(b): ハイライト中の右クリックメニュー
# --------------------------------------------------------------------------


def test_right_click_on_highlighted_object_selects_it(
    window: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    w = window
    rect = _add_rect(w, 100.0, 100.0)
    other = _add_rect(w, 400.0, 100.0)
    w.scene.snap_enabled = False

    w._api = AgentAPI(w)
    w._api.highlight_objects([rect.id], duration_ms=30000)

    w.tool_manager.set_tool("select")
    w.scene.clearSelection()
    w.scene.item_for(other).setSelected(True)
    assert {o.id for o in w.scene.selected_objects()} == {other.id}

    captured: list[QMenu] = []
    original_build = w._build_canvas_context_menu

    def spy_build() -> QMenu:
        menu = original_build()
        monkeypatch.setattr(menu, "exec", lambda *args, **kwargs: None)
        captured.append(menu)
        return menu

    monkeypatch.setattr(w, "_build_canvas_context_menu", spy_build)

    item = w.scene.item_for(rect)
    scene_pos = item.mapToScene(item.boundingRect().center())
    w._show_canvas_context_menu(scene_pos, QPoint(0, 0))

    assert {o.id for o in w.scene.selected_objects()} == {
        rect.id
    }, "ハイライト中でも右クリックした rect が選択されること(古い選択のまま止まらない)"
    assert len(captured) == 1


# --------------------------------------------------------------------------
# 統合テスト(c): ハイライト中のテキストインプレース編集
# --------------------------------------------------------------------------


def test_click_inside_highlighted_text_edit_does_not_commit(window: Any) -> None:
    w = window
    document = w.scene.document
    text_obj = TextObject(
        id=document.new_id(), text="hello", x=100.0, y=100.0, width=140.0, height=60.0
    )
    document.add_object(text_obj)
    item = w.scene.item_for(text_obj)
    assert item.begin_text_edit() is True

    w._api = AgentAPI(w)
    w._api.highlight_objects([text_obj.id], duration_ms=30000)

    vp_pt = w.view.mapFromScene(item.mapToScene(item.boundingRect().center()))
    QTest.mouseClick(w.view.viewport(), LB, NM, vp_pt)

    assert (
        w.scene.active_text_edit_item() is item
    ), "編集対象の内部クリックがハイライトに横取りされて確定終了しないこと"
