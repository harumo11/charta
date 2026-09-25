"""レビュー finding #5 残課題「b'」: 実ドラッグ後に吸着で正味変位が 1px 未満に
収まったとき、`item.pos()` とモデルが食い違ったまま残らないことの回帰テスト
（担当 X。`_select_release` のコマンド確定ゲートは `app/tools/tool_manager.py`
の `_GEOM_NOOP_EPS` を参照）。

クリック/ドラッグの区別自体（`_DRAG_START_SCREEN_PX` の閾値・グループ入り等、
finding #5/#6 の中心部分）は G/F 担当の並行編集で既に解消済みであることを
`scratchpad/review/verify_G_1.py` 等で確認済みなので、ここでは「ドラッグは
成立したが吸着で正味変位が小さい」場合の確定ゲートだけを狙って検証する。
"""

from __future__ import annotations

from typing import Any

import pytest
import shiboken6
from PySide6.QtCore import QPoint, Qt
from PySide6.QtTest import QTest

from app.commands.commands import AddObjectCommand
from app.model.objects import RectObject
from app.ui.main_window import MainWindow

NM = Qt.KeyboardModifier.NoModifier
LB = Qt.MouseButton.LeftButton


@pytest.fixture
def window(qapp: Any) -> Any:
    w = MainWindow()
    w.resize(1200, 900)
    w.show()
    yield w
    if shiboken6.isValid(w):
        w.close()


def test_large_drag_that_snaps_back_to_small_net_displacement_still_commits(
    window: Any,
) -> None:
    """20 画面px 動かしてから吸着対象の近くへ戻す(正味変位 0.4px)実ドラッグでも、
    確定後は表示位置とモデルが一致し、undo 1 件が積まれる。
    """
    w = window
    w.view._apply_zoom_factor(1.0 / w.view.transform().m11())
    doc = w.scene.document

    target = RectObject(id=1, x=100.0, y=200.0, width=50.0, height=50.0)
    w.undo_stack.push(AddObjectCommand(doc, target))
    moving = RectObject(id=2, x=100.4, y=400.0, width=50.0, height=50.0)
    w.undo_stack.push(AddObjectCommand(doc, moving))
    QTest.qWait(0)

    w.scene.snap_enabled = True
    w.scene.clearSelection()
    w.scene.item_for(moving).setSelected(True)
    item = w.scene.item_for(moving)
    vp_pt = w.view.mapFromScene(item.mapToScene(item.boundingRect().center()))

    n0 = w.undo_stack.count()
    QTest.mousePress(w.view.viewport(), LB, NM, vp_pt)
    # ドラッグ開始閾値(既定 3 画面px)を確実に超えさせてから、吸着対象の近くへ戻す。
    QTest.mouseMove(w.view.viewport(), vp_pt + QPoint(20, 0))
    QTest.mouseMove(w.view.viewport(), vp_pt)
    QTest.mouseRelease(w.view.viewport(), LB, NM, vp_pt)

    moved = doc.object_by_id(2)
    moved_item = w.scene.item_for(moved)
    assert moved.x == pytest.approx(
        moved_item.pos().x(), abs=1e-6
    ), "モデルと表示位置が食い違ったまま残っている(b' 回帰)"
    assert moved.x == pytest.approx(100.0, abs=1e-6), "吸着先(x=100)に確定しているはず"
    assert w.undo_stack.count() - n0 == 1, "正味 0.4px でもコマンドが 1 つ積まれるはず"


def test_drag_that_snaps_back_to_exact_start_pushes_nothing(window: Any) -> None:
    """吸着の結果、正味変位が厳密に 0 になる場合は今までどおり何も確定しない
    (真の no-op はコマンドを積まない、finding #5 の noop シナリオ)。
    """
    w = window
    w.view._apply_zoom_factor(1.0 / w.view.transform().m11())
    doc = w.scene.document

    target = RectObject(id=1, x=100.0, y=200.0, width=50.0, height=50.0)
    w.undo_stack.push(AddObjectCommand(doc, target))
    moving = RectObject(id=2, x=100.0, y=400.0, width=50.0, height=50.0)
    w.undo_stack.push(AddObjectCommand(doc, moving))
    QTest.qWait(0)

    w.scene.snap_enabled = True
    w.scene.clearSelection()
    w.scene.item_for(moving).setSelected(True)
    item = w.scene.item_for(moving)
    vp_pt = w.view.mapFromScene(item.mapToScene(item.boundingRect().center()))

    n0 = w.undo_stack.count()
    QTest.mousePress(w.view.viewport(), LB, NM, vp_pt)
    QTest.mouseMove(w.view.viewport(), vp_pt + QPoint(20, 0))
    QTest.mouseMove(w.view.viewport(), vp_pt)
    QTest.mouseRelease(w.view.viewport(), LB, NM, vp_pt)

    moved = doc.object_by_id(2)
    assert moved.x == pytest.approx(100.0, abs=1e-6)
    assert w.undo_stack.count() - n0 == 0
