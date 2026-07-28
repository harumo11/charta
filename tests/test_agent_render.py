"""app/agent/render.py のテスト（offscreen）。

重点は「エージェントが画像を見て正しい座標を言えるか」——座標契約が守られること、
そして注釈オーバーレイが書き出しに一切混入しないこと。
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from app.agent import render
from app.export.png_exporter import render_artboard_image
from app.export.svg_exporter import document_to_svg
from app.model.document import Artboard, Document, Physical
from app.model.objects import ConnectorObject, LineObject, RectObject


@pytest.fixture
def doc() -> Document:
    document = Document(
        artboard=Artboard(
            width_px=1000, height_px=500, physical=Physical(width_mm=100.0, target_dpi=300)
        )
    )
    document.add_object(RectObject(id=document.new_id(), x=100, y=50, width=200, height=100))
    document.add_object(
        LineObject(id=document.new_id(), type="arrow", p1=[400.0, 100.0], p2=[600.0, 300.0])
    )
    return document


# --------------------------------------------------------------------------
# サイズと座標契約
# --------------------------------------------------------------------------


def test_render_respects_max_edge_and_aspect(qapp: Any, doc: Document) -> None:
    image, view = render.render_document(doc, max_edge=500)
    assert (image.width(), image.height()) == (500, 250)
    assert view.image_width == 500 and view.image_height == 250
    assert view.scale_x == pytest.approx(0.5)
    assert view.scale_y == pytest.approx(0.5)


def test_max_edge_is_clamped_to_the_documented_range(qapp: Any, doc: Document) -> None:
    assert render.clamp_max_edge(10) == render.MIN_MAX_EDGE
    assert render.clamp_max_edge(99999) == render.MAX_MAX_EDGE
    assert render.clamp_max_edge(None) == render.DEFAULT_MAX_EDGE
    image, _ = render.render_document(doc, max_edge=99999)
    assert max(image.width(), image.height()) == render.MAX_MAX_EDGE


def test_image_to_artboard_roundtrip_matches_the_documented_formula(
    qapp: Any, doc: Document
) -> None:
    _, view = render.render_document(doc, region=(100.0, 50.0, 400.0, 200.0), max_edge=800)
    for ax, ay in ((100.0, 50.0), (300.0, 150.0), (500.0, 250.0)):
        ix, iy = view.to_image(ax, ay)
        back_x = view.region[0] + ix / view.scale_x
        back_y = view.region[1] + iy / view.scale_y
        assert back_x == pytest.approx(ax)
        assert back_y == pytest.approx(ay)
    assert "artboard_x = region.x + image_x / scale_x" in view.to_dict()["mapping"]


def test_region_render_has_no_letterboxing(qapp: Any, doc: Document) -> None:
    """丸めでレターボックスが入ると座標対応がずれる。scale は丸め後の真値を返す。"""
    region = (0.0, 0.0, 333.0, 111.0)
    image, view = render.render_document(doc, region=region, max_edge=1000)
    assert view.scale_x == pytest.approx(image.width() / region[2])
    assert view.scale_y == pytest.approx(image.height() / region[3])


def test_union_region_covers_requested_objects_with_padding(qapp: Any, doc: Document) -> None:
    rect_id = doc.objects[0].id
    region = render.union_region(doc, [rect_id], padding=10.0)
    assert region == (90.0, 40.0, 220.0, 120.0)
    assert render.union_region(doc, [9999]) is None


def test_union_region_of_a_horizontal_line_is_not_degenerate(qapp: Any) -> None:
    document = Document()
    line = LineObject(id=document.new_id(), p1=[10.0, 50.0], p2=[200.0, 50.0])
    document.add_object(line)
    region = render.union_region(document, [line.id], padding=0.0)
    assert region is not None and region[3] >= 1.0


def test_transparent_render_has_transparent_corner(qapp: Any, doc: Document) -> None:
    image, _ = render.render_document(doc, transparent=True, max_edge=256)
    assert image.pixelColor(0, image.height() - 1).alpha() == 0
    opaque, _ = render.render_document(doc, transparent=False, max_edge=256)
    assert opaque.pixelColor(0, opaque.height() - 1).alpha() == 255


def test_render_does_not_leak_document_listeners(qapp: Any, doc: Document) -> None:
    before = len(doc._listeners)
    for _ in range(5):
        render.render_document(doc, max_edge=256)
    assert len(doc._listeners) == before


# --------------------------------------------------------------------------
# オブジェクトのメタデータ
# --------------------------------------------------------------------------


def test_object_boxes_report_both_coordinate_spaces(qapp: Any, doc: Document) -> None:
    _, view = render.render_document(doc, max_edge=500)
    boxes = {entry["id"]: entry for entry in render.object_boxes(doc, view)}
    rect = boxes[doc.objects[0].id]
    assert rect["artboard_bbox"] == [100.0, 50.0, 200.0, 100.0]
    assert rect["image_bbox"] == [50.0, 25.0, 100.0, 50.0]


def test_object_boxes_include_endpoints_for_arrows(qapp: Any, doc: Document) -> None:
    _, view = render.render_document(doc, max_edge=500)
    arrow = next(e for e in render.object_boxes(doc, view) if e["type"] == "arrow")
    assert arrow["endpoints"]["p1"] == [400.0, 100.0]
    assert arrow["endpoints"]["p1_image"] == [200.0, 50.0]


def test_object_boxes_resolve_connector_endpoints_from_anchors(qapp: Any) -> None:
    """コネクタの端点は接続先のアンカーから解き直す（source_point は遅れうる）。"""
    document = Document()
    a = RectObject(id=document.new_id(), x=0, y=0, width=100, height=100)
    b = RectObject(id=document.new_id(), x=400, y=0, width=100, height=100)
    document.add_object(a)
    document.add_object(b)
    conn = ConnectorObject(
        id=document.new_id(),
        source_id=a.id,
        target_id=b.id,
        source_anchor="right",
        target_anchor="left",
        source_point=[0.0, 0.0],  # 意図的に古い値
        target_point=[0.0, 0.0],
    )
    document.add_object(conn)

    _, view = render.render_document(document, max_edge=500)
    entry = next(e for e in render.object_boxes(document, view) if e["type"] == "connector")
    assert entry["endpoints"]["p1"] == [100.0, 50.0]
    assert entry["endpoints"]["p2"] == [400.0, 50.0]


def test_invisible_objects_are_excluded_by_default(qapp: Any, doc: Document) -> None:
    doc.objects[0].visible = False
    _, view = render.render_document(doc, max_edge=256)
    ids = {e["id"] for e in render.object_boxes(doc, view)}
    assert doc.objects[0].id not in ids
    ids_all = {e["id"] for e in render.object_boxes(doc, view, visible_only=False)}
    assert doc.objects[0].id in ids_all


def test_offscreen_and_degenerate_objects_are_reported(qapp: Any, doc: Document) -> None:
    far = RectObject(id=doc.new_id(), x=5000, y=5000, width=10, height=10)
    flat = RectObject(id=doc.new_id(), x=10, y=10, width=0, height=0)
    doc.add_object(far)
    doc.add_object(flat)
    warnings = {w["id"]: w["code"] for w in render.offscreen_warnings(doc)}
    assert warnings[far.id] == "offscreen"
    assert warnings[flat.id] == "degenerate"


# --------------------------------------------------------------------------
# オーバーレイ
# --------------------------------------------------------------------------


def test_overlay_changes_pixels_but_never_the_document_or_exports(
    qapp: Any, doc: Document, tmp_path: Path
) -> None:
    baseline = document_to_svg(doc, outline_text=False)
    before_export = render_artboard_image(doc)

    image, view = render.render_document(doc, max_edge=400)
    plain = image.copy()
    render.draw_overlay(image, doc, view, mode="full")

    assert image != plain, "オーバーレイが描かれていない"
    # モデルにも書き出しにも一切影響しないこと。
    assert document_to_svg(doc, outline_text=False) == baseline
    after_export = render_artboard_image(doc)
    assert after_export == before_export
    assert len(doc.objects) == 2


def test_overlay_none_is_a_noop(qapp: Any, doc: Document) -> None:
    image, view = render.render_document(doc, max_edge=300)
    plain = image.copy()
    render.draw_overlay(image, doc, view, mode="none")
    assert image == plain


def test_unknown_overlay_mode_raises(qapp: Any, doc: Document) -> None:
    image, view = render.render_document(doc, max_edge=200)
    with pytest.raises(ValueError, match="overlay"):
        render.draw_overlay(image, doc, view, mode="sparkles")


# --------------------------------------------------------------------------
# ファイル出力
# --------------------------------------------------------------------------


def test_save_render_writes_a_png_and_prunes_old_ones(qapp: Any, doc: Document) -> None:
    from app.agent import paths

    image, _ = render.render_document(doc, max_edge=256)
    path = Path(render.save_render(image, doc))
    assert path.exists() and path.suffix == ".png"
    assert path.read_bytes()[:8] == b"\x89PNG\r\n\x1a\n"
    assert doc.uid[:8] in path.name and f"r{doc.revision}" in path.name

    for i in range(paths.RENDER_KEEP + 5):
        img, _ = render.render_document(doc, max_edge=256 + i)
        render.save_render(img, doc, tag=str(i))
    assert len(list(paths.render_dir().glob("*.png"))) <= paths.RENDER_KEEP
