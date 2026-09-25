"""stroke null 化・画面/SVG ストローク整合のテスト（P2 契約 §担当C・項目12「矩形の線を
なくせるようにしてほしい」）。

このファイルは 2 波に分けて追記される契約になっている:

- **前半（このセクション）は担当 C1（モデル/描画/書き出し側）が書く。** 対象は
  `app.graphics.strokes`（is_stroked/stroke_margin 単体）・画面描画
  （`pen_for`・rect/ellipse/line/freehand/curve の `boundingRect`）・
  SVG シリアライズ（`_stroke_attrs` 経由の `<rect>`/`<ellipse>`/`<path>`）・
  モデルの to_dict/from_dict（JSON）往復・診断（`app.agent.diagnose`）が
  `stroke=None` で落ちないこと、の 4 点に限る。
- **後半は担当 C2（プロパティパネル・agent API）が追記する。** 色ウィジェット
  （nullable メニュー版）・`update_objects`/`apply_style` の nullable 検証・
  style memory への `stroke=None` 伝播は、いずれも
  `app/model/properties.py` の `stroke` の kind 変更（"color" →
  "color_opt"、C2 の担当）が前提になる。C1 完了時点ではまだ kind が
  "color" のままなので、`update_objects` に `stroke: null` を送ると
  （rect であっても）`nullable=False` により `type_mismatch` になる
  （これは C2 の kind 変更が入ってから解消される、想定どおりの一時的な状態）。
  契約に載っている C2 側のテスト名（参照用）:
  `test_stroke_is_nullable_only_for_filled_shapes` /
  `test_color_opt_button_has_none_and_pick_actions` /
  `test_color_opt_none_action_sets_null_in_one_undo` /
  `test_color_opt_pick_color_uses_non_native_dialog` /
  `test_plain_color_row_has_no_menu` /
  `test_mask_color_none_action_uses_the_transparent_label` /
  `test_multi_selection_edits_fill_via_color_opt_menu` /
  `test_multi_rect_and_line_share_stroke_row_as_plain_color` /
  `test_update_objects_accepts_stroke_null_for_rect_and_rejects_for_line` /
  `test_apply_style_stroke_none_to_mixed_types_rejects_whole_call` /
  `test_style_memory_carries_stroke_none_to_next_shape`
  （C2 追記済み。実体は下の「担当 C2」節を参照）。
"""

from __future__ import annotations

import json
import xml.etree.ElementTree as ET
from typing import Any

import numpy as np
import pytest
import shiboken6
from PySide6.QtCore import Qt
from PySide6.QtGui import QImage, QPainter

from app.agent import diagnose, schema
from app.agent.api import AgentAPI
from app.agent.validate import AgentError
from app.commands.commands import AddObjectCommand
from app.export.png_exporter import render_artboard_image
from app.export.svg_exporter import document_to_svg
from app.graphics.strokes import is_stroked, stroke_margin
from app.model.document import Artboard, Document, Physical
from app.model.objects import (
    BaseObject,
    ConnectorObject,
    CurveObject,
    EllipseObject,
    FreehandObject,
    ImageObject,
    LineObject,
    RectObject,
    new_object,
)
from app.model.properties import PROPERTIES
from app.model.serialize import document_from_json, document_to_json
from app.scene.items.curve_item import CurveItem
from app.scene.items.freehand_item import FreehandItem
from app.scene.items.shape_item import LineItem, RectEllipseItem, pen_for
from app.ui.main_window import MainWindow
from app.ui.widgets import ColorSwatchButton, SimpleColorDialog

_SVG_NS = "http://www.w3.org/2000/svg"
_NS = {"svg": _SVG_NS}


def _small_artboard() -> Artboard:
    """PNG レンダリングを軽くするための小さいアートボード（既定 1920x1080 は不要に重い）。"""
    return Artboard(width_px=120, height_px=120, physical=Physical(width_mm=30.0, target_dpi=96))


def _find_object_element(svg: str, tag: str) -> ET.Element:
    """`svg` から `tag` の要素を 1 件だけ見つけて返す。

    アートボード背景も `<rect>` として出力される（stroke 属性を持たない）ため、
    `stroke` 属性を持つ要素だけに絞ることで背景と対象オブジェクトを区別する
    （`test_export_m4.py` の malicious_stroke テストと同じ絞り込み方）。
    """
    root = ET.fromstring(svg)
    candidates = [el for el in root.findall(f".//svg:{tag}", _NS) if "stroke" in el.attrib]
    assert len(candidates) == 1, f"tag={tag!r} の候補が {len(candidates)} 件（想定は1件）"
    return candidates[0]


def _render_item_pixels(item: Any, w: int = 120, h: int = 120) -> np.ndarray:
    """`item.paint` を直接 QImage に描画して RGBA numpy 配列で返す（非クラッシュも兼ねる）。"""
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


def _nonzero_alpha_count(arr: np.ndarray) -> int:
    return int(np.count_nonzero(arr[..., 3] > 0))


# ============================================================================
# 担当 C1（モデル/描画/書き出し） ここから
# ============================================================================

# --------------------------------------------------------------------------
# app.graphics.strokes 単体（is_stroked / stroke_margin）
# --------------------------------------------------------------------------


def test_is_stroked_true_for_a_normal_stroke() -> None:
    obj = RectObject(id=1, stroke="#000000", stroke_width=2.0)
    assert is_stroked(obj) is True


@pytest.mark.parametrize(
    "stroke,stroke_width",
    [(None, 2.0), ("#000000", 0.0), ("", 2.0), (None, 0.0)],
    ids=["stroke_none", "width_zero", "stroke_empty", "both"],
)
def test_is_stroked_false_when_stroke_none_or_width_zero(
    stroke: str | None, stroke_width: float
) -> None:
    obj = EllipseObject(id=1, stroke=stroke, stroke_width=stroke_width)
    assert is_stroked(obj) is False


def test_stroke_margin_is_half_the_stroke_width_when_stroked() -> None:
    obj = CurveObject(id=1, stroke="#123456", stroke_width=6.0)
    assert stroke_margin(obj) == pytest.approx(3.0)


@pytest.mark.parametrize("stroke,stroke_width", [(None, 4.0), ("#123456", 0.0)])
def test_stroke_margin_is_zero_when_unstroked(stroke: str | None, stroke_width: float) -> None:
    obj = RectObject(id=1, stroke=stroke, stroke_width=stroke_width)
    assert stroke_margin(obj) == 0.0


def test_stroke_margin_ignores_types_without_a_stroke_field() -> None:
    """`stroke`/`stroke_width` を持たない型（例: text）で落ちないこと
    （`getattr` フォールバックの安全網）。"""
    obj = BaseObject(id=1)
    assert is_stroked(obj) is False
    assert stroke_margin(obj) == 0.0


# --------------------------------------------------------------------------
# 画面（pen_for / boundingRect）
# --------------------------------------------------------------------------


@pytest.mark.parametrize(
    "stroke,stroke_width",
    [(None, 2.0), ("#ff0000", 0.0)],
    ids=["stroke_none", "stroke_width_zero"],
)
def test_pen_for_returns_nopen_when_unstroked(
    stroke: str | None, stroke_width: float, qapp: Any
) -> None:
    """stroke=None と stroke_width=0 のどちらでも NoPen になること。

    以前は `stroke_width=0` でも `QPen.setWidthF(0)` の cosmetic 1px ヘアラインが
    画面に残り、SVG（`_stroke_attrs`）は不可視になる食い違いがあった
    （既存バグの固定。CLAUDE.md §1「出力画質が最優先」の防波堤）。
    """
    obj = RectObject(
        id=1, x=0.0, y=0.0, width=40.0, height=30.0, stroke=stroke, stroke_width=stroke_width
    )
    pen = pen_for(obj)
    assert pen.style() == Qt.PenStyle.NoPen


def test_pen_for_still_strokes_a_normal_line(qapp: Any) -> None:
    """回帰していないことの確認: 通常のストロークは NoPen にならない。"""
    obj = LineObject(id=1, p1=[0.0, 0.0], p2=[10.0, 10.0], stroke="#000000", stroke_width=2.0)
    pen = pen_for(obj)
    assert pen.style() != Qt.PenStyle.NoPen


def test_bounding_rect_keeps_a_minimum_hit_band_when_unstroked_for_rect_and_ellipse(
    qapp: Any,
) -> None:
    """rect/ellipse: stroke=None・stroke_width=0 のどちらでも boundingRect は
    自然サイズに戻らず、_MIN_HIT_WIDTH(=8.0) 由来の 4.0 の下限マージンを持つこと。

    P2 契約時点では「線なしなら boundingRect は自然サイズ (0,0,w,h)」だったが、
    P4/P5 契約の項目11（塗りなし矩形の内部素通し）で「線なしでも掴める帯を
    残す」ことが要件になり、rect/ellipse の boundingRect はヒット判定帯の
    下限を確保するため常に 4.0 のマージンを持つよう変更された（P2 契約の
    ゼロマージン保証はここで撤回）。このマージンはヒット判定専用であり、
    SVG/PDF/PNG のインク境界には影響しない
    （`RectEllipseItem.ink_rect()` が実インク境界を別に返す。
    `app/ui/controllers/export_controller.py` の `selected_region()` は
    `ink_rect()` を使うため、選択範囲コピーの出力はこのマージンで膨らまない）。
    """
    stroked = RectEllipseItem(
        RectObject(id=1, x=0.0, y=0.0, width=40.0, height=30.0, stroke="#000000", stroke_width=10.0)
    )
    stroked_rect = stroked.boundingRect()
    assert stroked_rect.left() == pytest.approx(-5.0), "対照群: 通常のストロークは膨らむこと"

    cases = [
        RectEllipseItem(
            RectObject(id=2, x=0.0, y=0.0, width=40.0, height=30.0, stroke=None, stroke_width=10.0)
        ),
        RectEllipseItem(
            EllipseObject(
                id=3, x=0.0, y=0.0, width=40.0, height=30.0, stroke="#000000", stroke_width=0.0
            )
        ),
    ]
    for item in cases:
        rect = item.boundingRect()
        assert rect.left() == pytest.approx(-4.0)
        assert rect.top() == pytest.approx(-4.0)
        assert rect.width() == pytest.approx(40.0 + 8.0)
        assert rect.height() == pytest.approx(30.0 + 8.0)


def test_bounding_rect_has_no_stroke_margin_when_unstroked_for_freehand(qapp: Any) -> None:
    """freehand: 項目11の対象外（帯の下限を持たない）なので、P2 契約どおり
    stroke=None・stroke_width=0 で boundingRect が自然サイズ (0,0,w,h) のまま
    であること（未変更の回帰確認）。"""
    item = FreehandItem(
        FreehandObject(
            id=4,
            x=0.0,
            y=0.0,
            width=40.0,
            height=30.0,
            points=[[0.0, 0.0], [1.0, 1.0]],
            stroke="#000000",
            stroke_width=0.0,
        )
    )
    rect = item.boundingRect()
    assert rect.left() == pytest.approx(0.0)
    assert rect.top() == pytest.approx(0.0)
    assert rect.width() == pytest.approx(40.0)
    assert rect.height() == pytest.approx(30.0)


def test_curve_bounding_rect_margin_shrinks_when_unstroked(qapp: Any) -> None:
    """curve は Catmull-Rom のオーバーシュートで絶対座標を固定できないため、
    stroke あり/なしの相対比較で「線幅分の余白が無いこと」を確かめる。"""
    points = [[0.0, 0.0], [1.0, 1.0]]
    stroked = CurveItem(
        CurveObject(
            id=1,
            x=0.0,
            y=0.0,
            width=40.0,
            height=30.0,
            points=points,
            stroke="#000000",
            stroke_width=10.0,
        )
    )
    unstroked = CurveItem(
        CurveObject(
            id=2,
            x=0.0,
            y=0.0,
            width=40.0,
            height=30.0,
            points=points,
            stroke=None,
            stroke_width=10.0,
        )
    )
    stroked_rect = stroked.boundingRect()
    unstroked_rect = unstroked.boundingRect()
    assert unstroked_rect.width() < stroked_rect.width()
    assert unstroked_rect.height() < stroked_rect.height()


# --------------------------------------------------------------------------
# SVG（_stroke_attrs 経由）
# --------------------------------------------------------------------------


def test_stroke_none_rect_emits_stroke_none_and_no_width_in_svg(qapp: Any) -> None:
    doc = Document(artboard=_small_artboard())
    rect = RectObject(
        id=doc.new_id(),
        x=10.0,
        y=10.0,
        width=50.0,
        height=30.0,
        fill=None,
        stroke=None,
        stroke_width=3.0,
        dash="dash",
    )
    doc.add_object(rect)

    svg = document_to_svg(doc)
    el = _find_object_element(svg, "rect")
    assert el.get("stroke") == "none"
    assert "stroke-width" not in el.attrib
    assert "stroke-dasharray" not in el.attrib, "線なしなら dash='dash' でも dasharray を出さない"


def test_fill_survives_stroke_none_on_canvas_and_svg(qapp: Any) -> None:
    """項目12の主要シナリオ: 塗りありで線だけを消す（塗りなし線なしの退化ケースだけでなく、
    NoPen + ブラシ塗りで塗りそのものはちゃんと残ることを固定する）。"""
    obj = RectObject(
        id=1, x=0.0, y=0.0, width=40.0, height=30.0, fill="#ff0000", stroke=None, stroke_width=2.0
    )
    item = RectEllipseItem(obj)
    arr = _render_item_pixels(item)
    assert _nonzero_alpha_count(arr) > 0, "線なしでも塗りは画面に残ること"

    doc = Document(artboard=_small_artboard())
    doc.add_object(obj)
    svg = document_to_svg(doc)
    el = _find_object_element(svg, "rect")
    assert el.get("fill") == "#ff0000"
    assert el.get("stroke") == "none"


def _zero_width_rect() -> tuple[BaseObject, str]:
    return (
        RectObject(id=1, x=0.0, y=0.0, width=40.0, height=20.0, stroke="#123456", stroke_width=0.0),
        "rect",
    )


def _zero_width_ellipse() -> tuple[BaseObject, str]:
    return (
        EllipseObject(
            id=1, x=0.0, y=0.0, width=40.0, height=20.0, stroke="#123456", stroke_width=0.0
        ),
        "ellipse",
    )


def _zero_width_curve() -> tuple[BaseObject, str]:
    return (
        CurveObject(
            id=1,
            x=0.0,
            y=0.0,
            width=40.0,
            height=20.0,
            points=[[0.0, 0.0], [0.5, 1.0], [1.0, 0.0]],
            stroke="#123456",
            stroke_width=0.0,
        ),
        "path",
    )


def _zero_width_line() -> tuple[BaseObject, str]:
    return (
        LineObject(
            id=1,
            p1=[0.0, 0.0],
            p2=[40.0, 20.0],
            stroke="#123456",
            stroke_width=0.0,
            arrow_start="none",
            arrow_end="none",
        ),
        "path",
    )


def _zero_width_freehand() -> tuple[BaseObject, str]:
    return (
        FreehandObject(
            id=1,
            x=0.0,
            y=0.0,
            width=40.0,
            height=20.0,
            points=[[0.0, 0.0], [0.5, 0.5], [1.0, 1.0]],
            stroke="#123456",
            stroke_width=0.0,
        ),
        "path",
    )


def _zero_width_connector() -> tuple[BaseObject, str]:
    return (
        ConnectorObject(
            id=1,
            source_id=None,
            target_id=None,
            source_point=[0.0, 0.0],
            target_point=[40.0, 20.0],
            arrow_end="none",
            stroke="#123456",
            stroke_width=0.0,
        ),
        "path",
    )


_ZERO_WIDTH_BUILDERS = {
    "rect": _zero_width_rect,
    "ellipse": _zero_width_ellipse,
    "curve": _zero_width_curve,
    "line": _zero_width_line,
    "freehand": _zero_width_freehand,
    "connector": _zero_width_connector,
}


@pytest.mark.parametrize("type_name", sorted(_ZERO_WIDTH_BUILDERS))
def test_stroke_width_zero_emits_stroke_none_in_svg(type_name: str, qapp: Any) -> None:
    """既存バグの固定: stroke_width=0 は画面の cosmetic 1px ではなく SVG も不可視にする。

    line/freehand/connector は stroke が str のままだが、stroke_width=0 という
    既存の語彙だけで「線なし」を表現できる（stroke を null にできる必要はない）。
    """
    obj, tag = _ZERO_WIDTH_BUILDERS[type_name]()
    doc = Document(artboard=_small_artboard())
    doc.add_object(obj)

    svg = document_to_svg(doc)
    el = _find_object_element(svg, tag)
    assert el.get("stroke") == "none"
    assert "stroke-width" not in el.attrib
    assert "stroke-dasharray" not in el.attrib


# --------------------------------------------------------------------------
# 画面/PNG/SVG の三者整合
# --------------------------------------------------------------------------


def test_stroke_none_rect_draws_nothing_on_canvas(qapp: Any) -> None:
    """fill=None・stroke=None の矩形は画面(PNG)に何も描かない
    ＝ その矩形を含まない Document の PNG とバイト一致（画面/PNG/SVG 三者整合の一角）。
    """
    doc_with = Document(artboard=_small_artboard())
    doc_with.add_object(
        RectObject(
            id=doc_with.new_id(),
            x=20.0,
            y=20.0,
            width=60.0,
            height=40.0,
            fill=None,
            stroke=None,
            stroke_width=4.0,
        )
    )
    doc_without = Document(artboard=_small_artboard())

    image_with = render_artboard_image(doc_with)
    image_without = render_artboard_image(doc_without)
    assert (image_with.width(), image_with.height()) == (
        image_without.width(),
        image_without.height(),
    )

    buf_with = bytes(image_with.convertToFormat(QImage.Format.Format_RGBA8888).constBits())
    buf_without = bytes(image_without.convertToFormat(QImage.Format.Format_RGBA8888).constBits())
    assert buf_with == buf_without


def test_stroke_width_zero_line_is_invisible_on_canvas_and_svg(qapp: Any) -> None:
    obj = LineObject(
        id=1,
        p1=[10.0, 60.0],
        p2=[110.0, 60.0],
        stroke="#ff0000",
        stroke_width=0.0,
        arrow_start="none",
        arrow_end="none",
    )
    item = LineItem(obj)
    arr = _render_item_pixels(item)
    assert _nonzero_alpha_count(arr) == 0, "stroke_width=0 の線は画面にも描かれないこと"

    doc = Document(artboard=_small_artboard())
    doc.add_object(obj)
    svg = document_to_svg(doc)
    el = _find_object_element(svg, "path")
    assert el.get("stroke") == "none"


def test_arrowhead_triangle_survives_stroke_width_zero(qapp: Any) -> None:
    """三角矢じりは `obj.stroke` 由来の色でブラシ塗りされる（pen 経由ではない）ため、
    幅0でも画面・SVG のどちらでも消えない（P2 契約: パリティは保たれる、と明記された点）。
    """
    obj = LineObject(
        id=1,
        p1=[20.0, 60.0],
        p2=[100.0, 60.0],
        stroke="#000000",
        stroke_width=0.0,
        arrow_start="none",
        arrow_end="triangle",
        arrow_size=14.0,
    )
    item = LineItem(obj)
    arr = _render_item_pixels(item)
    assert _nonzero_alpha_count(arr) > 0, "矢じりは幅0でも画面に描かれること"

    doc = Document(artboard=_small_artboard())
    doc.add_object(obj)
    svg = document_to_svg(doc)
    root = ET.fromstring(svg)
    paths = root.findall(".//svg:path", _NS)
    # 矢じり(triangle)は fill=stroke の閉パス（"...Z" 終端）。線本体は fill="none"。
    triangle_paths = [
        p for p in paths if p.get("d", "").rstrip().endswith("Z") and p.get("fill") != "none"
    ]
    assert len(triangle_paths) == 1, "SVG にも矢じりのパスが1件あること"


# --------------------------------------------------------------------------
# シリアライズ・診断
# --------------------------------------------------------------------------


def test_serialize_roundtrip_preserves_stroke_none() -> None:
    doc = Document()
    rect = RectObject(id=doc.new_id(), fill=None, stroke=None)
    ellipse = EllipseObject(id=doc.new_id(), stroke=None)
    curve = CurveObject(id=doc.new_id(), points=[[0.0, 0.0], [1.0, 1.0]], stroke=None)
    doc.add_object(rect)
    doc.add_object(ellipse)
    doc.add_object(curve)

    payload = document_to_json(doc)
    # 辞書往復だけでなく実際に JSON テキストを経由させ、null が生き残ることを確かめる
    # （project.json は実際にこの経路でディスクへ出る）。
    restored = document_from_json(json.loads(json.dumps(payload)))

    restored_rect = restored.object_by_id(rect.id)
    restored_ellipse = restored.object_by_id(ellipse.id)
    restored_curve = restored.object_by_id(curve.id)
    assert restored_rect is not None and restored_rect.stroke is None
    assert restored_ellipse is not None and restored_ellipse.stroke is None
    assert restored_curve is not None and restored_curve.stroke is None


def test_critique_does_not_crash_with_unstroked_shapes(qapp: Any) -> None:
    """`app.graphics.diagnostics`/`legibility` は stroke を一度も読まないため
    stroke=None で例外は出ないはず（P2 契約の前提事実の固定）。"""
    doc = Document(artboard=_small_artboard())
    doc.add_object(
        RectObject(id=doc.new_id(), x=10.0, y=10.0, width=40.0, height=30.0, fill=None, stroke=None)
    )
    doc.add_object(
        EllipseObject(
            id=doc.new_id(), x=60.0, y=10.0, width=40.0, height=30.0, stroke=None, stroke_width=0.0
        )
    )
    doc.add_object(
        CurveObject(
            id=doc.new_id(),
            x=10.0,
            y=60.0,
            width=40.0,
            height=30.0,
            points=[[0.0, 0.0], [0.5, 1.0], [1.0, 0.0]],
            stroke=None,
        )
    )

    findings = diagnose.collect(doc)
    assert isinstance(findings, list)


# ============================================================================
# 担当 C1 ここまで。以降は担当 C2（プロパティパネル・agent API）が追記する。
# ============================================================================


# --------------------------------------------------------------------------
# フィクスチャ・ヘルパ（tests/test_panel_edit_m8.py / tests/test_panel_sections.py
# と同様の構成。プロパティパネル・AgentAPI を実際に駆動するテストはこの節でのみ使う）。
# --------------------------------------------------------------------------


@pytest.fixture
def window(qapp: Any) -> Any:
    w = MainWindow()
    w.resize(900, 700)
    w.show()
    yield w
    if shiboken6.isValid(w):
        w.close()


@pytest.fixture
def env(window: Any, qapp: Any) -> dict[str, Any]:
    return {
        "window": window,
        "scene": window.scene,
        "stack": window.undo_stack,
        "panel": window.property_panel,
        "app": qapp,
    }


def _select_only(env: dict[str, Any], obj: Any) -> None:
    scene = env["scene"]
    item = scene.item_for(obj)
    assert item is not None
    scene.clearSelection()
    item.setSelected(True)
    env["app"].processEvents()


def _add(env: dict[str, Any], obj: Any) -> Any:
    env["stack"].push(AddObjectCommand(env["scene"].document, obj))
    return obj


# --------------------------------------------------------------------------
# PROPERTIES / describe_schema: stroke が nullable なのは塗りを持つ図形だけ
# --------------------------------------------------------------------------


def test_stroke_is_nullable_only_for_filled_shapes() -> None:
    """rect/ellipse/curve（塗りを持つ図形）の stroke だけ "color_opt"/nullable。

    line/arrow/freehand/connector は「線そのものが実体」のオブジェクトなので
    stroke を null にすると「削除すべきものが図に残る」だけになる（すでに
    `visible` と `stroke_width=0` に "線を消す" 語彙がある）。P2契約 §担当C。
    """
    nullable_types = ("rect", "ellipse", "curve")
    plain_types = ("line", "arrow", "freehand", "connector")

    for type_name in nullable_types:
        spec = next(s for s in PROPERTIES[type_name] if s.key == "stroke")
        assert spec.kind == "color_opt", type_name
        props = schema.editable_keys(type_name)
        assert props["stroke"]["nullable"] is True, type_name

    for type_name in plain_types:
        spec = next(s for s in PROPERTIES[type_name] if s.key == "stroke")
        assert spec.kind == "color", type_name
        props = schema.editable_keys(type_name)
        assert props["stroke"].get("nullable") is not True, type_name


# --------------------------------------------------------------------------
# プロパティパネル: 色ボタン（color_opt は QMenu、color はメニュー無し）
# --------------------------------------------------------------------------


def test_color_opt_button_has_none_and_pick_actions(env: dict[str, Any]) -> None:
    """2026-09-25（要望8/11/12）: 色ウィジェットは `ColorSwatchButton` に統一した。
    メニューはパレット色（未選択なら基本色8色）＋「なし」＋「色を選択…」を
    末尾に持つ（`reports/color.md` §4）。旧テストの「アクションは2つ」という
    前提はパレット色の追加で崩れるため、固定位置での unpack ではなく
    「「なし」と「色を選択…」がこの順で末尾にある」ことを確認する。
    """
    scene = env["scene"]
    rect = _add(
        env,
        RectObject(
            id=scene.document.new_id(), x=0.0, y=0.0, width=40.0, height=30.0, fill="#FF0000"
        ),
    )
    _select_only(env, rect)

    button = env["panel"].field_widget_for("fill")
    assert isinstance(button, ColorSwatchButton)
    menu = button.menu()
    assert menu is not None
    texts = [a.text() for a in menu.actions() if not a.isSeparator()]
    assert texts[-2:] == ["なし", "色を選択…"]


def test_color_opt_none_action_sets_null_in_one_undo(env: dict[str, Any]) -> None:
    scene, stack, panel = env["scene"], env["stack"], env["panel"]
    rect = _add(
        env,
        RectObject(
            id=scene.document.new_id(), x=0.0, y=0.0, width=40.0, height=30.0, stroke="#000000"
        ),
    )
    _select_only(env, rect)

    button = panel.field_widget_for("stroke")
    index_before = stack.index()
    none_action = next(a for a in button.menu().actions() if a.text() == "なし")
    none_action.trigger()

    assert rect.stroke is None
    assert stack.index() == index_before + 1, "1 アクション = 1 undo エントリ"

    stack.undo()
    assert rect.stroke == "#000000"


def test_color_opt_pick_color_uses_the_simple_color_dialog(
    env: dict[str, Any], monkeypatch: pytest.MonkeyPatch
) -> None:
    """2026-09-25: `QColorDialog` はアプリから消え、色ダイアログの唯一の経路は
    `SimpleColorDialog.get_color`（要望12）。「色を選択…」アクションがこの
    1関数だけを通ることを、stroke（color_opt）側でも固定する（fill 側は
    `tests/test_panel_edit_m8.py::test_rect_fill_pick_action_opens_the_simple_color_dialog`）。
    """
    scene, panel = env["scene"], env["panel"]
    rect = _add(
        env,
        RectObject(
            id=scene.document.new_id(), x=0.0, y=0.0, width=40.0, height=30.0, stroke="#000000"
        ),
    )
    _select_only(env, rect)

    button = panel.field_widget_for("stroke")
    captured: dict[str, Any] = {}

    def _fake_get_color(*args: Any, **kwargs: Any) -> None:
        captured["called"] = True
        return None  # キャンセル相当 → 変更しない

    monkeypatch.setattr(SimpleColorDialog, "get_color", staticmethod(_fake_get_color))
    pick_action = next(a for a in button.menu().actions() if a.text() == "色を選択…")
    pick_action.trigger()

    assert captured.get("called") is True


@pytest.mark.parametrize(
    "obj_type,key,kwargs",
    [
        ("line", "stroke", {"p1": [0.0, 0.0], "p2": [10.0, 10.0]}),
        ("text", "color", {"x": 0.0, "y": 0.0, "width": 100.0, "height": 20.0}),
    ],
    ids=["line_stroke", "text_color"],
)
def test_plain_color_row_has_no_null_action(
    env: dict[str, Any], obj_type: str, key: str, kwargs: dict[str, Any]
) -> None:
    """kind="color"（null 不可）の行は `ColorSwatchButton` のメニュー自体は持つ
    （要望11: 全色行が同じ部品）が、「なし」アクションは出さない（一括で None
    を送れない対象への操作を UI 上提示しない）。2026-09-25 に
    `menu() is None` から反転した（`reports/color.md` §6 の既定の記載どおり）。
    """
    scene, panel = env["scene"], env["panel"]
    obj = _add(env, new_object(obj_type, id=scene.document.new_id(), **kwargs))
    _select_only(env, obj)

    button = panel.field_widget_for(key)
    assert isinstance(button, ColorSwatchButton)
    menu = button.menu()
    assert menu is not None
    assert "なし" not in [a.text() for a in menu.actions()]


def test_mask_color_none_action_uses_the_transparent_label(env: dict[str, Any]) -> None:
    """mask_color の null は「なし」ではなく「透明（切り取り）」（SAM3 §9.5 の操作の意味）。"""
    scene, panel = env["scene"], env["panel"]
    image = _add(
        env,
        ImageObject(
            id=scene.document.new_id(),
            x=0.0,
            y=0.0,
            width=50.0,
            height=50.0,
            src="assets/does_not_exist.png",
            mask_src="assets/mask_001.png",
        ),
    )
    _select_only(env, image)

    button = panel.field_widget_for("mask_color")
    menu = button.menu()
    assert menu is not None
    none_action = next(a for a in menu.actions() if a.text() == "透明（切り取り）")

    none_action.trigger()
    assert image.mask_color is None
    assert button.text() == "透明（切り取り）"


# --------------------------------------------------------------------------
# multi モード: color_opt の混在互換（rect+line で「線色」行を消さない）
# --------------------------------------------------------------------------


def test_multi_selection_edits_fill_via_color_opt_menu(env: dict[str, Any]) -> None:
    scene, app, panel = env["scene"], env["app"], env["panel"]
    r1 = _add(
        env,
        RectObject(
            id=scene.document.new_id(), x=0.0, y=0.0, width=40.0, height=30.0, fill="#FF0000"
        ),
    )
    r2 = _add(
        env,
        RectObject(
            id=scene.document.new_id(), x=50.0, y=0.0, width=40.0, height=30.0, fill="#00FF00"
        ),
    )
    scene.item_for(r1).setSelected(True)
    scene.item_for(r2).setSelected(True)
    app.processEvents()
    assert len(scene.selected_objects()) == 2

    button = panel.field_widget_for("fill")
    menu = button.menu()
    assert menu is not None
    none_action = next(a for a in menu.actions() if a.text() == "なし")
    none_action.trigger()

    assert r1.fill is None
    assert r2.fill is None


def test_multi_rect_and_line_share_stroke_row_as_plain_color(env: dict[str, Any]) -> None:
    """回帰の固定: rect（stroke=color_opt）+ line（stroke=color）を同時選択しても
    「線色」行が消えないこと（color/color_opt を互換扱いにし、実効 kind を狭い方
    =color に寄せる）。line が null を許容しないので、この行のメニューには
    「なし」アクションを出さない（一括で None を送る操作を UI 上提示しない。
    2026-09-25 に `menu() is None` から反転——メニュー自体は他の色行と同じ
    `ColorSwatchButton` が常に持つため）。
    """
    scene, app, panel = env["scene"], env["app"], env["panel"]
    rect = _add(env, RectObject(id=scene.document.new_id(), x=0.0, y=0.0, width=40.0, height=30.0))
    line = _add(env, LineObject(id=scene.document.new_id(), p1=[0.0, 0.0], p2=[10.0, 10.0]))
    scene.item_for(rect).setSelected(True)
    scene.item_for(line).setSelected(True)
    app.processEvents()
    assert len(scene.selected_objects()) == 2

    assert "stroke" in panel.keys_in_form(), "混在で「線色」行が消える回帰"
    button = panel.field_widget_for("stroke")
    assert isinstance(button, ColorSwatchButton)
    menu = button.menu()
    assert menu is not None
    assert "なし" not in [a.text() for a in menu.actions()]


# --------------------------------------------------------------------------
# AgentAPI: update_objects / apply_style の nullable 検証
# --------------------------------------------------------------------------


def test_update_objects_accepts_stroke_null_for_rect_and_rejects_for_line(window: Any) -> None:
    api = AgentAPI(window)
    rect_id = api.create_objects(
        [{"type": "rect", "x": 0.0, "y": 0.0, "width": 40.0, "height": 30.0}]
    )["created"][0]["id"]
    line_id = api.create_objects([{"type": "line", "p1": [0.0, 0.0], "p2": [10.0, 10.0]}])[
        "created"
    ][0]["id"]

    api.update_objects([{"id": rect_id, "stroke": None}])
    assert window.scene.document.object_by_id(rect_id).stroke is None

    with pytest.raises(AgentError) as excinfo:
        api.update_objects([{"id": line_id, "stroke": None}])
    assert excinfo.value.code == "validation_failed"
    assert excinfo.value.extra["errors"][0]["code"] == "type_mismatch"
    assert window.scene.document.object_by_id(line_id).stroke is not None


def test_apply_style_stroke_none_to_mixed_types_rejects_whole_call(window: Any) -> None:
    """不変条件3（P2契約・CLAUDE.md §15）: 全件検証をパスするまで1つも適用しない。
    rect（stroke=color_opt、null 許容）+ line（stroke=color、null 不許容）へ
    `style={"stroke": None}` を配ると、line 側の type_mismatch で呼び出し全体が
    拒否され、rect 側も変更されないこと（バッチの一部だけ適用される片手落ちを防ぐ）。
    """
    api = AgentAPI(window)
    rect_id = api.create_objects(
        [{"type": "rect", "x": 0.0, "y": 0.0, "width": 40.0, "height": 30.0}]
    )["created"][0]["id"]
    line_id = api.create_objects([{"type": "line", "p1": [0.0, 0.0], "p2": [10.0, 10.0]}])[
        "created"
    ][0]["id"]
    rect = window.scene.document.object_by_id(rect_id)
    line = window.scene.document.object_by_id(line_id)
    rect_stroke_before = rect.stroke
    line_stroke_before = line.stroke

    with pytest.raises(AgentError) as excinfo:
        api.apply_style(ids=[rect_id, line_id], style={"stroke": None})
    assert excinfo.value.code == "validation_failed"
    assert rect.stroke == rect_stroke_before, "呼び出し全体が拒否されるので rect も無変更"
    assert line.stroke == line_stroke_before


# --------------------------------------------------------------------------
# style memory（sticky defaults）: stroke=None も次のオブジェクトへ伝播する
# --------------------------------------------------------------------------


def test_style_memory_carries_stroke_none_to_next_shape(qapp: Any) -> None:
    """`ToolManager._style_memory` は dataclass fields ∩ `_STYLE_KEYS` を型別に
    記憶するだけの汎用機構で、値の型を特別扱いしない。stroke=None も
    fill=None（既存の「塗りなし」の粘り）と同じ挙動で次の同種オブジェクトへ
    伝播する（P2契約: 意図的挙動として受け入れる）。
    """
    from PySide6.QtCore import QPointF
    from PySide6.QtGui import QUndoStack

    from app.commands.commands import SetPropertyCommand
    from app.model.document import Document as _Document
    from app.scene.canvas_scene import CanvasScene
    from app.tools.tool_manager import ToolManager

    class _FakeEvent:
        def button(self) -> Qt.MouseButton:
            return Qt.MouseButton.LeftButton

    document = _Document()
    scene = CanvasScene(document)
    stack = QUndoStack()
    scene.set_undo_stack(stack)
    tm = ToolManager(scene)

    def _drag(tool: str, p1: QPointF, p2: QPointF) -> None:
        tm.set_tool(tool)
        tm.handle_mouse_press(_FakeEvent(), p1)
        tm.handle_mouse_move(_FakeEvent(), p2)
        tm.handle_mouse_release(_FakeEvent(), p2)

    _drag("rect", QPointF(10, 10), QPointF(110, 90))
    first = document.objects[0]
    stack.push(SetPropertyCommand(document, first, "stroke", None, first.stroke))
    assert first.stroke is None

    _drag("rect", QPointF(200, 200), QPointF(260, 240))
    second = document.objects[-1]
    assert second.stroke is None


# ============================================================================
# 担当 C2 ここまで。
# ============================================================================
