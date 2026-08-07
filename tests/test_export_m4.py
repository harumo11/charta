"""app/export の統合テスト（M4契約 §9、offscreen）。

rect/ellipse/line/arrow/freehand/text/image を含む Document を temp project
（base_dir 設定 + PIL 画像）に用意し、PNG/PDF/SVG それぞれの契約を検証する。
"""

from __future__ import annotations

import re
import xml.etree.ElementTree as ET
from pathlib import Path
from typing import Any

import numpy as np
import pytest
from PIL import Image
from PySide6.QtGui import QFontInfo, QFontMetricsF

from app.export.pdf_exporter import export_pdf
from app.export.png_exporter import artboard_pixel_size, export_png
from app.export.svg_exporter import _build_text_font, document_to_svg, export_svg
from app.model.document import Artboard, Document, Physical
from app.model.objects import (
    EllipseObject,
    FreehandObject,
    ImageObject,
    LineObject,
    RectObject,
    TextObject,
)
from app.model.serialize import import_image, save_document

_SVG_NS = "http://www.w3.org/2000/svg"
_XLINK_NS = "http://www.w3.org/1999/xlink"
_NS = {"svg": _SVG_NS}


def _make_source_image(path: Path, w: int = 40, h: int = 30) -> None:
    x = np.linspace(0, 255, w, dtype=np.uint8)
    row = np.tile(x, (h, 1))
    rgb = np.stack([row, row, row], axis=-1).astype(np.uint8)
    Image.fromarray(rgb, mode="RGB").save(path)


@pytest.fixture
def project_dir(tmp_path: Path) -> Path:
    return tmp_path / "proj"


def _build_document(project_dir: Path, tmp_path: Path) -> Document:
    """全種別(rect/ellipse/line/arrow/freehand/text/image)を含む Document を作る。

    出力 PNG(295x221 程度)の四隅が背景のままであることをテストで確認できるよう、
    全オブジェクトは model 座標(800x600 アートボード)の中央寄りに配置する。
    """
    artboard = Artboard(
        width_px=800,
        height_px=600,
        physical=Physical(width_mm=50.0, target_dpi=150),
        background="#FFFFFF",
    )
    doc = Document(artboard=artboard)
    save_document(doc, project_dir)  # base_dir 設定 + assets/exports 作成

    src = tmp_path / "src.png"
    _make_source_image(src, w=40, h=30)
    rel = import_image(doc, str(src))

    rect = RectObject(
        id=doc.new_id(),
        x=60.0,
        y=60.0,
        width=150.0,
        height=80.0,
        fill="#FF0000",
        stroke="#111111",
        stroke_width=3.0,
        corner_radius=8.0,
    )
    ellipse = EllipseObject(
        id=doc.new_id(),
        x=250.0,
        y=60.0,
        width=120.0,
        height=80.0,
        fill="#00CC00",
        stroke="#222222",
        stroke_width=3.0,
    )
    line = LineObject(
        id=doc.new_id(),
        type="line",
        p1=[60.0, 180.0],
        p2=[200.0, 180.0],
        stroke="#333333",
        stroke_width=3.0,
    )
    arrow = LineObject(
        id=doc.new_id(),
        type="arrow",
        p1=[60.0, 220.0],
        p2=[200.0, 220.0],
        stroke="#444444",
        stroke_width=3.0,
        arrow_end="triangle",
        arrow_size=14.0,
    )
    freehand = FreehandObject(
        id=doc.new_id(),
        x=60.0,
        y=260.0,
        width=140.0,
        height=60.0,
        points=[[0.0, 0.0], [0.3, 1.0], [0.6, 0.2], [1.0, 0.8]],
        stroke="#555555",
        stroke_width=3.0,
    )
    text = TextObject(
        id=doc.new_id(),
        x=60.0,
        y=340.0,
        width=200.0,
        height=50.0,
        text="Hello 図",
        color="#666666",
        font_size=20.0,
    )
    image = ImageObject(id=doc.new_id(), x=60.0, y=410.0, width=100.0, height=80.0, src=rel)

    for obj in (rect, ellipse, line, arrow, freehand, text, image):
        doc.add_object(obj)

    return doc


# --------------------------------------------------------------------------
# PNG
# --------------------------------------------------------------------------


def test_png_size_matches_artboard_pixel_size(qapp: Any, project_dir: Path, tmp_path: Path) -> None:
    doc = _build_document(project_dir, tmp_path)
    out_path = project_dir / "exports" / "out.png"
    export_png(doc, str(out_path), transparent=False)

    expected_w, expected_h = artboard_pixel_size(doc)
    with Image.open(out_path) as img:
        assert img.size == (expected_w, expected_h)


def test_png_opaque_has_content(qapp: Any, project_dir: Path, tmp_path: Path) -> None:
    doc = _build_document(project_dir, tmp_path)
    out_path = project_dir / "exports" / "opaque.png"
    export_png(doc, str(out_path), transparent=False)

    with Image.open(out_path) as img:
        arr = np.asarray(img.convert("RGBA"))
    # 白背景一色ではなく、図形の色が写り込んでいること。
    assert arr[..., :3].std() > 5.0


def test_png_transparent_has_zero_alpha_corners(
    qapp: Any, project_dir: Path, tmp_path: Path
) -> None:
    doc = _build_document(project_dir, tmp_path)
    out_path = project_dir / "exports" / "transparent.png"
    export_png(doc, str(out_path), transparent=True)

    with Image.open(out_path) as img:
        arr = np.asarray(img.convert("RGBA"))
    h, w = arr.shape[0], arr.shape[1]
    corners = [(0, 0), (0, w - 1), (h - 1, 0), (h - 1, w - 1)]
    for cy, cx in corners:
        assert arr[cy, cx, 3] == 0, f"corner ({cy},{cx}) should be fully transparent"


def test_render_artboard_image_matches_export_png_size(
    qapp: Any, project_dir: Path, tmp_path: Path
) -> None:
    """クリップボードコピーが使う `render_artboard_image` も PNG と同じ寸法・内容を持つ。"""
    from app.export.png_exporter import render_artboard_image

    doc = _build_document(project_dir, tmp_path)
    image = render_artboard_image(doc)
    assert (image.width(), image.height()) == artboard_pixel_size(doc)


def test_copy_canvas_to_clipboard_puts_artboard_image(
    qapp: Any, project_dir: Path, tmp_path: Path
) -> None:
    """`copy_canvas_to_clipboard` でクリップボードに高DPI画像が載る。"""
    from PySide6.QtGui import QGuiApplication

    from app.scene.canvas_scene import CanvasScene
    from app.ui.controllers.export_controller import ExportController

    doc = _build_document(project_dir, tmp_path)
    scene = CanvasScene(doc)
    controller = ExportController(None, scene, lambda: None)

    QGuiApplication.clipboard().clear()
    controller.copy_canvas_to_clipboard()

    image = QGuiApplication.clipboard().image()
    assert not image.isNull()
    assert (image.width(), image.height()) == artboard_pixel_size(doc)


# --------------------------------------------------------------------------
# PDF
# --------------------------------------------------------------------------


def test_pdf_header_and_reasonable_size(qapp: Any, project_dir: Path, tmp_path: Path) -> None:
    doc = _build_document(project_dir, tmp_path)
    out_path = project_dir / "exports" / "out.pdf"
    export_pdf(doc, str(out_path), outline_text=True)

    data = out_path.read_bytes()
    assert data[:4] == b"%PDF"
    assert len(data) > 1024, "PDF が小さすぎる（生成失敗の疑い）"
    assert len(data) < 5_000_000, "PDF が異常に大きい"


def test_pdf_is_not_a_full_page_raster(qapp: Any, project_dir: Path, tmp_path: Path) -> None:
    """フルページの巨大ラスター埋め込みでないことのヒューリスティック検証。"""
    doc = _build_document(project_dir, tmp_path)
    out_path = project_dir / "exports" / "vector_check.pdf"
    export_pdf(doc, str(out_path), outline_text=True)

    data = out_path.read_bytes()
    width_px, height_px = artboard_pixel_size(doc)
    raw_bitmap_bytes = width_px * height_px * 4
    assert len(data) < raw_bitmap_bytes, (
        "PDF がアートボード px を ARGB 生ラスターで持つのと同程度以上のサイズ:"
        " フルページラスター埋め込みの疑い"
    )

    # 画像 XObject の個数は Document 中の image オブジェクト数(1個)に対して
    # 過剰でないこと(数個程度の余裕を見る)。
    image_xobject_count = len(re.findall(rb"/Subtype\s*/Image", data))
    assert image_xobject_count <= 4, f"/Image XObject が多すぎる: {image_xobject_count}"


def test_pdf_outline_text_true_generates_ok(qapp: Any, project_dir: Path, tmp_path: Path) -> None:
    doc = _build_document(project_dir, tmp_path)
    out_path = project_dir / "exports" / "outline.pdf"
    export_pdf(doc, str(out_path), outline_text=True)
    assert out_path.exists() and out_path.stat().st_size > 0


def test_pdf_outline_text_false_generates_ok(qapp: Any, project_dir: Path, tmp_path: Path) -> None:
    doc = _build_document(project_dir, tmp_path)
    out_path = project_dir / "exports" / "no_outline.pdf"
    export_pdf(doc, str(out_path), outline_text=False)
    assert out_path.exists() and out_path.stat().st_size > 0


# --------------------------------------------------------------------------
# SVG
# --------------------------------------------------------------------------


def test_svg_is_well_formed_xml(qapp: Any, project_dir: Path, tmp_path: Path) -> None:
    doc = _build_document(project_dir, tmp_path)
    svg = document_to_svg(doc, outline_text=True)
    root = ET.fromstring(svg)  # 整形式でなければ ParseError
    assert root.tag == f"{{{_SVG_NS}}}svg"


def test_svg_viewbox_matches_pixel_size(qapp: Any, project_dir: Path, tmp_path: Path) -> None:
    doc = _build_document(project_dir, tmp_path)
    svg = document_to_svg(doc, outline_text=True)
    root = ET.fromstring(svg)
    assert root.get("viewBox") == f"0 0 {doc.artboard.width_px} {doc.artboard.height_px}"


def test_svg_contains_expected_elements_outline_true(
    qapp: Any, project_dir: Path, tmp_path: Path
) -> None:
    doc = _build_document(project_dir, tmp_path)
    svg = document_to_svg(doc, outline_text=True)
    root = ET.fromstring(svg)

    rects = root.findall(".//svg:rect", _NS)
    ellipses = root.findall(".//svg:ellipse", _NS)
    paths = root.findall(".//svg:path", _NS)
    images = root.findall(".//svg:image", _NS)
    texts = root.findall(".//svg:text", _NS)

    # background rect + rect オブジェクト = 少なくとも 2
    assert len(rects) >= 2
    assert len(ellipses) >= 1
    # line body(1) + arrow body+arrowhead(2) + freehand(1) + text outline(1) >= 5
    assert len(paths) >= 5
    assert len(images) == 1
    # outline_text=True では <text> は出ない(すべて path 化)
    assert len(texts) == 0

    href = images[0].get(f"{{{_XLINK_NS}}}href")
    assert href is not None and href.startswith("data:image/png;base64,")


def test_svg_outline_text_false_emits_text_element(
    qapp: Any, project_dir: Path, tmp_path: Path
) -> None:
    doc = _build_document(project_dir, tmp_path)
    svg = document_to_svg(doc, outline_text=False)
    root = ET.fromstring(svg)
    texts = root.findall(".//svg:text", _NS)
    assert len(texts) >= 1


def test_svg_z_order_matches_objects_order(qapp: Any, project_dir: Path, tmp_path: Path) -> None:
    """document.objects の配列順(z順)通りに SVG 要素が出力されること。"""
    doc = _build_document(project_dir, tmp_path)
    svg = document_to_svg(doc, outline_text=True)

    markers = [
        'stroke="#111111"',  # rect
        'stroke="#222222"',  # ellipse
        'stroke="#333333"',  # line
        'stroke="#444444"',  # arrow
        'stroke="#555555"',  # freehand
        'fill="#666666"',  # text outline path
        "<image",  # image
    ]
    positions = [svg.index(marker) for marker in markers]
    assert positions == sorted(positions), "z順(document.objects 順)で出力されていない"


def test_export_svg_writes_file(qapp: Any, project_dir: Path, tmp_path: Path) -> None:
    doc = _build_document(project_dir, tmp_path)
    out_path = project_dir / "exports" / "out.svg"
    export_svg(doc, str(out_path), outline_text=True)
    assert out_path.exists()
    root = ET.fromstring(out_path.read_text(encoding="utf-8"))
    assert root.tag == f"{{{_SVG_NS}}}svg"


def test_svg_non_outline_text_font_size_matches_metrics_px(
    qapp: Any, project_dir: Path, tmp_path: Path
) -> None:
    """非アウトライン<text>の font-size が **em サイズ**、行送りが lineSpacing であること。

    2 段階の修正を経ている:
    1. 旧々実装はモデルの point-size 値をそのまま SVG user-unit(px) にしていた（DPI 換算漏れ）。
    2. その修正で `ascent + descent` を使ったが、これは**行ボックスの高さであって em ではない**。
       Noto Sans CJK では約 1.45em あるため `<text>` だけ 1.45 倍で描かれ、
       outline 版・画面・PDF と食い違っていた。
    正解は解決後のピクセルサイズ（`QFontInfo(font).pixelSize()`）。
    """
    doc = _build_document(project_dir, tmp_path)
    text_obj = next(obj for obj in doc.objects if obj.type == "text")
    text_obj.text = "Hello 図\nWorld"  # 複数行にして行送り(lineSpacing)も検証する

    font = _build_text_font(text_obj)
    metrics = QFontMetricsF(font)
    expected_font_size_px = float(QFontInfo(font).pixelSize())
    expected_line_spacing = metrics.lineSpacing()
    expected_baseline = metrics.ascent()

    # モデルのpoint-size値をそのままpxとして使う旧実装では、この程度のフォント
    # サイズではmetrics由来のpx値と大きく異なるはず（回帰防止の非退化チェック）。
    assert abs(expected_font_size_px - text_obj.font_size) > 0.5
    # 行ボックス高（旧実装の値）とも明確に違うこと。
    assert abs(expected_font_size_px - (metrics.ascent() + metrics.descent())) > 0.5

    svg = document_to_svg(doc, outline_text=False)
    root = ET.fromstring(svg)
    text_el = root.find(".//svg:text", _NS)
    assert text_el is not None

    font_size_attr = float(text_el.get("font-size"))
    assert font_size_attr == pytest.approx(expected_font_size_px, abs=0.01)

    tspans = text_el.findall("svg:tspan", _NS)
    assert len(tspans) == 2
    y0 = float(tspans[0].get("y"))
    y1 = float(tspans[1].get("y"))
    assert y0 == pytest.approx(expected_baseline, abs=0.01)
    assert (y1 - y0) == pytest.approx(expected_line_spacing, abs=0.01)


def test_svg_color_with_embedded_quote_stays_well_formed_xml(
    qapp: Any, project_dir: Path, tmp_path: Path
) -> None:
    """手書き/読込project.jsonの色値に二重引用符が紛れても、生成SVGが整形式であり
    値が壊れず(エスケープされて)復元できること。"""
    artboard = Artboard(
        width_px=200, height_px=200, physical=Physical(width_mm=50.0, target_dpi=150)
    )
    doc = Document(artboard=artboard)
    malicious_stroke = 'red" onmouseover="alert(1)'
    rect = RectObject(
        id=doc.new_id(),
        x=10.0,
        y=10.0,
        width=50.0,
        height=30.0,
        fill=None,
        stroke=malicious_stroke,
        stroke_width=2.0,
    )
    doc.add_object(rect)

    svg = document_to_svg(doc, outline_text=True)
    root = ET.fromstring(svg)  # 不正なXMLなら ParseError で失敗する

    rects = root.findall(".//svg:rect", _NS)
    obj_rect = next(r for r in rects if r.get("stroke") is not None)
    # 属性値が injection されず、元の文字列どおりに往復すること。
    assert obj_rect.get("stroke") == malicious_stroke
    assert obj_rect.get("fill") == "none"


# --------------------------------------------------------------------------
# 使い捨て CanvasScene のリスナーリーク回帰（Phase 0-2）
# --------------------------------------------------------------------------


def test_render_and_export_do_not_leak_document_listeners(
    qapp: Any, project_dir: Path, tmp_path: Path
) -> None:
    """レンダリング/書き出しが document のリスナーを増やさない。

    `CanvasScene.__init__` は `document.add_listener(self)` する。解除を忘れると
    影シーンが永久に残り、以後のすべてのモデル変更がそこにもファンアウトする
    （エージェントの「操作→撮影」ループで線形に劣化する）。
    """
    from app.export.png_exporter import render_artboard_image

    doc = _build_document(project_dir, tmp_path)
    before = len(doc._listeners)

    for _ in range(3):
        render_artboard_image(doc)
    export_png(doc, str(tmp_path / "leak.png"))
    export_pdf(doc, str(tmp_path / "leak.pdf"))
    export_svg(doc, str(tmp_path / "leak.svg"))

    assert len(doc._listeners) == before


def test_canvas_scene_close_is_idempotent_and_context_manager_works(
    qapp: Any, project_dir: Path, tmp_path: Path
) -> None:
    from app.scene.canvas_scene import CanvasScene

    doc = _build_document(project_dir, tmp_path)
    before = len(doc._listeners)

    with CanvasScene(doc) as scene:
        assert len(doc._listeners) == before + 1
        assert scene.item_for(doc.objects[0]) is not None
    assert len(doc._listeners) == before

    scene2 = CanvasScene(doc)
    scene2.close()
    scene2.close()  # 冪等
    assert len(doc._listeners) == before


def test_unknown_object_type_does_not_desync_scene_from_document(qapp: Any, tmp_path: Path) -> None:
    """未知 type の追加でも例外を投げず、後続リスナーへの通知を止めない。"""
    from app.scene.canvas_scene import CanvasScene

    doc = Document()
    with CanvasScene(doc) as scene:
        seen: list[int] = []

        class _Tail:
            def on_object_added(self, obj: Any, index: int) -> None:
                seen.append(obj.id)

            def on_object_removed(self, obj: Any) -> None: ...
            def on_object_changed(self, obj: Any, keys: tuple[str, ...]) -> None: ...
            def on_order_changed(self) -> None: ...
            def on_artboard_changed(self) -> None: ...

        doc.add_listener(_Tail())
        alien = RectObject(id=doc.new_id())
        alien.type = "no_such_type"
        doc.add_object(alien)  # 例外を投げないこと

        assert seen == [alien.id]
        assert doc.objects[-1] is alien
        assert scene.item_for(alien) is None


# --------------------------------------------------------------------------
# フォントサイズがデバイス DPI に依存しないこと（PDF テキスト消失の回帰防止）
# --------------------------------------------------------------------------


def _text_only_doc(**kwargs: Any) -> Document:
    artboard = Artboard(
        width_px=600, height_px=200, physical=Physical(width_mm=100.0, target_dpi=300)
    )
    doc = Document(artboard=artboard)
    doc.add_object(TextObject(id=doc.new_id(), x=20.0, y=40.0, width=520.0, height=100.0, **kwargs))
    return doc


def _ink_bbox_normalized(image: Any) -> tuple[float, float, float, float] | None:
    """白背景に合成したうえでインクの外接矩形を [0,1] 正規化して返す。

    PDF のラスタライズ結果は背景が透明（RGB=0）なので、合成せずに暗さだけを見ると
    ページ全体をインクと誤認する。
    """
    w, h = image.width(), image.height()
    arr = np.frombuffer(image.constBits(), dtype=np.uint8).reshape(h, image.bytesPerLine() // 4, 4)[
        :, :w, :
    ]
    alpha = arr[:, :, 3:4].astype(float) / 255.0
    rgb = arr[:, :, 2::-1].astype(float) * alpha + 255.0 * (1.0 - alpha)
    ys, xs = np.where(rgb.sum(axis=2) < 3 * 200)
    if not len(xs):
        return None
    return (xs.min() / w, ys.min() / h, xs.max() / w, ys.max() / h)


def _pdf_ink_bbox(doc: Document, path: Path, outline_text: bool) -> Any:
    from PySide6.QtCore import QSize
    from PySide6.QtGui import QImage
    from PySide6.QtPdf import QPdfDocument

    export_pdf(doc, str(path), outline_text=outline_text)
    pdf = QPdfDocument()
    pdf.load(str(path))
    image = pdf.render(0, QSize(1200, 400)).convertToFormat(QImage.Format.Format_ARGB32)
    return _ink_bbox_normalized(image)


@pytest.mark.parametrize("outline_text", [True, False])
def test_pdf_text_position_matches_png(qapp: Any, tmp_path: Path, outline_text: bool) -> None:
    """PDF のテキストが PNG と同じ位置・大きさで出ること。

    `QFont.setPointSizeF` はポイントを**描画デバイスの DPI** で px 解決する。
    `QPrinter(HighResolution)` は 1200dpi なので、画面/PNG（96dpi）の 12.5 倍になり、
    `outline_text=False` ではテキストがページ外へ飛んで **PDF が真っ白**になっていた。
    `_font_for` / `_build_text_font` が `setPixelSize` で実寸を焼き込むことで防ぐ。
    """
    from app.export.png_exporter import render_artboard_image

    png = _ink_bbox_normalized(
        render_artboard_image(_text_only_doc(text="Hello charta", font_size=30.0))
    )
    pdf = _pdf_ink_bbox(
        _text_only_doc(text="Hello charta", font_size=30.0),
        tmp_path / f"t_{outline_text}.pdf",
        outline_text,
    )
    assert png is not None
    assert pdf is not None, "PDF にテキストが 1 ピクセルも描かれていない"
    # 600px 幅のアートボード換算で 3px 以内（残差は PDF ページサイズの整数 pt 丸め由来）。
    deviation = max(abs(a - b) for a, b in zip(png, pdf, strict=True)) * 600
    assert deviation < 3.0, f"PNG と PDF のずれが大きい: {deviation:.2f}px"


def test_font_pixel_size_is_device_independent(qapp: Any) -> None:
    """`_font_for` が返すフォントは、どの描画デバイスでも同じピクセルサイズになる。"""
    from PySide6.QtGui import QFont, QImage, QPainter
    from PySide6.QtPrintSupport import QPrinter

    from app.scene.items.text_item import _font_for

    font = _font_for(TextObject(id=0, text="x", font_size=30.0))

    image = QImage(60, 20, QImage.Format.Format_ARGB32)
    image_painter = QPainter(image)
    on_image = QFontInfo(QFont(font, image_painter.device())).pixelSize()
    image_painter.end()

    printer = QPrinter(QPrinter.PrinterMode.HighResolution)
    printer.setOutputFormat(QPrinter.OutputFormat.PdfFormat)
    printer.setOutputFileName("/dev/null")
    printer_painter = QPainter(printer)
    assert printer.logicalDpiY() != image.logicalDpiY(), "前提: 2 つのデバイスの DPI は違う"
    on_printer = QFontInfo(QFont(font, printer_painter.device())).pixelSize()
    printer_painter.end()

    assert on_image == on_printer == QFontInfo(font).pixelSize()


# --------------------------------------------------------------------------
# valign（縦位置）: 画面 / SVG <text> / SVG outline / PDF の 4 経路一致
# --------------------------------------------------------------------------


def _valign_svg_doc(
    text: str, valign: str, height: float = 250.0, font_size: float = 28.0
) -> Document:
    """valign 検証専用の、テキスト 1 個だけの Document（背の高い箱）。"""
    artboard = Artboard(
        width_px=400, height_px=int(height) + 40, physical=Physical(width_mm=80.0, target_dpi=200)
    )
    doc = Document(artboard=artboard)
    doc.add_object(
        TextObject(
            id=doc.new_id(),
            x=10.0,
            y=10.0,
            width=300.0,
            height=height,
            text=text,
            valign=valign,
            font_size=font_size,
        )
    )
    return doc


def test_text_block_height_matches_layout_line_count(qapp: Any) -> None:
    """折返し・改行混在で `text_block_height` == 実レイアウトの行数 × lineSpacing。"""
    from app.export.text_outline import _layout_lines, text_block_height
    from app.scene.items.text_item import _font_for

    obj = TextObject(id=0, font_size=24.0)
    font = _font_for(obj)
    metrics = QFontMetricsF(font)

    text = "line one that is somewhat long and will wrap around\nsecond paragraph\nthird"
    wrap_width = 150.0
    lines, _metrics, total = _layout_lines(text, font, wrap_width)
    assert len(lines) >= 3, "前提: 折返しで複数行になっていること"
    expected = len(lines) * metrics.lineSpacing()

    assert text_block_height(text, font, wrap_width) == pytest.approx(expected)
    assert total == pytest.approx(expected), "空行が無ければ総送り == 行数 × lineSpacing"


def test_text_block_height_counts_blank_lines(qapp: Any) -> None:
    """空行はグリフを持たないが縦位置は占める。行数で数えると valign がずれる。"""
    from app.export.text_outline import _layout_lines, text_block_height
    from app.scene.items.text_item import _font_for

    obj = TextObject(id=0, font_size=24.0)
    font = _font_for(obj)
    metrics = QFontMetricsF(font)

    text = "Hello\n\nWorld"
    wrap_width = 400.0
    lines, _metrics, _total = _layout_lines(text, font, wrap_width)
    assert len(lines) == 2, "前提: 空行は描画対象の行としては落ちる"

    # 実際には 3 行スロット分の高さを占める。
    assert text_block_height(text, font, wrap_width) == pytest.approx(3 * metrics.lineSpacing())


def test_valign_bottom_keeps_text_with_blank_lines_inside_the_box(qapp: Any) -> None:
    """空行入りテキストでも bottom で箱からはみ出さない（実地のはみ出しバグの回帰）。"""
    from PySide6.QtCore import QRectF

    from app.export.text_outline import text_block_height, valign_offset
    from app.scene.items.text_item import _font_for

    obj = TextObject(id=0, font_size=16.0)
    font = _font_for(obj)
    rect = QRectF(0.0, 0.0, 120.0, 200.0)
    text = "Hello\n\nWorld"

    offset = valign_offset(text, font, rect, "bottom")
    block = text_block_height(text, font, rect.width())

    assert offset + block == pytest.approx(rect.height()), "ブロック下端が箱の下端に一致する"
    assert offset >= 0.0


def test_svg_text_element_baseline_matches_valign_offset(qapp: Any) -> None:
    """非アウトライン `<text>` の最初の `<tspan>` の y が ascent + valign_offset と一致する。"""
    from PySide6.QtCore import QRectF

    from app.export.text_outline import valign_offset

    doc = _valign_svg_doc("Hello valign", "middle", height=250.0)
    text_obj = doc.objects[0]

    font = _build_text_font(text_obj)
    metrics = QFontMetricsF(font)
    rect = QRectF(0.0, 0.0, text_obj.width, text_obj.height)
    offset = valign_offset(text_obj.text, font, rect, text_obj.valign)
    assert offset > 0.0, "前提: middle でオフセットが生じていること"
    expected_y0 = metrics.ascent() + offset

    svg = document_to_svg(doc, outline_text=False)
    root = ET.fromstring(svg)
    text_el = root.find(".//svg:text", _NS)
    assert text_el is not None
    tspan0 = text_el.find("svg:tspan", _NS)
    assert tspan0 is not None
    y0 = float(tspan0.get("y"))
    assert y0 == pytest.approx(expected_y0, abs=0.01)


def test_svg_outline_and_text_element_agree_on_valign(qapp: Any) -> None:
    """同じ text を outline_text=True/False で出しても、valign による移動量が一致する。

    Qt の `QSvgRenderer` はこちらが生成した `<text>`/`<tspan>` を安定して再現しない
    （CLAUDE.md §4: Qt の SVG テキスト経路は不安定という前提そのもの）ため、
    ラスタライズして比較するのではなく、両分岐が生成する幾何値を直接比較する。
    絶対位置（ascent と実際のグリフ ink top の差、いわゆる cap-height の余白）は
    経路によらず一定なので、それを打ち消すため valign="top"（offset==0）を基準とした
    **差分**（bottom 移動量）で比較する。
    """
    from PySide6.QtCore import QRectF

    from app.export.text_outline import text_to_path

    def _outline_top(valign: str) -> float:
        doc = _valign_svg_doc("Hello valign", valign, height=250.0)
        obj = doc.objects[0]
        font = _build_text_font(obj)
        rect = QRectF(0.0, 0.0, obj.width, obj.height)
        path = text_to_path(obj.text, font, rect, obj.align, valign=obj.valign)
        return path.boundingRect().top()

    def _text_baseline(valign: str) -> float:
        doc = _valign_svg_doc("Hello valign", valign, height=250.0)
        svg = document_to_svg(doc, outline_text=False)
        root = ET.fromstring(svg)
        tspan0 = root.find(".//svg:text/svg:tspan", _NS)
        assert tspan0 is not None
        return float(tspan0.get("y"))

    outline_delta = _outline_top("bottom") - _outline_top("top")
    text_delta = _text_baseline("bottom") - _text_baseline("top")

    assert outline_delta == pytest.approx(text_delta, abs=0.5), (
        f"outline と <text> とで valign による移動量が食い違う: "
        f"outline={outline_delta:.2f} text={text_delta:.2f}"
    )


def test_png_export_matches_screen_for_middle_valign(qapp: Any) -> None:
    """valign="middle" でも、画面描画(render_document)と PNG 書き出しが一致する。"""
    from app.agent.render import render_document
    from app.export.png_exporter import render_artboard_image

    doc_screen = _valign_svg_doc("Ay charta", "middle", height=180.0)
    doc_png = _valign_svg_doc("Ay charta", "middle", height=180.0)

    w_px, h_px = artboard_pixel_size(doc_png)
    screen_img, _ = render_document(doc_screen, max_edge=max(w_px, h_px))
    png_img = render_artboard_image(doc_png)

    screen_bbox = _ink_bbox_normalized(screen_img)
    png_bbox = _ink_bbox_normalized(png_img)
    assert screen_bbox is not None
    assert png_bbox is not None
    deviation = max(abs(a - b) for a, b in zip(screen_bbox, png_bbox, strict=True)) * w_px
    assert deviation < 3.0, f"画面と PNG のずれが大きい(valign): {deviation:.2f}px"


@pytest.mark.parametrize("outline_text", [True, False])
def test_pdf_text_position_matches_png_for_valign(
    qapp: Any, tmp_path: Path, outline_text: bool
) -> None:
    """valign="middle" の背の高い箱でも、PDF と PNG のインク位置が一致する
    （4経路一致の最終ゲート）。
    """
    from app.export.png_exporter import render_artboard_image

    doc_png = _valign_svg_doc("Hello valign", "middle", height=180.0)
    doc_pdf = _valign_svg_doc("Hello valign", "middle", height=180.0)

    png = _ink_bbox_normalized(render_artboard_image(doc_png))
    pdf = _pdf_ink_bbox(doc_pdf, tmp_path / f"valign_{outline_text}.pdf", outline_text)
    assert png is not None
    assert pdf is not None, "PDF にテキストが 1 ピクセルも描かれていない"
    w_px, _h_px = artboard_pixel_size(doc_png)
    deviation = max(abs(a - b) for a, b in zip(png, pdf, strict=True)) * w_px
    assert deviation < 3.0, f"PNG と PDF のずれが大きい(valign): {deviation:.2f}px"
