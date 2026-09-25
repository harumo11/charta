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

import math
from typing import Any

import numpy as np
import pytest
from PySide6.QtCore import QEvent, QRectF, Qt
from PySide6.QtGui import (
    QFontDatabase,
    QFontInfo,
    QFontMetricsF,
    QGuiApplication,
    QImage,
    QInputMethodEvent,
    QKeyEvent,
    QPainter,
    QTextBlockFormat,
    QTextCursor,
    QUndoStack,
)
from PySide6.QtWidgets import QGraphicsScene, QGraphicsView, QWidget

from app.commands.commands import AddObjectCommand, SetPropertyCommand
from app.model.document import Document
from app.model.objects import TextObject
from app.scene.canvas_scene import CanvasScene
from app.scene.items.text_item import TextItem, font_for

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
    `widget` に実体のある `QWidget` を渡す（B-3: `TextItem._draw_placeholder` は
    `widget is None`＝書き出し経路とみなしてプレースホルダを描かないため、
    「画面描画を直接呼ぶ」という本テストの意図どおりに widget 引数を渡す）。
    """
    image = QImage(w, h, QImage.Format.Format_ARGB32)
    image.fill(0)
    painter = QPainter(image)
    widget = QWidget()
    try:
        painter.setRenderHint(QPainter.RenderHint.Antialiasing, True)
        item.paint(painter, None, widget)
    finally:
        painter.end()
    qimage = image.convertToFormat(QImage.Format.Format_RGBA8888)
    buf = bytes(qimage.constBits())[: w * h * 4]
    return np.frombuffer(buf, dtype=np.uint8).reshape(h, w, 4)


def _render_via_view(scene: QGraphicsScene, rect: QRectF, w: int, h: int) -> np.ndarray:
    """`QGraphicsView.grab()` 経由（`widget` 引数が実ウィジェットになる実画面描画。
    B-3 の実測: `scene.render()`/`QGraphicsView.render()` は常に `widget=None` だが、
    `QWidget.grab()`（内部で実際の paintEvent 相当の描画を行う）は viewport を渡す）
    でレンダリングする。プレースホルダの可視性を「実画面では出る」ことの確認に使う。
    """
    view = QGraphicsView(scene)
    view.setFrameStyle(0)
    view.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
    view.setVerticalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
    view.resize(w, h)
    view.setSceneRect(rect)
    view.fitInView(rect, Qt.AspectRatioMode.IgnoreAspectRatio)
    pixmap = view.grab()
    image = pixmap.toImage().convertToFormat(QImage.Format.Format_RGBA8888)
    buf = bytes(image.constBits())[: w * h * 4]
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
    # 所見6: 「両方とも真っ白で不一致0%」という偽陽性合格を防ぐため、通常描画に
    # 実際にインクがあることを主張する（フォント解決失敗を検出可能にする）。
    assert (
        int(np.count_nonzero(normal[..., 3] > 0)) > 0
    ), "通常描画が完全に透明（フォント解決失敗等で判別力が失われている疑い）"


def test_edit_mode_render_matches_normal_render_for_empty_text(qapp: Any) -> None:
    """パリティ本丸(1)に空テキストのケースを追加する（所見4: プレースホルダも
    編集中/非編集で完全一致することを固定する）。

    2026-09-25（B-3）: プレースホルダは実画面（`widget` が実体を持つ描画）でのみ
    出し、書き出し（`scene.render()` 経由。`widget=None`）では出さない。
    `_render_scene`（`scene.render()` 直呼び）は widget=None を渡すため、ここでは
    `_render_via_view`（`QGraphicsView.grab()`）を使い「実画面」を模す
    （実測: `scene.render()` は view の有無に関わらず常に widget=None、
    `QWidget.grab()` は viewport を渡す）。
    """
    scene, item, _obj = _make_scene_item("", width=100.0, height=60.0)

    normal = _render_via_view(scene, _RENDER_RECT, _RENDER_SIZE, _RENDER_SIZE)

    assert item.begin_text_edit() is True
    item._editor.clearFocus()
    editing = _render_via_view(scene, _RENDER_RECT, _RENDER_SIZE, _RENDER_SIZE)

    frac = _mismatch_fraction(normal, editing)
    assert frac < 0.005, f"空テキスト: mismatch={frac:.4%} (threshold 0.5%)"
    assert int(np.count_nonzero(normal[..., 3] > 0)) > 0, "プレースホルダ破線が描かれていない"


def test_placeholder_does_not_appear_in_exported_png(qapp: Any, tmp_path: Any) -> None:
    """空テキストのプレースホルダ破線が PNG 書き出しに写り込まない（B-3 回帰）。

    既存の不具合（`reports/text.md` 末尾）: 空テキストのプレースホルダが
    `png_exporter.render_artboard_image` の使い捨て `CanvasScene`（`scene.render()`
    経由 = `widget=None`）にも描かれてしまっていた。書き出しは背景色一色になること
    を固定する。
    """
    from app.export.png_exporter import render_artboard_image
    from app.model.document import Artboard, Document, Physical

    doc = Document(
        artboard=Artboard(
            width_px=200,
            height_px=150,
            physical=Physical(width_mm=50.0, target_dpi=150),
            background="#FFFFFF",
        )
    )
    doc.add_object(TextObject(id=doc.new_id(), text="", x=20, y=20, width=100, height=40))

    image = render_artboard_image(doc, transparent=False).convertToFormat(
        QImage.Format.Format_RGBA8888
    )
    arr = np.frombuffer(bytes(image.constBits()), dtype=np.uint8).reshape(
        image.height(), image.bytesPerLine() // 4, 4
    )[:, : image.width(), :]
    rgb = arr[..., :3]
    assert np.all(rgb == 255), "空テキストのプレースホルダ破線が PNG 書き出しに写り込んでいる"


# --------------------------------------------------------------------------
# 1b. 上端トリム（要望3・B-1）の代表ケースでの見た目一致
# --------------------------------------------------------------------------


@pytest.mark.parametrize(
    "text,label",
    [
        ("Only latin first line\n続きは日本語", "先頭行が欧文だけ"),
        ("日本語とEnglishが混在する先頭行\n二行目", "先頭行が和欧混在"),
        ("\nabc が二行目", "先頭行が空行"),
        ("Éclair\n二行目もある", "先頭行がアクセント付き大文字"),
    ],
)
def test_edit_mode_render_matches_normal_render_for_top_trim_cases(
    qapp: Any, text: str, label: str
) -> None:
    """上端トリム（B-1）の計算対象になる先頭行の代表ケースでも、編集中/非編集の
    見た目が一致する（`text_top_trim` と `_layout_lines` の trim が食い違うと、
    エディタだけ数 px ずれる）。
    """
    scene, item, _obj = _make_scene_item(text, width=220.0, height=220.0)

    normal = _render_scene(scene, _RENDER_RECT, _RENDER_SIZE, _RENDER_SIZE)
    assert int(np.count_nonzero(normal[..., 3] > 0)) > 0, f"{label}: 通常描画にインクが無い"

    assert item.begin_text_edit() is True
    item._editor.clearFocus()
    editing = _render_scene(scene, _RENDER_RECT, _RENDER_SIZE, _RENDER_SIZE)

    frac = _mismatch_fraction(normal, editing)
    assert frac < 0.005, f"{label}: mismatch={frac:.4%} (threshold 0.5%)"


@pytest.mark.parametrize("valign", ["top", "middle"])
@pytest.mark.parametrize(
    "family,text",
    [("EB Garamond", "Éclair ÅÉÎ"), ("DejaVu Sans", "Ẫ test")],
    ids=["EBGaramond", "DejaVuSans"],
)
def test_edit_mode_render_matches_normal_render_for_accent_overhang(
    qapp: Any, family: str, text: str, valign: str
) -> None:
    """先頭行の字面が typo ascender を超えるフォント（round-2 #5/#8。既定の
    Noto Sans CJK JP では起きないため `_make_scene_item` 既定と別に用意する）
    でも、編集中/非編集の見た目が一致する。全画素一致率だけでなくインクの
    上端/下端の行が ±1px 以内で一致することも見る（0.5% の全画素閾値は数px の
    上端超過を見逃しうるため。round-2 #4 Part1(d) と同じ機構を再利用）。
    """
    from PySide6.QtGui import QFontInfo

    probe_font = font_for(TextObject(id=0, font_family=family, font_size=24.0))
    if QFontInfo(probe_font).family() != family:
        pytest.skip(f"{family} はこの環境にインストールされていない")

    scene, item, obj = _make_scene_item(text, width=220.0, height=220.0, valign=valign)
    obj.font_family = family
    obj.font_size = 24.0
    item.sync_from_model()

    normal = _render_scene(scene, _RENDER_RECT, _RENDER_SIZE, _RENDER_SIZE)
    assert int(np.count_nonzero(normal[..., 3] > 0)) > 0, f"{family}: 通常描画にインクが無い"

    assert item.begin_text_edit() is True
    item._editor.clearFocus()
    editing = _render_scene(scene, _RENDER_RECT, _RENDER_SIZE, _RENDER_SIZE)

    frac = _mismatch_fraction(normal, editing)
    assert frac < 0.005, f"{family} valign={valign}: mismatch={frac:.4%} (threshold 0.5%)"

    normal_rows = _ink_row_range(normal)
    editing_rows = _ink_row_range(editing)
    assert normal_rows is not None and editing_rows is not None
    assert abs(normal_rows[0] - editing_rows[0]) <= 1, (
        f"{family} valign={valign}: インク上端の行が normal={normal_rows[0]} "
        f"editing={editing_rows[0]} でずれている"
    )
    assert abs(normal_rows[1] - editing_rows[1]) <= 1, (
        f"{family} valign={valign}: インク下端の行が normal={normal_rows[1]} "
        f"editing={editing_rows[1]} でずれている"
    )


def test_edit_mode_render_matches_normal_render_with_background(qapp: Any) -> None:
    """背景色あり（要望4・B-2）でも編集中/非編集の見た目が一致する。

    背景は親 `TextItem.paint` だけが描く（`TextEditorItem` は変更不要・`reports/text.md`
    §4-4 の想定どおり）ため、編集中も背景が消えず・二重にもならないことを固定する。
    """
    obj = TextObject(
        id=1,
        text=_MULTILINE_JA,
        x=0.0,
        y=0.0,
        width=220.0,
        height=220.0,
        background="#FFE0B2",
    )
    scene = QGraphicsScene()
    item = TextItem(obj)
    scene.addItem(item)
    item.sync_from_model()

    normal = _render_scene(scene, _RENDER_RECT, _RENDER_SIZE, _RENDER_SIZE)
    # 背景色そのものの画素が写っていること(箱内・文字が届かない右下寄りの隅を採る。
    # `_RENDER_RECT` の原点オフセット(-10,-10)分だけ画像座標はシーン座標より+10される)。
    assert tuple(int(v) for v in normal[205, 205][:3]) == (0xFF, 0xE0, 0xB2)

    assert item.begin_text_edit() is True
    item._editor.clearFocus()
    editing = _render_scene(scene, _RENDER_RECT, _RENDER_SIZE, _RENDER_SIZE)

    frac = _mismatch_fraction(normal, editing)
    assert frac < 0.005, f"背景色あり: mismatch={frac:.4%} (threshold 0.5%)"


def test_edit_mode_background_follows_growth_before_commit(qapp: Any) -> None:
    """レビュー3巡目 finding #15: 背景色ありのテキストを編集中に改行で箱より
    伸ばしても、背景プレビューが確定後の箱を先取りしていて食い違わないこと。

    以前は `TextItem.paint` が常に確定前の古い `obj.width/height` の箱を塗って
    いたため、改行で増えた行が背景の外（白地）に描かれていた
    （確定した瞬間だけ背景が本来の大きさへ飛ぶように見える）。

    `commit_text` が実際にモデルへ高さを反映するには本物の `undo_stack` が
    要る（`_make_scene_item` の素の `QGraphicsScene` には無く、`commit_text`
    は `undo_stack is None` を検出すると `sync_from_model()` のみで static
    return してしまう）ため、ここだけ `CanvasScene`+`QUndoStack` を使う
    （`test_begin_commit_text_edit_goes_through_commit_text_as_one_undo_macro`
    と同じ流儀）。
    """
    doc = Document()
    obj = TextObject(
        id=doc.new_id(),
        text="Ag",
        x=10.0,
        y=10.0,
        width=60.0,
        height=30.0,
        align="left",
        valign="middle",
        font_size=18.0,
        background="#2E7D32",
    )
    scene = CanvasScene(doc)
    stack = QUndoStack()
    scene.set_undo_stack(stack)
    stack.push(AddObjectCommand(scene.document, obj))
    item = scene.item_for(obj)
    assert isinstance(item, TextItem)

    assert item.begin_text_edit() is True
    cursor = item._editor.textCursor()
    cursor.movePosition(QTextCursor.MoveOperation.End)
    cursor.insertText("\nsecond line\nthird line")
    item._editor.setTextCursor(cursor)
    item._editor.clearFocus()

    editing = _render_scene(scene, _RENDER_RECT, _RENDER_SIZE, _RENDER_SIZE)
    assert item._edit_bg_rect is not None
    preview_scene_rect = item.mapRectToScene(item._edit_bg_rect).normalized()

    item.commit_text_edit()
    committed = _render_scene(scene, _RENDER_RECT, _RENDER_SIZE, _RENDER_SIZE)

    frac = _mismatch_fraction(editing, committed)
    assert (
        frac < 0.005
    ), f"編集中(伸びたプレビュー)と確定後で背景の見た目が食い違う: mismatch={frac:.4%}"

    committed_box = QRectF(obj.x, obj.y, obj.width, obj.height)
    assert abs(preview_scene_rect.left() - committed_box.left()) < 1.0
    assert abs(preview_scene_rect.top() - committed_box.top()) < 1.0
    assert abs(preview_scene_rect.width() - committed_box.width()) < 1.0
    assert abs(preview_scene_rect.height() - committed_box.height()) < 1.0


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
    """valign="top" では、行が増えても y は変わらない（既存挙動の非破壊）。

    2026-09-25（要望3・B-1）: `y` は `valign_offset - text_top_trim` になったため、
    トリムが 0 でない限り厳密に `0.0` ではなくなった（先頭行の字面まで詰めた分
    だけ上へシフトする）。ここでは「先頭行（"短い"）が変わらない限り y は一定」
    ことを固定する（トリムは先頭行だけで決まるため、行が増えても変わらない）。
    """
    from app.export.text_outline import text_top_trim
    from app.scene.items.text_item import font_for

    scene, item, obj = _make_scene_item("短い", valign="top", width=150.0, height=200.0)

    assert item.begin_text_edit() is True
    editor = item._editor
    y_before = editor.y()

    editor.setPlainText("短い\nもっと長い複数行のテキストをここに追加していく\nさらに行を足す")
    y_after = editor.y()

    expected_y = -text_top_trim("短い", font_for(obj), 150.0)
    assert y_before == pytest.approx(expected_y)
    assert y_after == pytest.approx(expected_y)


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


def test_paint_shows_placeholder_for_empty_text_while_editing(qapp: Any) -> None:
    """所見4の是正: 空テキストは編集中も確定後と同じプレースホルダ破線を描く。

    契約 §A-2 の原文「空テキストのプレースホルダ破線も出さない」は、TextItem が
    これまで描いてきた唯一の「枠」を編集中だけ消してしまい方針b（見た目完全一致）
    を自ら破る矛盾だったため、契約側の記述を「編集中も出す」に倒した
    （レビュー所見4の修正案どおり）。
    """
    _scene, item, _obj = _make_scene_item("", width=60.0, height=30.0)

    assert item.begin_text_edit() is True
    arr = _render_paint_only(item, 80, 80)
    assert (
        int(np.count_nonzero(arr[..., 3] > 0)) > 0
    ), "空テキストは編集中もプレースホルダ破線を描くこと（方針b・所見4）"


def test_paint_placeholder_tracks_editor_live_text_not_frozen_model_text(qapp: Any) -> None:
    """プレースホルダの有無はエディタの生テキストで判定し、凍結された `obj.text` では
    判定しない（advisor 指摘: `obj.text` 起点だと全選択削除/新規入力の両方で壊れる）。
    """
    # ケース1: 既存テキストを編集中に全選択削除 -> obj.text は変わらないが
    # プレースホルダが出る必要がある。
    _scene1, item1, _obj1 = _make_scene_item("hello", width=100.0, height=40.0)
    assert item1.begin_text_edit() is True
    item1._editor.setPlainText("")
    arr1 = _render_paint_only(item1, 120, 120)
    assert (
        int(np.count_nonzero(arr1[..., 3] > 0)) > 0
    ), "obj.text が非空のままでも、編集中の生テキストが空ならプレースホルダを描くこと"

    # ケース2: 空テキストのオブジェクトを編集開始し文字を入力 -> obj.text は
    # 空のままだが、プレースホルダは消えて二重描画にならない必要がある。
    _scene2, item2, _obj2 = _make_scene_item("", width=100.0, height=40.0)
    assert item2.begin_text_edit() is True
    item2._editor.setPlainText("abc")
    arr2 = _render_paint_only(item2, 120, 120)
    assert (
        int(np.count_nonzero(arr2[..., 3] > 0)) == 0
    ), "obj.text が空のままでも、編集中に文字を入れたらプレースホルダを消すこと"


# --------------------------------------------------------------------------
# 7. 既知の非一致（所見1）: 箱幅より長い1トークン × center/right は
#    QTextDocument が行幅基準（実質左寄せ）で整列する一方、paint/text_to_path/
#    SVG/PDF は箱幅基準で中央/右揃えするため編集中だけ食い違う。
# --------------------------------------------------------------------------


@pytest.mark.parametrize("align", ["center", "right"])
def test_long_unbreakable_token_wraps_and_matches_editor(qapp: Any, align: str) -> None:
    """箱幅を超える分割不能トークンは途中で折られ、編集中も見た目が一致する。

    旧仕様（WordWrap）では 1 行のまま箱を水平にはみ出し、center/right で編集中のみ
    整列が食い違う既知の非一致だった。2026-08-15 のユーザー決定で折返しモードを
    `WrapAtWordBoundaryOrAnywhere` に全経路統一（`text_outline.WRAP_MODE`）し解消。
    """
    token = "Supercalifragilisticexpialidocious"
    scene, item, obj = _make_scene_item(
        token,
        align=align,
        width=180.0,
        height=180.0,
        font_size=16.0,
    )
    # 折返し自体の回帰: 途中で折られて複数行になり、各行は箱幅に収まる。
    from app.export.text_outline import wrapped_lines
    from app.scene.items.text_item import font_for

    lines = wrapped_lines(token, font_for(obj), 180.0)
    assert len(lines) >= 2, "箱幅超過トークンが途中で折られていない"
    assert all(nw <= 180.0 + 1.0 for _t, nw, _top, _ascent in lines)

    render_rect = QRectF(-10.0, -10.0, 200.0, 200.0)
    normal = _render_scene(scene, render_rect, 200, 200)
    assert int((normal[..., 3] > 0).sum()) > 0, "通常描画にインクがあること（空対空の合格防止）"

    assert item.begin_text_edit() is True
    item._editor.clearFocus()
    editing = _render_scene(scene, render_rect, 200, 200)

    frac = _mismatch_fraction(normal, editing)
    assert frac < 0.005, f"align={align}: mismatch={frac:.4%} (threshold 0.5%)"


# --------------------------------------------------------------------------
# 8. タブ文字は単一スペースとして扱う（所見3）
# --------------------------------------------------------------------------


def test_tab_key_inserts_space_not_literal_tab(qapp: Any) -> None:
    """Tab キー押下はタブ文字ではなくスペースを挿入する（drawText の
    `TextExpandTabs` 非設定に合わせる）。paint との行送り一致もあわせて確認する。
    """
    scene, item, _obj = _make_scene_item("bbb", width=180.0, height=100.0)
    assert item.begin_text_edit() is True
    editor = item._editor

    cursor = editor.textCursor()
    cursor.movePosition(QTextCursor.MoveOperation.End)
    editor.setTextCursor(cursor)
    tab_event = QKeyEvent(QEvent.Type.KeyPress, Qt.Key.Key_Tab, Qt.KeyboardModifier.NoModifier)
    editor.keyPressEvent(tab_event)
    editor.keyPressEvent(
        QKeyEvent(QEvent.Type.KeyPress, Qt.Key.Key_Tab, Qt.KeyboardModifier.NoModifier)
    )
    editor.textCursor().insertText("ccc")

    assert "\t" not in editor.current_text(), "Tab キーがタブ文字を挿入しないこと"
    assert editor.current_text() == "bbb  ccc"


def test_pasted_tabs_are_normalized_to_spaces(qapp: Any) -> None:
    """クリップボード貼り付け（Ctrl+V）のタブもスペースへ正規化する（所見3）。"""
    scene, item, _obj = _make_scene_item("", width=180.0, height=100.0)
    assert item.begin_text_edit() is True
    editor = item._editor

    clipboard = QGuiApplication.clipboard()
    clipboard.setText("bbb\t\tccc")
    paste_event = QKeyEvent(QEvent.Type.KeyPress, Qt.Key.Key_V, Qt.KeyboardModifier.ControlModifier)
    editor.keyPressEvent(paste_event)

    assert "\t" not in editor.current_text(), "貼り付けのタブ文字がスペースに正規化されること"
    assert editor.current_text() == "bbb  ccc"


# --------------------------------------------------------------------------
# 9. 編集中の外部モデル変更（所見2）: font/color/align は再適用、
#    text の外部変更はエディタの下書きを上書きせずキャンセルする
# --------------------------------------------------------------------------


def test_external_style_change_during_edit_is_reflected_in_editor(qapp: Any) -> None:
    """編集中にプロパティパネル相当の `SetPropertyCommand`（font_size/color/align）が
    push されると、エディタの見た目にも反映される（所見2）。編集モードは継続する。
    """
    doc = Document()
    obj = TextObject(
        id=doc.new_id(),
        text="hello",
        x=0.0,
        y=0.0,
        width=200.0,
        height=100.0,
        font_size=18.0,
        color="#000000",
        align="left",
    )
    scene = CanvasScene(doc)
    stack = QUndoStack()
    scene.set_undo_stack(stack)
    stack.push(AddObjectCommand(scene.document, obj))
    item = scene.item_for(obj)
    assert isinstance(item, TextItem)

    assert item.begin_text_edit() is True
    editor = item._editor
    assert editor is not None

    stack.push(SetPropertyCommand(doc, obj, "font_size", 40.0, 18.0))
    stack.push(SetPropertyCommand(doc, obj, "color", "#ff0000", "#000000"))
    stack.push(SetPropertyCommand(doc, obj, "align", "center", "left"))

    assert item._text_edit_mode is True, "スタイル変更だけでは編集モードを打ち切らないこと"
    assert item._editor is editor, "エディタを作り直さず同一インスタンスのまま更新すること"
    assert editor.font().pixelSize() == pytest.approx(font_for(obj).pixelSize())
    assert editor.defaultTextColor().name() == "#ff0000"
    assert editor.document().defaultTextOption().alignment() == Qt.AlignmentFlag.AlignHCenter
    scene.close()


def test_external_style_change_during_edit_keeps_editor_undo_history(qapp: Any) -> None:
    """編集中に色/整列/太字などフォント以外のプロパティが外部から変わっても、
    エディタ内蔵の undo 履歴（`QTextDocument` 自身の Ctrl+Z。アプリの
    `QUndoStack` とは別物）が消えないこと（round-2 #3）。

    修正前は `TextItem._on_sync_geometry` が `sync_from_model()` 経由で
    `_apply_leading_compensation()` を毎回呼んでおり、そこにある
    `setUndoRedoEnabled(False)`→`True` の往復が「フォーマットだけの undo
    ステップを積まないため」に**ドキュメントの内部 undo 履歴そのもの**を
    消す副作用を持っていた。既定フォント（leading==0）でもこの分岐
    （`SingleHeight`）を必ず通るため、色/整列/太字などパネル操作 1 回ごとに、
    それまでタイプした文字の Ctrl+Z による取り消しができなくなっていた。
    """
    doc = Document()
    obj = TextObject(
        id=doc.new_id(),
        text="Hello",
        x=0.0,
        y=0.0,
        width=200.0,
        height=100.0,
        font_size=18.0,
        color="#000000",
        align="left",
        bold=False,
    )
    scene = CanvasScene(doc)
    stack = QUndoStack()
    scene.set_undo_stack(stack)
    stack.push(AddObjectCommand(scene.document, obj))
    item = scene.item_for(obj)
    assert isinstance(item, TextItem)

    assert item.begin_text_edit() is True
    editor = item._editor
    assert editor is not None

    cursor = editor.textCursor()
    cursor.movePosition(QTextCursor.MoveOperation.End)
    cursor.insertText(" world")
    assert editor.document().isUndoAvailable() is True, "前提: タイプ直後は undo 可能であること"

    stack.push(SetPropertyCommand(doc, obj, "color", "#ff0000", "#000000"))
    stack.push(SetPropertyCommand(doc, obj, "align", "center", "left"))
    stack.push(SetPropertyCommand(doc, obj, "bold", True, False))

    assert (
        editor.document().isUndoAvailable() is True
    ), "スタイル変更のたびにエディタ内蔵 undo 履歴が消えている(round-2 #3 の再発)"
    editor.document().undo()
    assert editor.toPlainText() == "Hello", "エディタ内蔵 undo でタイプした文字が取り消せること"
    scene.close()


def test_font_family_change_during_edit_still_applies_leading_compensation(
    qapp: Any, any_leading_font_family: str
) -> None:
    """編集中にフォントを leading==0 → leading>0 へ切り替えると、行高補正
    （`LineDistanceHeight`）が正しく適用されること（round-2 #3 の「変更が無
    ければ何もしない」ガードが、本当に変更があるときまで誤ってスキップして
    いないことの確認）。leading が実際に変わるこのケースでは内蔵 undo 履歴が
    消えるのが意図どおり（#5-a が直した行間ずれを、ユーザーの Ctrl+Z で
    フォントはそのままに書式だけ巻き戻して呼び戻さないため）。
    """
    doc = Document()
    obj = TextObject(
        id=doc.new_id(), text="abc", x=0.0, y=0.0, width=200.0, height=200.0, font_size=18.0
    )
    scene = CanvasScene(doc)
    stack = QUndoStack()
    scene.set_undo_stack(stack)
    stack.push(AddObjectCommand(scene.document, obj))
    item = scene.item_for(obj)
    assert isinstance(item, TextItem)

    assert item.begin_text_edit() is True
    editor = item._editor
    assert editor is not None
    fmt0 = editor.document().findBlockByNumber(0).blockFormat()
    assert (
        fmt0.lineHeightType() == QTextBlockFormat.LineHeightTypes.SingleHeight.value
    ), "前提: 既定フォント(leading==0)は SingleHeight のままであること"

    stack.push(
        SetPropertyCommand(doc, obj, "font_family", any_leading_font_family, obj.font_family)
    )

    fmt1 = editor.document().findBlockByNumber(0).blockFormat()
    assert (
        fmt1.lineHeightType() == QTextBlockFormat.LineHeightTypes.LineDistanceHeight.value
    ), "leading>0 フォントへ切り替えたのに行高補正(LineDistanceHeight)が適用されていない"
    metrics = QFontMetricsF(editor.font())
    lead = metrics.leading()
    natural_h = metrics.ascent() + metrics.descent()
    comp = math.ceil(natural_h + lead - 1e-9) - math.ceil(natural_h - 1e-9)
    assert fmt1.lineHeight() == pytest.approx(
        float(-comp)
    ), "丸め込み対応の式(round-2 #4/#7)通りの補正量であること"
    scene.close()


def test_external_text_change_during_edit_cancels_without_overwriting_draft(qapp: Any) -> None:
    """編集中に本文 (`text`) が外部から変わった場合は、エディタの下書きで上書き
    確定せず、そのままキャンセルする（所見2）。undo は 1 ステップのみ増える。
    """
    doc = Document()
    obj = TextObject(
        id=doc.new_id(), text="サンプル\nテキスト", x=0.0, y=0.0, width=150.0, height=80.0
    )
    scene = CanvasScene(doc)
    stack = QUndoStack()
    scene.set_undo_stack(stack)
    stack.push(AddObjectCommand(scene.document, obj))
    item = scene.item_for(obj)
    assert isinstance(item, TextItem)

    assert item.begin_text_edit() is True
    item._editor.setPlainText("パネルで書き換えた")

    idx_before = stack.index()
    stack.push(SetPropertyCommand(doc, obj, "text", "外部から書き換え", "サンプル\nテキスト"))

    assert item._text_edit_mode is False, "外部からの本文変更で編集は打ち切られること"
    assert item._editor is None
    assert (
        obj.text == "外部から書き換え"
    ), "エディタの下書きで上書き確定してはいけない（外部変更をそのまま反映する）"
    assert stack.index() == idx_before + 1, "余計な undo ステップが積まれないこと"
    scene.close()


def test_edit_mode_parity_with_fallback_font(qapp: Any) -> None:
    """欧文フォント指定 + 日本語（CJK グリフはフォールバックフォントで拾われる）でも一致する。

    行の実 ascent（`QTextLine.ascent()`、フォールバック込みで膨らむ）がプライマリ
    フォントの `QFontMetricsF.ascent()` と食い違う構成。ベースラインを固定 ascent で
    計算していた実装では編集中に本文が数 px 飛んだ（レビュー所見1の退行の再現条件。
    `text_outline._layout_lines` が行ごとの ascent を返すことで解消）。
    """
    scene, item, obj = _make_scene_item(_MULTILINE_JA, width=220.0, height=220.0)
    obj.font_family = "DejaVu Sans"
    item.sync_from_model()

    normal = _render_scene(scene, _RENDER_RECT, _RENDER_SIZE, _RENDER_SIZE)
    assert int(np.count_nonzero(normal[..., 3] > 0)) > 0

    assert item.begin_text_edit() is True
    item._editor.clearFocus()
    editing = _render_scene(scene, _RENDER_RECT, _RENDER_SIZE, _RENDER_SIZE)

    frac = _mismatch_fraction(normal, editing)
    assert frac < 0.005, f"fallback font: mismatch={frac:.4%} (threshold 0.5%)"


# --------------------------------------------------------------------------
# 10. IME 変換中の文字列の確定（altkey.md §3・B-4）
# --------------------------------------------------------------------------


def _set_preedit(editor: Any, preedit: str) -> None:
    """`editor` に変換中の文字列（preedit）を置く（まだ確定しない）。

    `QApplication.sendEvent` ではなく `inputMethodEvent()` を直接呼ぶ（実測:
    フォーカスの無いシーンでは `sendEvent` がイベントフィルタ止まりで届かない
    ことがある）。
    """
    editor.inputMethodEvent(QInputMethodEvent(preedit, []))


def _make_undoable_item(
    text: str, *, width: float = 160.0, height: float = 60.0
) -> tuple[CanvasScene, TextItem, TextObject]:
    """undo_stack 付きの `CanvasScene`（`_make_scene_item` の素の `QGraphicsScene` と違い、
    `commit_text` が実際にモデルへ反映される。`commit_text` は undo_stack が無いと
    `sync_from_model()` するだけの no-op になるため、本文がモデルに書き込まれる
    ことを検証する IME テストにはこちらが要る）。
    """
    doc = Document()
    obj = TextObject(id=doc.new_id(), text=text, x=0.0, y=0.0, width=width, height=height)
    scene = CanvasScene(doc)
    stack = QUndoStack()
    scene.set_undo_stack(stack)
    stack.push(AddObjectCommand(scene.document, obj))
    item = scene.item_for(obj)
    assert isinstance(item, TextItem)
    return scene, item, obj


def test_commit_text_edit_flushes_pending_preedit(qapp: Any) -> None:
    """`commit_text_edit()` 直接呼び出しで、変換中の文字列を破棄せず確定する。"""
    scene, item, obj = _make_undoable_item("abc")
    assert item.begin_text_edit() is True
    editor = item._editor
    _set_preedit(editor, "にほん")
    preedit = editor.textCursor().block().layout().preeditAreaText()
    assert preedit == "にほん", "前提: preedit が置かれていること"

    item.commit_text_edit()

    assert obj.text == "abcにほん"
    scene.close()


def test_ctrl_enter_flushes_pending_preedit(qapp: Any) -> None:
    """Ctrl+Enter による確定でも、変換中の文字列を破棄せず確定する。"""
    scene, item, obj = _make_undoable_item("abc")
    assert item.begin_text_edit() is True
    editor = item._editor
    _set_preedit(editor, "にほん")

    enter_event = QKeyEvent(
        QEvent.Type.KeyPress, Qt.Key.Key_Return, Qt.KeyboardModifier.ControlModifier
    )
    editor.keyPressEvent(enter_event)

    assert obj.text == "abcにほん"
    scene.close()


def test_cancel_text_edit_discards_pending_preedit(qapp: Any) -> None:
    """キャンセルは変換中の文字列も含めて破棄する（モデル不変）。"""
    scene, item, obj = _make_undoable_item("abc")
    assert item.begin_text_edit() is True
    editor = item._editor
    _set_preedit(editor, "にほん")

    item.cancel_text_edit()

    assert obj.text == "abc"
    scene.close()


# --------------------------------------------------------------------------
# 11. 全選択→IME変換/Backspace でキャレット行が飛ばない（review 所見2）
# --------------------------------------------------------------------------


@pytest.mark.parametrize("valign", ["top", "middle", "bottom"])
def test_editor_does_not_jump_on_select_all_then_ime_preedit(qapp: Any, valign: str) -> None:
    """既定テキスト「テキスト」を全選択して IME 変換を始めても、エディタの y が
    飛ばない（所見2: `toPlainText()` は preedit を含まないため、全選択直後の
    最初の preedit イベントで一瞬 `""` になる。`_sync_layout` が probe（空なら
    `" "`）ではなく素の `""` で `valign_offset`/`text_top_trim` を計算していたと、
    箱の高さ規則（B-1: 空テキストは 1 文字分 `" "` で測る）と食い違う offset/trim
    になり、キャレット行が上下に飛んで見えた（既定 valign が middle になった
    2026-09-25 で顕在化。top でも trim 分だけ飛ぶ）。
    """
    scene, item, obj = _make_scene_item("テキスト", valign=valign, width=100.0, height=28.29)
    assert item.begin_text_edit() is True
    editor = item._editor
    y0 = editor.y()

    cursor = editor.textCursor()
    cursor.select(QTextCursor.SelectionType.Document)
    editor.setTextCursor(cursor)
    # 前提: 全選択直後の preedit イベントで選択が消え、生テキストが空になること
    # （実機の QWidgetTextControl と同じ挙動。この前提が崩れると本テストは
    # フィックス無しでも無条件に通ってしまう）。
    _set_preedit(editor, "にほん")
    assert editor.toPlainText() == "", "前提: 全選択+preeditで生テキストが空になること"
    assert abs(editor.y() - y0) < 0.5, f"valign={valign}: IME変換開始でキャレット行が飛んだ"


@pytest.mark.parametrize("valign", ["top", "middle", "bottom"])
def test_editor_does_not_jump_on_select_all_then_backspace(qapp: Any, valign: str) -> None:
    """全選択→Backspace でも同様に飛ばない（IME を使わない経路。所見2）。"""
    scene, item, obj = _make_scene_item("テキスト", valign=valign, width=100.0, height=28.29)
    assert item.begin_text_edit() is True
    editor = item._editor
    y0 = editor.y()

    cursor = editor.textCursor()
    cursor.select(QTextCursor.SelectionType.Document)
    editor.setTextCursor(cursor)
    backspace_event = QKeyEvent(
        QEvent.Type.KeyPress, Qt.Key.Key_Backspace, Qt.KeyboardModifier.NoModifier
    )
    editor.keyPressEvent(backspace_event)
    assert editor.toPlainText() == "", "前提: 全選択+Backspaceで空になること"
    assert abs(editor.y() - y0) < 0.5, f"valign={valign}: Backspaceでキャレット行が飛んだ"


def test_editor_does_not_jump_when_typing_into_empty_text(qapp: Any) -> None:
    """本文が最初から空の text を編集開始し、1 文字目を打っても y が飛ばない
    （所見2 (c): 箱の高さは `fitted_text_height("", font, w)` で作られている
    ため、空の間も 1 文字分 `" "` の probe と一致していること）。
    """
    from app.scene.items.text_item import fitted_text_height

    font = font_for(TextObject(id=0, text="", font_size=18.0))
    height = fitted_text_height("", font, 100.0)
    scene, item, obj = _make_scene_item("", valign="middle", width=100.0, height=height)
    assert item.begin_text_edit() is True
    editor = item._editor
    y_empty = editor.y()

    editor.textCursor().insertText("a")
    assert abs(editor.y() - y_empty) < 0.5, "1 文字目を打った直後にキャレット行が飛んだ"


# --------------------------------------------------------------------------
# 12. leading>0 のフォント（Type 1 の Utopia 等）でも編集中/非編集が一致する
#    （review 所見5-a。req14 とは無関係の既存バグだが、フォントドロップダウン
#    （要望1）で一段踏みやすくなったため低リスクな範囲で直す）
# --------------------------------------------------------------------------


def _ink_row_range(arr: np.ndarray, *, alpha_threshold: int = 60) -> tuple[int, int] | None:
    """`arr`（RGBA, HxWx4）でアルファが閾値を超える行の最初/最後の index。

    インクが無ければ None。全画素一致率（`_mismatch_fraction`、閾値 0.5%）は
    サイズの小さいテキストの行単位のわずかなズレを見逃すため（round-2 #4
    Part1(d)・#7）、インクの上端/下端の行そのものを別に比較する。
    """
    rows = np.where(np.any(arr[..., 3] > alpha_threshold, axis=1))[0]
    if rows.size == 0:
        return None
    return int(rows[0]), int(rows[-1])


def _mismatch_for_family(
    family: str, text: str, *, valign: str = "middle", font_size: float = 18.0
) -> float:
    """`family`/`text`/`valign` で編集中/非編集の見た目を比較し、不一致率を返す。

    全画素一致率に加えて、インク上端/下端の行が ±1px 以内で一致することも
    ここで assert する（round-2 #4 Part1(d)）。行単位の判定は全画素一致率の
    0.5% 閾値より厳しく、小さいテキストの数px ずれを見逃さない。
    """
    obj = TextObject(
        id=1,
        text=text,
        x=0.0,
        y=0.0,
        width=200.0,
        height=200.0,
        font_family=family,
        font_size=font_size,
        valign=valign,
    )
    scene = QGraphicsScene()
    item = TextItem(obj)
    scene.addItem(item)
    item.sync_from_model()
    rect = QRectF(-10.0, -10.0, 220.0, 220.0)
    normal = _render_scene(scene, rect, 220, 220)
    assert item.begin_text_edit() is True
    item._editor.clearFocus()
    editing = _render_scene(scene, rect, 220, 220)
    item.cancel_text_edit()

    normal_rows = _ink_row_range(normal)
    editing_rows = _ink_row_range(editing)
    if normal_rows is not None and editing_rows is not None:
        assert abs(normal_rows[0] - editing_rows[0]) <= 1, (
            f"{family} {text!r} valign={valign}: インク上端の行が normal={normal_rows[0]} "
            f"editing={editing_rows[0]} で 1px を超えてずれている"
        )
        assert abs(normal_rows[1] - editing_rows[1]) <= 1, (
            f"{family} {text!r} valign={valign}: インク下端の行が normal={normal_rows[1]} "
            f"editing={editing_rows[1]} で 1px を超えてずれている"
        )
    return _mismatch_fraction(normal, editing)


_LEADING_PROBE_TEXTS = (
    "line one\nline two",
    "日本語のテキストです。\n複数行にわたる内容を確認する。\nテスト文字列。",
)


def _leading_font_families(*, size: float = 18.0) -> list[str]:
    """`leading > 0` の実インストール済みフォントを**パリティの結果を見ずに**列挙する。

    round-2 #4/#7: 旧実装（`_find_leading_font_family`）はここで
    `_mismatch_for_family(...) < 0.005` を満たすものだけを候補にしていた。
    これは「テストが検査したい補正そのもの」で候補を絞り込む自己選択で、
    `_apply_leading_compensation` が壊れて誰もパリティに達さなくなると
    候補が 0 件になり、依存する全テストが黙って skip（green のまま）になって
    しまっていた。ここでは `QFontMetricsF.leading() > 0` という機械的な条件
    だけで列挙し、パリティが取れるかどうかは呼び出し側（テスト本体）で
    検査させる。
    """
    families: list[str] = []
    for family in sorted(QFontDatabase.families()):
        # `font_for`（本番と同じ経路）を通す。`QFont(family); setPixelSize(size)`
        # 直書きだと、`font_for` のポイントサイズ→ピクセルサイズ解決
        # （`font_for` の docstring 参照）で実際に使われるピクセルサイズと
        # ずれ、ここでの leading 判定と `_apply_leading_compensation` が見る
        # leading が食い違うことがある（実測で判明）。
        probe_font = font_for(TextObject(id=0, font_family=family, font_size=size))
        if QFontMetricsF(probe_font).leading() <= 0:
            continue
        if QFontInfo(probe_font).family() != family:
            # 未インストールで別ファミリへ解決された（エイリアス等）。
            continue
        families.append(family)
    return families


#: `test_leading_gt_zero_font_editor_parity` が対象にする固定の候補集合
#: （round-2 #7 Part1(b)）。`Utopia`/`Courier` は leading が整数（旧式でも
#: 一致する対照）、`FreeSans`/`FreeSerif` は leading が端数の一般的な本文用
#: フォント（新しい丸め込み対応の式でないと一致しない）。`EB Garamond 12
#: All SC`/`Gillius ADF` は和文段落プローブでだけ既知の限界（CJK フォールバック
#: で行の実 ascent/descent がこのフォント自身の値と食い違う。`_apply_leading_
#: compensation` の docstring 参照）に当たる代表例として意図的に含め、
#: `xfail(strict=False)` で固定する（後述）。インストールされていないフォントは
#: `test_leading_gt_zero_font_editor_parity` 内で `pytest.skip` する
#: （これは「パリティの自己選択」ではなく「環境にそのフォントが無い」という
#: 正当な環境ゲートであり、#7 が問題にした自己選択とは別物）。
_LEADING_TEST_FAMILIES: tuple[str, ...] = (
    "Utopia",
    "Courier",
    "FreeSans",
    "FreeSerif",
    "EB Garamond 12 All SC",
    "Gillius ADF",
)

#: 上記のうち、和文段落プローブ（`_LEADING_PROBE_TEXTS[1]`）でだけパリティに
#: 達しないことが実測で分かっているフォント（round-2 #7 Part2 のブロック単位
#: 定数という設計の既知の限界。欧文のみのプローブは一致する）。値は環境依存
#: （インストールされているフォント次第・フォールバック用にどの CJK フォントが
#: 入っているかは distro/fontconfig 設定次第）で、このリポジトリの開発環境では
#: 実測でこの 3 種が該当した（`FreeSerif` は §1(b) の「必ず含める」候補だが、
#: 和文段落プローブでは CJK フォールバックにより不一致 mismatch=10.1% が実測
#: された）。他環境でこれらが未インストールなら該当テストは skip され、xfail
#: は評価されない。
#:
#: `strict=False`（2026-09-25 レビュー3巡目 finding「CJK-fallback フォント名の
#: 固定リストがポータブルでない」対応。以前は `strict=True` だった）: この
#: リストはこの開発機で実測して選んだものなので、フォールバック構成が異なる
#: 別機では (a) ここに挙げた3種が実はパリティに達して xfail が
#: **unexpectedly passing**（xpass）になる可能性、(b) 逆にここに無い
#: フォント（例 FreeSans）が別機のフォールバック構成では和文プローブで
#: 不一致になり、xfail マークの無い素の assert が落ちる可能性、の両方が
#: ある。`strict=False` は (a) を「テストの意味が壊れたわけではない」
#: ものとして黙って許容する（xfail のまま扱い、テストスイート全体を赤く
#: しない）。(b) は名前リストを実測なしで拡張しても解決しない残存リスクで、
#: CI が別機で赤くなったら実測してこのリストを更新すること
#: （`notes_for_user` にも記載）。
_KNOWN_CJK_FALLBACK_MISMATCH: frozenset[str] = frozenset(
    {"EB Garamond 12 All SC", "Gillius ADF", "FreeSerif"}
)


def _leading_family_text_cases() -> list[Any]:
    cases: list[Any] = []
    for family in _LEADING_TEST_FAMILIES:
        for text_index, text in enumerate(_LEADING_PROBE_TEXTS):
            probe_label = "latin" if text_index == 0 else "ja"
            marks = []
            if family in _KNOWN_CJK_FALLBACK_MISMATCH and text_index == 1:
                marks.append(
                    pytest.mark.xfail(
                        reason=(
                            f"{family}: 和文段落でこの行が CJK フォールバックへ落ち、"
                            "実 ascent/descent が本来のフォントと変わるため "
                            "ブロック単位の定数では追えない（round-2 #7 Part2 既知の限界）"
                        ),
                        strict=False,
                    )
                )
            cases.append(pytest.param(family, text, id=f"{family}-{probe_label}", marks=marks))
    return cases


_LEADING_FAMILY_TEXT_CASES = _leading_family_text_cases()


@pytest.mark.parametrize("family,text", _LEADING_FAMILY_TEXT_CASES)
@pytest.mark.parametrize("valign", ["top", "middle", "bottom"])
def test_leading_gt_zero_font_editor_parity(qapp: Any, family: str, text: str, valign: str) -> None:
    """leading>0 のフォントでも編集中/非編集の見た目が一致する（review 所見5-a、
    round-2 #4/#7 でフォント選択の自己選択を撤廃）。

    `_apply_leading_compensation` が無いと、`QTextDocumentLayout` は
    `_layout_lines` より 1 行につき広く（leading を上乗せして）送るため、
    行を追うごとにずれが蓄積し、行数の多いテキストほど大きく食い違っていた。
    """
    if family not in _leading_font_families():
        pytest.skip(f"{family} はこの環境にインストールされていない")
    frac = _mismatch_for_family(family, text, valign=valign)
    assert frac < 0.005, f"font={family} valign={valign}: mismatch={frac:.4%} (threshold 0.5%)"


def test_leading_parity_test_detects_missing_compensation(
    qapp: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    """`_mismatch_for_family` が leading 補正の欠落を実際に検出できることの陰性対照
    （round-2 #7 A.4）。`_apply_leading_compensation` を no-op にすると、
    非整数 leading のフォント（FreeSans）で明確に閾値を超えることを固定する
    ――旧実装の自己選択フィルタでは、これが起きても候補が単に 0 件になって
    依存する全テストが skip（見かけ上 green）になっていた。
    """
    family = "FreeSans"
    if family not in _leading_font_families():
        pytest.skip(f"{family} はこの環境にインストールされていない")

    from app.scene.items.text_editor_item import TextEditorItem

    monkeypatch.setattr(TextEditorItem, "_apply_leading_compensation", lambda self: None)
    # `_mismatch_for_family` はインク行の ±1px 一致か全画素不一致率 0.5% 未満の
    # どちらか早い方で検出する。どちらで捕まっても「検査が機能している」ことの
    # 証明としては同値なので、例外が飛ぶこと自体を確認する
    # （既定の valign="middle" は上下対称に崩れて誤って一致することがあるため、
    #  非対称に崩れる valign="top" を使う）。
    with pytest.raises(AssertionError):
        _mismatch_for_family(family, _LEADING_PROBE_TEXTS[1], valign="top")


@pytest.fixture
def any_leading_font_family() -> str:
    """leading>0 のフォントを 1 つ（パリティの結果を見ずに選ぶ。round-2 #7 Part1(c)）。

    `test_leading_compensation_block_format_survives_enter_and_paste` は
    ブロック書式の継承だけを見るテストで、どのフォントでパリティが取れるかとは
    無関係のため、パリティで絞り込まれていた旧 `leading_font_family` に依存
    させない。
    """
    families = _leading_font_families()
    if not families:
        pytest.skip("leading>0 のフォントがこの環境に見つからない")
    return families[0]


def test_leading_compensation_block_format_survives_enter_and_paste(
    qapp: Any, any_leading_font_family: str
) -> None:
    """Enter で増える段落も、貼り付け（Ctrl+V）で増える段落も、直前の段落の
    leading 補正書式（`LineDistanceHeight`）を引き継ぐこと。`QTextCursor` の
    既定挙動（新しいブロックは直前のブロック書式を継承する）に乗る設計であり、
    `_apply_leading_compensation` はキー入力のたびではなく `begin_text_edit`/
    `sync_from_model` でしか呼ばない（毎キー入力で呼ぶ必要が無いことの裏付け）。
    """
    obj = TextObject(
        id=1,
        text="abc",
        x=0.0,
        y=0.0,
        width=200.0,
        height=200.0,
        font_family=any_leading_font_family,
        font_size=18.0,
    )
    scene = QGraphicsScene()
    item = TextItem(obj)
    scene.addItem(item)
    item.sync_from_model()
    assert item.begin_text_edit() is True
    editor = item._editor

    cursor = editor.textCursor()
    cursor.movePosition(QTextCursor.MoveOperation.End)
    editor.setTextCursor(cursor)
    enter_event = QKeyEvent(QEvent.Type.KeyPress, Qt.Key.Key_Return, Qt.KeyboardModifier.NoModifier)
    editor.keyPressEvent(enter_event)

    clipboard = QGuiApplication.clipboard()
    clipboard.setText("a\nb\nc")
    paste_event = QKeyEvent(QEvent.Type.KeyPress, Qt.Key.Key_V, Qt.KeyboardModifier.ControlModifier)
    editor.keyPressEvent(paste_event)

    document = editor.document()
    assert document.blockCount() >= 4, "前提: Enter+貼り付けで複数ブロックに分かれていること"
    for i in range(document.blockCount()):
        fmt = document.findBlockByNumber(i).blockFormat()
        assert (
            fmt.lineHeightType() == QTextBlockFormat.LineHeightTypes.LineDistanceHeight.value
        ), f"block {i} が leading 補正の書式を引き継いでいない"


def test_begin_text_edit_starts_with_no_document_undo_history(qapp: Any) -> None:
    """`begin_text_edit()` 直後、ドキュメント自身の内部 undo 履歴
    （`QTextDocument`/`QWidgetTextControl` の Ctrl+Z。アプリの `QUndoStack` とは
    別物）が空であること（review 所見5-a: `_apply_leading_compensation` の
    `setUndoRedoEnabled(False)`→`True` の往復が、まだ何も編集していない時点の
    「空の履歴」を初期化するだけで、新たな undo ステップを作らないことの確認。
    既定フォント（leading==0）でも `SingleHeight` 分岐を必ず通るため、フォント
    指定なしで確認できる）。
    """
    scene, item, _obj = _make_scene_item("hello", width=200.0, height=200.0)
    assert item.begin_text_edit() is True
    assert item._editor.document().availableUndoSteps() == 0
