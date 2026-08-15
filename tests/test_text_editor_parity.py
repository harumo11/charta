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
from PySide6.QtCore import QEvent, QRectF, Qt
from PySide6.QtGui import QGuiApplication, QImage, QKeyEvent, QPainter, QTextCursor, QUndoStack
from PySide6.QtWidgets import QGraphicsScene

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
    # 所見6: 「両方とも真っ白で不一致0%」という偽陽性合格を防ぐため、通常描画に
    # 実際にインクがあることを主張する（フォント解決失敗を検出可能にする）。
    assert (
        int(np.count_nonzero(normal[..., 3] > 0)) > 0
    ), "通常描画が完全に透明（フォント解決失敗等で判別力が失われている疑い）"


def test_edit_mode_render_matches_normal_render_for_empty_text(qapp: Any) -> None:
    """パリティ本丸(1)に空テキストのケースを追加する（所見4: プレースホルダも
    編集中/非編集で完全一致することを固定する）。
    """
    scene, item, _obj = _make_scene_item("", width=100.0, height=60.0)

    normal = _render_scene(scene, _RENDER_RECT, _RENDER_SIZE, _RENDER_SIZE)

    assert item.begin_text_edit() is True
    item._editor.clearFocus()
    editing = _render_scene(scene, _RENDER_RECT, _RENDER_SIZE, _RENDER_SIZE)

    frac = _mismatch_fraction(normal, editing)
    assert frac < 0.005, f"空テキスト: mismatch={frac:.4%} (threshold 0.5%)"
    assert int(np.count_nonzero(normal[..., 3] > 0)) > 0, "プレースホルダ破線が描かれていない"


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
def test_known_mismatch_long_unbreakable_token_exceeding_box_width(qapp: Any, align: str) -> None:
    """既知の非一致を境界として固定する（契約オーナー判断待ち）。

    根本解決（break-anywhere で3経路すべてを揃える）は行分割の仕様変更であり、
    ユーザー判断が必要（レビュー所見1）。このテストは「解消していないこと」を
    下限つきで固定し、将来 break-anywhere を実装したら本テストを置き換える
    （下限が無いと閾値を緩めて事実上見なかったことにできてしまう）。
    """
    scene, item, _obj = _make_scene_item(
        "Supercalifragilisticexpialidocious",
        align=align,
        width=180.0,
        height=180.0,
        font_size=16.0,
    )
    render_rect = QRectF(-10.0, -10.0, 200.0, 200.0)
    normal = _render_scene(scene, render_rect, 200, 200)

    assert item.begin_text_edit() is True
    item._editor.clearFocus()
    editing = _render_scene(scene, render_rect, 200, 200)

    frac = _mismatch_fraction(normal, editing)
    assert 0.01 < frac < 0.10, (
        f"align={align}: mismatch={frac:.4%}（既知の非一致の想定範囲 1%〜10%から外れた。"
        "解消したなら本テストを更新/削除すること。悪化したなら要調査）"
    )


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
