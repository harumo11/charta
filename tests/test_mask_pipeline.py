"""app/graphics/image_pipeline・app/model の SAM3 マスク合成テスト（contract_sam3 E-1）。

Qt 非依存・純 Python。マスク合成の画素演算（`apply_mask_overlay`）、色/マスク読込の
補助関数、`ImageObject` の新規フィールド、`save_mask_png`、`combined_mask_u8`
（app.ai.sam3・torch 不要）、SVG 埋め込みへの反映を検証する。
"""

from __future__ import annotations

import base64
import io
import xml.etree.ElementTree as ET
from pathlib import Path
from typing import Any

import numpy as np
import pytest
from PIL import Image

from app.ai.sam3 import MaskCandidate, combined_mask_u8
from app.export.svg_exporter import document_to_svg
from app.graphics.image_pipeline import (
    apply_mask_overlay,
    build_processed_rgba,
    load_mask_gray,
    parse_hex_color,
)
from app.model.document import Artboard, Document, Physical
from app.model.objects import BaseObject, ImageObject
from app.model.serialize import import_image, save_document, save_mask_png

_SVG_NS = "http://www.w3.org/2000/svg"
_XLINK_NS = "http://www.w3.org/1999/xlink"


@pytest.fixture
def project_dir(tmp_path: Path) -> Path:
    return tmp_path / "proj"


def _rgba(w: int, h: int, rgb: tuple[int, int, int], alpha: int = 255) -> np.ndarray:
    """一様な色で塗った RGBA uint8 [h,w,4] を作る。"""
    arr = np.zeros((h, w, 4), dtype=np.uint8)
    arr[..., 0] = rgb[0]
    arr[..., 1] = rgb[1]
    arr[..., 2] = rgb[2]
    arr[..., 3] = alpha
    return arr


def _make_source_image(path: Path, w: int, h: int, rgb: tuple[int, int, int]) -> np.ndarray:
    """一様な色の RGB 画像を PNG 保存し、その RGB numpy 配列 [h,w,3] を返す。"""
    arr = np.zeros((h, w, 3), dtype=np.uint8)
    arr[..., 0] = rgb[0]
    arr[..., 1] = rgb[1]
    arr[..., 2] = rgb[2]
    Image.fromarray(arr, mode="RGB").save(path)
    return arr


# --------------------------------------------------------------------------
# apply_mask_overlay
# --------------------------------------------------------------------------


def test_apply_mask_overlay_color_full_opacity() -> None:
    arr = _rgba(4, 4, (100, 150, 200))
    mask = np.zeros((4, 4), dtype=np.uint8)
    mask[:, 2:] = 255  # 右半分 = 対象物（見せる）

    out = apply_mask_overlay(arr, mask, "#C86432", 1.0)

    # 左半分（対象外）は完全に覆い色になる。
    assert np.array_equal(out[:, :2, :3], np.full((4, 2, 3), (200, 100, 50), dtype=np.uint8))
    # 右半分（対象物）は不変。
    assert np.array_equal(out[:, 2:, :3], arr[:, 2:, :3])
    # alpha は全画素で不変。
    assert np.array_equal(out[..., 3], arr[..., 3])
    assert out.dtype == np.uint8
    assert out.flags["C_CONTIGUOUS"]


def test_apply_mask_overlay_color_half_opacity() -> None:
    arr = _rgba(4, 4, (100, 150, 200))
    mask = np.zeros((4, 4), dtype=np.uint8)
    mask[:, 2:] = 255

    out = apply_mask_overlay(arr, mask, "#C86432", 0.5)

    # coverage=0.5: rgb' = rgb*0.5 + color*0.5 -> (150, 125, 125)（ちょうど割り切れる値）。
    expected = np.array([150, 125, 125], dtype=np.uint8)
    assert np.array_equal(out[:, :2, :3], np.broadcast_to(expected, (4, 2, 3)))
    assert np.array_equal(out[:, 2:, :3], arr[:, 2:, :3])


def test_apply_mask_overlay_cutout_full_opacity() -> None:
    arr = _rgba(4, 4, (100, 150, 200), alpha=255)
    mask = np.zeros((4, 4), dtype=np.uint8)
    mask[:, 2:] = 255

    out = apply_mask_overlay(arr, mask, None, 1.0)

    # rgb は素通し、alpha だけ変化する。
    assert np.array_equal(out[..., :3], arr[..., :3])
    assert np.all(out[:, :2, 3] == 0)
    assert np.all(out[:, 2:, 3] == 255)


def test_apply_mask_overlay_cutout_partial_opacity() -> None:
    arr = _rgba(4, 4, (100, 150, 200), alpha=255)
    mask = np.zeros((4, 4), dtype=np.uint8)
    mask[:, 2:] = 255

    out = apply_mask_overlay(arr, mask, None, 0.6)

    # alpha' = round(255*(1-0.6)) = round(102.0) = 102
    assert np.all(out[:, :2, 3] == 102)
    assert np.all(out[:, 2:, 3] == 255)
    assert np.array_equal(out[..., :3], arr[..., :3])


def test_apply_mask_overlay_identity_zero_opacity() -> None:
    arr = _rgba(4, 4, (10, 20, 30))
    mask = np.zeros((4, 4), dtype=np.uint8)

    out = apply_mask_overlay(arr, mask, "#FF0000", 0.0)

    assert np.array_equal(out, arr)


def test_apply_mask_overlay_identity_all_255_mask() -> None:
    arr = _rgba(4, 4, (10, 20, 30))
    mask = np.full((4, 4), 255, dtype=np.uint8)

    out = apply_mask_overlay(arr, mask, "#FF0000", 1.0)

    assert np.array_equal(out, arr)


def test_apply_mask_overlay_soft_edge_mid_value() -> None:
    arr = _rgba(2, 2, (100, 150, 200))
    mask = np.full((2, 2), 128, dtype=np.uint8)
    color = (200, 100, 50)

    out = apply_mask_overlay(arr, mask, "#C86432", 1.0)

    coverage = (1.0 - 128.0 / 255.0) * 1.0
    expected = np.array([100.0, 150.0, 200.0]) * (1.0 - coverage) + np.array(color) * coverage
    np.testing.assert_allclose(out[0, 0, :3].astype(np.float64), expected, atol=1.5)


def test_apply_mask_overlay_shape_mismatch_raises() -> None:
    arr = _rgba(4, 4, (1, 2, 3))
    mask = np.zeros((3, 3), dtype=np.uint8)

    with pytest.raises(ValueError):
        apply_mask_overlay(arr, mask, "#FFFFFF", 1.0)


# --------------------------------------------------------------------------
# parse_hex_color
# --------------------------------------------------------------------------


def test_parse_hex_color_valid() -> None:
    assert parse_hex_color("#FF8000") == (255, 128, 0)


def test_parse_hex_color_lowercase() -> None:
    assert parse_hex_color("#ff8000") == (255, 128, 0)


def test_parse_hex_color_invalid_name_raises() -> None:
    with pytest.raises(ValueError):
        parse_hex_color("red")


def test_parse_hex_color_invalid_short_form_raises() -> None:
    with pytest.raises(ValueError):
        parse_hex_color("#FFF")


# --------------------------------------------------------------------------
# load_mask_gray
# --------------------------------------------------------------------------


def test_load_mask_gray_reads_existing_png(tmp_path: Path) -> None:
    mask_path = tmp_path / "mask.png"
    arr = np.zeros((7, 5), dtype=np.uint8)
    arr[:, 3:] = 255
    Image.fromarray(arr, mode="L").save(mask_path)

    obj = ImageObject(id=1, mask_src=str(mask_path))
    loaded = load_mask_gray(None, obj)

    assert loaded is not None
    assert loaded.shape == (7, 5)
    assert np.array_equal(loaded, arr)


def test_load_mask_gray_none_when_mask_src_none() -> None:
    obj = ImageObject(id=1, mask_src=None)
    assert load_mask_gray(None, obj) is None


def test_load_mask_gray_missing_path_warns_and_returns_none() -> None:
    obj = ImageObject(id=1, mask_src="/no/such/mask_missing.png")
    with pytest.warns(UserWarning):
        result = load_mask_gray(None, obj)
    assert result is None


def test_load_mask_gray_resizes_on_size_mismatch(tmp_path: Path) -> None:
    mask_path = tmp_path / "mask_mismatch.png"
    arr = np.zeros((10, 10), dtype=np.uint8)
    arr[:, 5:] = 255
    Image.fromarray(arr, mode="L").save(mask_path)

    obj = ImageObject(id=1, mask_src=str(mask_path))
    with pytest.warns(UserWarning):
        loaded = load_mask_gray(None, obj, expected_size=(5, 7))  # (width, height)

    assert loaded is not None
    assert loaded.shape == (7, 5)  # (height, width)


# --------------------------------------------------------------------------
# build_processed_rgba end-to-end（mask 反映）
# --------------------------------------------------------------------------


def test_build_processed_rgba_applies_mask(project_dir: Path, tmp_path: Path) -> None:
    doc = Document()
    save_document(doc, project_dir)

    w, h = 6, 4
    src = tmp_path / "base.png"
    base_arr = _make_source_image(src, w, h, (100, 150, 200))
    rel = import_image(doc, str(src))

    mask_arr = np.zeros((h, w), dtype=np.uint8)
    mask_arr[:, w // 2 :] = 255  # 右半分 = 対象物
    mask_rel = save_mask_png(doc, mask_arr)

    obj = ImageObject(
        id=doc.new_id(),
        src=rel,
        mask_src=mask_rel,
        mask_color="#C86432",
        mask_opacity=1.0,
        mask_enabled=True,
    )
    doc.add_object(obj)

    result = build_processed_rgba(doc, obj)
    assert result is not None
    assert np.array_equal(
        result[:, : w // 2, :3], np.full((h, w // 2, 3), (200, 100, 50), dtype=np.uint8)
    )
    assert np.array_equal(result[:, w // 2 :, :3], base_arr[:, w // 2 :, :])


def test_build_processed_rgba_mask_disabled_passthrough(project_dir: Path, tmp_path: Path) -> None:
    doc = Document()
    save_document(doc, project_dir)

    w, h = 6, 4
    src = tmp_path / "base_disabled.png"
    base_arr = _make_source_image(src, w, h, (10, 20, 30))
    rel = import_image(doc, str(src))

    mask_arr = np.zeros((h, w), dtype=np.uint8)
    mask_rel = save_mask_png(doc, mask_arr)

    obj = ImageObject(
        id=doc.new_id(),
        src=rel,
        mask_src=mask_rel,
        mask_color="#FF0000",
        mask_opacity=1.0,
        mask_enabled=False,
    )
    doc.add_object(obj)

    result = build_processed_rgba(doc, obj)
    assert result is not None
    assert np.array_equal(result[..., :3], base_arr)
    assert np.all(result[..., 3] == 255)


def test_build_processed_rgba_mask_with_crop(project_dir: Path, tmp_path: Path) -> None:
    """crop=[1,1,2,2] が画像・マスク双方に同一適用され、対象外領域が覆われること。"""
    doc = Document()
    save_document(doc, project_dir)

    w = h = 10
    src = tmp_path / "base_crop.png"
    _make_source_image(src, w, h, (30, 60, 90))
    rel = import_image(doc, str(src))

    mask_arr = np.full((h, w), 255, dtype=np.uint8)
    mask_arr[1:3, 1:3] = 0  # x=1..3, y=1..3 の 2x2 が対象外
    mask_rel = save_mask_png(doc, mask_arr)

    obj = ImageObject(
        id=doc.new_id(),
        src=rel,
        mask_src=mask_rel,
        mask_color="#000000",
        mask_opacity=1.0,
        mask_enabled=True,
        crop=[1.0, 1.0, 2.0, 2.0],
    )
    doc.add_object(obj)

    result = build_processed_rgba(doc, obj)
    assert result is not None
    assert result.shape == (2, 2, 4)
    assert np.array_equal(result[..., :3], np.zeros((2, 2, 3), dtype=np.uint8))
    assert np.all(result[..., 3] == 255)


# --------------------------------------------------------------------------
# ImageObject の新規フィールド roundtrip
# --------------------------------------------------------------------------


def test_image_object_mask_fields_roundtrip() -> None:
    obj = ImageObject(
        id=1,
        src="assets/img_001.png",
        mask_src="assets/mask_001.png",
        mask_color="#112233",
        mask_opacity=0.42,
        mask_enabled=False,
    )
    d = obj.to_dict()
    restored = BaseObject.from_dict(d)

    assert isinstance(restored, ImageObject)
    assert restored.mask_src == "assets/mask_001.png"
    assert restored.mask_color == "#112233"
    assert restored.mask_opacity == 0.42
    assert restored.mask_enabled is False


def test_image_object_mask_fields_default_when_missing_from_legacy_dict() -> None:
    """マスクフィールドの無い旧 project.json（4フィールド追加前）でも既定値で復元される。"""
    legacy = {"type": "image", "id": 2, "src": "assets/img_002.png"}
    restored = BaseObject.from_dict(legacy)

    assert isinstance(restored, ImageObject)
    assert restored.mask_src is None
    assert restored.mask_color == "#FFFFFF"
    assert restored.mask_opacity == 0.8
    assert restored.mask_enabled is True


# --------------------------------------------------------------------------
# save_mask_png
# --------------------------------------------------------------------------


def test_save_mask_png_sequential_naming(project_dir: Path) -> None:
    doc = Document()
    save_document(doc, project_dir)

    mask1 = np.zeros((4, 4), dtype=np.uint8)
    rel1 = save_mask_png(doc, mask1)
    assert rel1 == "assets/mask_001.png"
    assert (project_dir / "assets" / "mask_001.png").exists()

    mask2 = np.full((4, 4), 255, dtype=np.uint8)
    rel2 = save_mask_png(doc, mask2)
    assert rel2 == "assets/mask_002.png"
    assert (project_dir / "assets" / "mask_002.png").exists()


def test_save_mask_png_l_mode_matches_dims(project_dir: Path) -> None:
    doc = Document()
    save_document(doc, project_dir)

    mask = np.zeros((5, 9), dtype=np.uint8)  # h=5, w=9
    rel = save_mask_png(doc, mask)

    with Image.open(project_dir / rel) as img:
        assert img.mode == "L"
        assert img.size == (9, 5)  # PIL size = (width, height)


def test_save_mask_png_requires_base_dir() -> None:
    doc = Document()
    mask = np.zeros((4, 4), dtype=np.uint8)

    with pytest.raises(RuntimeError):
        save_mask_png(doc, mask)


# --------------------------------------------------------------------------
# combined_mask_u8（app.ai.sam3, torch 不要な純 numpy 関数）
# --------------------------------------------------------------------------


def test_combined_mask_u8_union_of_accepted() -> None:
    m1 = np.zeros((4, 4), dtype=bool)
    m1[0, 0] = True
    m2 = np.zeros((4, 4), dtype=bool)
    m2[3, 3] = True
    candidates = [
        MaskCandidate(mask=m1, box=(0.0, 0.0, 1.0, 1.0), score=0.9, accepted=True),
        MaskCandidate(mask=m2, box=(3.0, 3.0, 4.0, 4.0), score=0.8, accepted=True),
    ]

    out = combined_mask_u8(candidates, (4, 4))

    assert out is not None
    assert out.dtype == np.uint8
    assert out[0, 0] == 255
    assert out[3, 3] == 255
    assert out[1, 1] == 0


def test_combined_mask_u8_excludes_non_accepted() -> None:
    m1 = np.ones((4, 4), dtype=bool)
    m2 = np.zeros((4, 4), dtype=bool)
    m2[0, 0] = True
    candidates = [
        MaskCandidate(mask=m1, box=(0.0, 0.0, 4.0, 4.0), score=0.9, accepted=False),
        MaskCandidate(mask=m2, box=(0.0, 0.0, 1.0, 1.0), score=0.5, accepted=True),
    ]

    out = combined_mask_u8(candidates, (4, 4))

    assert out is not None
    assert out[0, 0] == 255
    assert int(out.sum()) == 255  # 他画素はすべて 0


def test_combined_mask_u8_none_when_all_excluded() -> None:
    m1 = np.ones((4, 4), dtype=bool)
    candidates = [
        MaskCandidate(mask=m1, box=(0.0, 0.0, 4.0, 4.0), score=0.9, accepted=False),
    ]
    assert combined_mask_u8(candidates, (4, 4)) is None


def test_combined_mask_u8_none_when_empty_list() -> None:
    assert combined_mask_u8([], (4, 4)) is None


# --------------------------------------------------------------------------
# SVG エクスポートへの反映
# --------------------------------------------------------------------------


def test_svg_export_embeds_mask_applied_pixels(
    qapp: Any, project_dir: Path, tmp_path: Path
) -> None:
    artboard = Artboard(
        width_px=100, height_px=100, physical=Physical(width_mm=50.0, target_dpi=150)
    )
    doc = Document(artboard=artboard)
    save_document(doc, project_dir)

    w = h = 8
    src = tmp_path / "svg_base.png"
    base_arr = _make_source_image(src, w, h, (100, 150, 200))
    rel = import_image(doc, str(src))

    mask_arr = np.zeros((h, w), dtype=np.uint8)
    mask_arr[:, w // 2 :] = 255
    mask_rel = save_mask_png(doc, mask_arr)

    obj = ImageObject(
        id=doc.new_id(),
        x=0.0,
        y=0.0,
        width=float(w),
        height=float(h),
        src=rel,
        mask_src=mask_rel,
        mask_color="#C86432",
        mask_opacity=1.0,
        mask_enabled=True,
    )
    doc.add_object(obj)

    svg = document_to_svg(doc, outline_text=True)
    root = ET.fromstring(svg)
    image_el = root.find(f".//{{{_SVG_NS}}}image")
    assert image_el is not None

    href = image_el.get(f"{{{_XLINK_NS}}}href")
    assert href is not None and href.startswith("data:image/png;base64,")
    png_bytes = base64.b64decode(href.split(",", 1)[1])
    embedded = np.asarray(Image.open(io.BytesIO(png_bytes)).convert("RGBA"))

    assert np.array_equal(
        embedded[:, : w // 2, :3], np.full((h, w // 2, 3), (200, 100, 50), dtype=np.uint8)
    )
    assert np.array_equal(embedded[:, w // 2 :, :3], base_arr[:, w // 2 :, :])
