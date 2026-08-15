"""text のインプレース編集の見た目完全一致テスト（inline_text_contract.md §D、担当A）。

方針 b の核心: 「編集中の見た目は確定後の描画と完全一致」させる。
`TextItem.paint`（drawText 経路）と `TextEditorItem`（QGraphicsTextItem/QTextDocument 経路）の
レイアウトが同一フォント・同一折返し幅・同一マージン・同一整列で一致することを、
scene 合成（親 TextItem + 子 TextEditorItem）を QImage へ render してピクセル比較で固定する。

`tests/test_shapes_m3.py`（`_render_item` の流儀）・`tests/test_curve_node_edit.py`
（scene/undo_stack を使った begin/commit/cancel のテスト流儀）に倣うが、本ファイルは
scene 側（担当B）の実装に依存しない単体テストとして完結させる（`_notify_scene_text_edit`
はダックタイピングで no-op になるため、素の `QGraphicsScene` で十分）。
"""

from __future__ import annotations

from typing import Any

import numpy as np
import pytest
from PySide6.QtCore import QRectF
from PySide6.QtGui import QImage, QPainter, QUndoStack
from PySide6.QtWidgets import QGraphicsScene

from app.commands.commands import AddObjectCommand
from app.model.document import Document
from app.model.objects import TextObject
from app.scene.canvas_scene import CanvasScene
from app.scene.items.text_item import TextItem

_MULTILINE_JA = "日本語のテキストです。\n複数行にわたる内容を確認する。\nテスト文字列。"


# --------------------------------------------------------------------------
# ヘルパ
# --------------------------------------------------------------------------


def _make_scene_item(
    text: str,
    *,
    align: str = "left",
    valign: str = "top",
    bold: bool = False,
    font_size: float = 18.0,
    width: float = 220.0,
    height: float = 220.0,
    locked: bool = False,
) -> tuple[QGraphicsScene, TextItem, TextObject]:
    obj = TextObject(
        id=1,
        text=text,
        x=0.0,
        y=0.0,
        width=width,
        height=height,
        align=align,
        valign=valign,
        bold=bold,
        font_size=font_size,
        locked=locked,
    )
    scene = QGraphicsScene()
    item = TextItem(obj)
    scene.addItem(item)
    item.sync_from_model()
    return scene, item, obj


def _render_scene(scene: QGraphicsScene, rect: QRectF, w: int, h: int) -> np.ndarray:
    """`rect`（シーン座標）の領域を wxh の `QImage` へ render し、RGBA numpy 配列で返す。

    子アイテム（`TextEditorItem`）も含めて合成される点が `test_shapes_m3._render_item`
    （`item.paint()` 直呼び、子を含まない）と異なる。
    """
    image = QImage(w, h, QImage.Format.Format_ARGB32)
    image.fill(0)
    painter = QPainter(image)
    try:
        painter.setRenderHint(QPainter.RenderHint.Antialiasing, True)
        scene.render(painter, QRectF(0.0, 0.0, float(w), float(h)), rect)
    finally:
        painter.end()
    qimage = image.convertToFormat(QImage.Format.Format_RGBA8888)
    buf = bytes(qimage.constBits())[: w * h * 4]
    return np.frombuffer(buf, dtype=np.uint8).reshape(h, w, 4)


def _render_paint_only(item: Any, w: int = 160, h: int = 160) -> np.ndarray:
    """`item.paint()` だけを直接呼んで render する（子アイテムは含まない）。

    `test_shapes_m3._render_item` と同じ流儀。編集中に本体が二重描画しないことの確認に使う。
    """
    image = QImage(w, h, QImage.Format.Format_ARGB32)
    image.fill(0)
    painter = QPainter(image)
    try:
        painter.setRenderHint(QPainter.RenderHint.Antialiasing, True)
        item.paint(painter, None, None)
    finally:
        painter.end()
    qimage = image.convertToFormat(QImage.Format.Format_RGBA8888)
    buf = bytes(qimage.constBits())[: w * h * 4]
    return np.frombuffer(buf, dtype=np.uint8).reshape(h, w, 4)


def _mismatch_fraction(a: np.ndarray, b: np.ndarray, *, channel_tol: int = 24) -> float:
    """全画素に対する「有意に異なる」画素の割合（アンチエイリアスの丸め猶予つき）。"""
    diff = np.abs(a.astype(np.int16) - b.astype(np.int16))
    mismatched = np.any(diff > channel_tol, axis=-1)
    return float(mismatched.mean())


_RENDER_RECT = QRectF(-10.0, -10.0, 240.0, 240.0)
_RENDER_SIZE = 240


# --------------------------------------------------------------------------
# 1. ピクセル一致（方針 b の本丸）
# --------------------------------------------------------------------------


@pytest.mark.parametrize(
    "align,valign,bold,font_size",
    [
        ("left", "top", False, 18.0),
        ("left", "middle", False, 18.0),
        ("left", "bottom", False, 18.0),
        ("center", "top", False, 18.0),
        ("center", "middle", True, 32.0),
        ("center", "bottom", True, 32.0),
        ("right", "top", True, 32.0),
        ("right", "middle", False, 18.0),
        ("right", "bottom", True, 32.0),
    ],
)
def test_edit_mode_render_matches_normal_render(
    qapp: Any, align: str, valign: str, bold: bool, font_size: float
) -> None:
    scene, item, _obj = _make_scene_item(
        _MULTILINE_JA, align=align, valign=valign, bold=bold, font_size=font_size
    )

    normal = _render_scene(scene, _RENDER_RECT, _RENDER_SIZE, _RENDER_SIZE)

    assert item.begin_text_edit() is True
    # カーソル/選択のちらつきが混ざらないよう、撮影前にフォーカスを外す
    # （契約 §D-1: 「フォーカスを外すか...カーソル非表示条件で撮ること」）。
    item._editor.clearFocus()
    editing = _render_scene(scene, _RENDER_RECT, _RENDER_SIZE, _RENDER_SIZE)

    frac = _mismatch_fraction(normal, editing)
    assert frac < 0.005, (
        f"align={align} valign={valign} bold={bold} font_size={font_size}: "
        f"mismatch={frac:.4%} (threshold 0.5%)"
    )


# --------------------------------------------------------------------------
# 2. 編集中に文字を足すと valign=middle/bottom でエディタ y が再計算される
# --------------------------------------------------------------------------


@pytest.mark.parametrize("valign", ["middle", "bottom"])
def test_editor_y_recalculates_on_text_growth(qapp: Any, valign: str) -> None:
    scene, item, _obj = _make_scene_item("短い", valign=valign, width=150.0, height=200.0)

    assert item.begin_text_edit() is True
    editor = item._editor
    y_before = editor.y()

    editor.setPlainText("短い\nもっと長い複数行のテキストをここに追加していく\nさらに行を足す")
    y_after = editor.y()

    assert y_after != y_before
    # テキストが伸びる = 残り空間が減る = オフセットが小さくなる(top からの下げ幅が減る)
    assert y_after < y_before


def test_editor_y_unchanged_on_text_growth_for_top_valign(qapp: Any) -> None:
    scene, item, _obj = _make_scene_item("短い", valign="top", width=150.0, height=200.0)

    assert item.begin_text_edit() is True
    editor = item._editor
    y_before = editor.y()

    editor.setPlainText("短い\nもっと長い複数行のテキストをここに追加していく\nさらに行を足す")
    y_after = editor.y()

    assert y_before == 0.0
    assert y_after == 0.0


# --------------------------------------------------------------------------
# 3. begin -> commit で commit_text 経由の 1 undo マクロ・高さ再採寸・undo 完全復元
# --------------------------------------------------------------------------


def test_begin_commit_text_edit_goes_through_commit_text_as_one_undo_macro(qapp: Any) -> None:
    doc = Document()
    obj = TextObject(id=doc.new_id(), text="テキスト", x=50.0, y=50.0, width=100.0, height=40.0)
    scene = CanvasScene(doc)
    stack = QUndoStack()
    scene.set_undo_stack(stack)
    stack.push(AddObjectCommand(scene.document, obj))
    item = scene.item_for(obj)
    assert isinstance(item, TextItem)

    idx_before = stack.index()
    assert item.begin_text_edit() is True
    editor = item._editor
    assert editor is not None
    editor.setPlainText("新しいテキスト\nともう一行増えて箱が高くなる")

    item.commit_text_edit()

    assert item._text_edit_mode is False
    assert item._editor is None
    assert obj.text == "新しいテキスト\nともう一行増えて箱が高くなる"
    # begin->commit の全体が 1 undo マクロであること(commit_text 自身が push する
    # text + height の 2 コマンドも合わせて、undo 操作としては 1 ステップ)。
    assert stack.index() == idx_before + 1

    stack.undo()
    assert obj.text == "テキスト"
    stack.redo()
    assert obj.text == "新しいテキスト\nともう一行増えて箱が高くなる"


# --------------------------------------------------------------------------
# 4. begin -> cancel でモデル不変・表示復元
# --------------------------------------------------------------------------


def test_begin_cancel_text_edit_keeps_model_unchanged_and_restores_display(qapp: Any) -> None:
    scene, item, obj = _make_scene_item("変更前のテキスト", width=160.0, height=160.0)

    render_rect = QRectF(-10.0, -10.0, 180.0, 180.0)
    before = _render_scene(scene, render_rect, 180, 180)

    assert item.begin_text_edit() is True
    item._editor.setPlainText("編集中の下書きで確定はしない")
    item.cancel_text_edit()

    assert obj.text == "変更前のテキスト", "cancel はモデルを変更しない"
    assert item._text_edit_mode is False
    assert item._editor is None

    after = _render_scene(scene, render_rect, 180, 180)
    frac = _mismatch_fraction(before, after)
    assert frac < 0.005, f"cancel 後の表示復元が一致しない: mismatch={frac:.4%}"


# --------------------------------------------------------------------------
# 5. locked で begin_text_edit が False
# --------------------------------------------------------------------------


def test_begin_text_edit_locked_returns_false(qapp: Any) -> None:
    _scene, item, _obj = _make_scene_item("固定テキスト", locked=True)

    assert item.begin_text_edit() is False
    assert item._text_edit_mode is False
    assert item._editor is None


# --------------------------------------------------------------------------
# 6. 編集中 paint が本体テキストを描かない(二重描画防止)
# --------------------------------------------------------------------------


def test_paint_draws_nothing_while_editing(qapp: Any) -> None:
    _scene, item, _obj = _make_scene_item("編集中は本体を描かない", width=100.0, height=40.0)

    assert item.begin_text_edit() is True
    arr = _render_paint_only(item, 120, 120)
    assert int(np.count_nonzero(arr[..., 3] > 0)) == 0, "編集中は paint が何も描かないこと"


def test_paint_suppresses_placeholder_for_empty_text_while_editing(qapp: Any) -> None:
    _scene, item, _obj = _make_scene_item("", width=60.0, height=30.0)

    assert item.begin_text_edit() is True
    arr = _render_paint_only(item, 80, 80)
    assert (
        int(np.count_nonzero(arr[..., 3] > 0)) == 0
    ), "空テキストでも編集中はプレースホルダ破線を描かないこと"
