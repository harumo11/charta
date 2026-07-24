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
from PySide6.QtGui import QFontMetricsF

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
    """非アウトライン<text>のfont-size/行送りが、outline側と同じQFontから導出した
    QFontMetricsF由来のpx値であること（旧実装: モデルのpoint-size値をそのまま
    SVG user-unit(px)として使っていたためoutline/画面表示とずれていた）。"""
    doc = _build_document(project_dir, tmp_path)
    text_obj = next(obj for obj in doc.objects if obj.type == "text")
    text_obj.text = "Hello 図\nWorld"  # 複数行にして行送り(lineSpacing)も検証する

    font = _build_text_font(text_obj)
    metrics = QFontMetricsF(font)
    expected_font_size_px = metrics.ascent() + metrics.descent()
    expected_line_spacing = metrics.lineSpacing()
    expected_baseline = metrics.ascent()

    # モデルのpoint-size値をそのままpxとして使う旧実装では、この程度のフォント
    # サイズではmetrics由来のpx値と大きく異なるはず（回帰防止の非退化チェック）。
    assert abs(expected_font_size_px - text_obj.font_size) > 0.5

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
