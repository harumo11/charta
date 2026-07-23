"""app/model・app/scene の画像パイプライン統合テスト（M2契約 §5、offscreen）。

PIL で一時プロジェクトディレクトリに画像を生成し、契約 §5 の 1〜6 を検証する:
1. import_image（base_dir 未設定 RuntimeError / assets/img_001, img_002 の連番複製）
2. create_item(obj, document) が ImageItem を返し、_display_pixmap が非 null
3. brightness/contrast で表示画素平均・分散が原画と有意に異なる
4. crop begin/set/commit と undo
5. save/load roundtrip（src/brightness/contrast/crop 一致、resolve_asset_path 実在）
6. 存在しない src でプレースホルダかつ paint 非クラッシュ
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import numpy as np
import pytest
from PIL import Image
from PySide6.QtGui import QImage, QPainter

from app.commands.commands import AddObjectCommand
from app.model.document import Document
from app.model.objects import ImageObject
from app.model.serialize import (
    import_image,
    load_document,
    resolve_asset_path,
    save_document,
)
from app.scene.canvas_scene import CanvasScene
from app.scene.items import create_item
from app.scene.items.image_item import ImageItem


def _make_source_image(path: Path, w: int = 40, h: int = 30) -> None:
    """グラデーション画像（平坦でない = brightness/contrast の効果を検出しやすい）を作る。"""
    x = np.linspace(0, 255, w, dtype=np.uint8)
    row = np.tile(x, (h, 1))
    rgb = np.stack([row, row, row], axis=-1).astype(np.uint8)
    img = Image.fromarray(rgb, mode="RGB")
    img.save(path)


def _pixmap_to_array(pixmap: Any) -> np.ndarray:
    qimage = pixmap.toImage().convertToFormat(QImage.Format.Format_RGBA8888)
    w, h = qimage.width(), qimage.height()
    ptr = qimage.constBits()
    buf = bytes(ptr)[: w * h * 4]
    return np.frombuffer(buf, dtype=np.uint8).reshape(h, w, 4)


@pytest.fixture
def project_dir(tmp_path: Path) -> Path:
    return tmp_path / "proj"


# --------------------------------------------------------------------------
# 1. import_image
# --------------------------------------------------------------------------


def test_import_image_requires_base_dir(project_dir: Path, tmp_path: Path) -> None:
    doc = Document()
    src = tmp_path / "src1.png"
    _make_source_image(src)
    with pytest.raises(RuntimeError):
        import_image(doc, str(src))


def test_import_image_copies_with_sequential_names(project_dir: Path, tmp_path: Path) -> None:
    doc = Document()
    save_document(doc, project_dir)  # base_dir を設定する

    src1 = tmp_path / "a.png"
    src2 = tmp_path / "b.PNG"
    _make_source_image(src1)
    _make_source_image(src2)

    rel1 = import_image(doc, str(src1))
    assert rel1 == "assets/img_001.png"
    assert (project_dir / "assets" / "img_001.png").exists()

    rel2 = import_image(doc, str(src2))
    assert rel2 == "assets/img_002.png"
    assert (project_dir / "assets" / "img_002.png").exists()


# --------------------------------------------------------------------------
# 2. create_item(obj, document) -> ImageItem, _display_pixmap 非 null
# --------------------------------------------------------------------------


def test_create_item_returns_image_item_with_display_pixmap(
    qapp: Any, project_dir: Path, tmp_path: Path
) -> None:
    doc = Document()
    save_document(doc, project_dir)
    src = tmp_path / "src.png"
    _make_source_image(src, w=50, h=40)
    rel = import_image(doc, str(src))

    obj = ImageObject(id=doc.new_id(), src=rel, x=0, y=0, width=50, height=40)
    doc.add_object(obj)

    item = create_item(obj, doc)
    assert isinstance(item, ImageItem)
    assert not item._display_pixmap.isNull()


# --------------------------------------------------------------------------
# 3. brightness/contrast で表示画素が有意に変化する
# --------------------------------------------------------------------------


def test_brightness_changes_display_mean(qapp: Any, project_dir: Path, tmp_path: Path) -> None:
    doc = Document()
    save_document(doc, project_dir)
    src = tmp_path / "grad.png"
    _make_source_image(src, w=60, h=40)
    rel = import_image(doc, str(src))

    obj = ImageObject(id=doc.new_id(), src=rel, x=0, y=0, width=60, height=40)
    doc.add_object(obj)
    item = create_item(obj, doc)

    base_arr = _pixmap_to_array(item._display_pixmap)
    base_mean = base_arr[..., :3].astype(np.float64).mean()

    obj.brightness = 0.5
    item.sync_from_model()
    bright_arr = _pixmap_to_array(item._display_pixmap)
    bright_mean = bright_arr[..., :3].astype(np.float64).mean()

    assert bright_mean > base_mean + 10.0, "brightness=+0.5 で表示画素平均が有意に上がること"


def test_contrast_changes_display_variance(qapp: Any, project_dir: Path, tmp_path: Path) -> None:
    doc = Document()
    save_document(doc, project_dir)
    src = tmp_path / "grad2.png"
    _make_source_image(src, w=60, h=40)
    rel = import_image(doc, str(src))

    obj = ImageObject(id=doc.new_id(), src=rel, x=0, y=0, width=60, height=40)
    doc.add_object(obj)
    item = create_item(obj, doc)

    base_arr = _pixmap_to_array(item._display_pixmap)
    base_std = base_arr[..., :3].astype(np.float64).std()

    obj.contrast = 0.8
    item.sync_from_model()
    contrast_arr = _pixmap_to_array(item._display_pixmap)
    contrast_std = contrast_arr[..., :3].astype(np.float64).std()

    assert contrast_std > base_std + 1.0, "contrast 増加で分散が有意に増えること"
    # 原画と有意に異なること
    assert not np.array_equal(base_arr, contrast_arr)


# --------------------------------------------------------------------------
# 4. crop begin/set/commit + undo
# --------------------------------------------------------------------------


def test_crop_begin_set_commit_and_undo(qapp: Any, project_dir: Path, tmp_path: Path) -> None:
    doc = Document()
    save_document(doc, project_dir)
    src = tmp_path / "crop_src.png"
    _make_source_image(src, w=80, h=60)
    rel = import_image(doc, str(src))

    obj = ImageObject(id=doc.new_id(), src=rel, x=0, y=0, width=80, height=60)
    scene = CanvasScene(doc)
    from PySide6.QtGui import QUndoStack

    stack = QUndoStack()
    scene.set_undo_stack(stack)
    stack.push(AddObjectCommand(scene, obj))

    item = scene.item_for(obj)
    assert isinstance(item, ImageItem)

    item.begin_crop()
    assert item._crop_mode is True

    item.set_crop_rect(10.0, 5.0, 30.0, 20.0)
    assert item._crop_overlay_px == [10.0, 5.0, 30.0, 20.0]

    # crop 前の表示は原画全体（cache key が None で全体表示中）。
    full_display_w = item._display_pixmap.width()

    item.commit_crop()
    assert obj.crop == [10.0, 5.0, 30.0, 20.0]
    assert item._crop_mode is False
    # crop 前後で表示スケールを軸ごとに維持する: 元 80x60 (等倍表示) を
    # crop 30x20 したので width/height も同じ倍率で 30x20 に調整される
    # （引き伸ばして歪ませない、修正2）。
    assert obj.width == pytest.approx(30.0)
    assert obj.height == pytest.approx(20.0)

    # crop 反映後、表示バッファの幅が原画全体より縮小していること。
    cropped_display_w = item._display_pixmap.width()
    assert cropped_display_w < full_display_w
    assert cropped_display_w == 30

    stack.undo()
    assert obj.crop is None
    # マクロ1回の undo で crop と width/height の両方が元に戻ること。
    assert obj.width == pytest.approx(80.0)
    assert obj.height == pytest.approx(60.0)


def test_crop_preserves_per_axis_display_scale_when_prescaled(
    qapp: Any, project_dir: Path, tmp_path: Path
) -> None:
    """表示サイズが原画ピクセルサイズと異なる（既にスケール済みの）場合でも、

    crop 後の width/height が軸ごとのスケールを維持して計算されること
    （縦横で異なる倍率を掛けても歪まないことの確認、修正2）。
    """
    doc = Document()
    save_document(doc, project_dir)
    src = tmp_path / "crop_scaled_src.png"
    _make_source_image(src, w=80, h=60)
    rel = import_image(doc, str(src))

    # 表示サイズは原画 80x60 に対し軸ごとに異なる倍率(x2, x1.5)でスケール済み。
    obj = ImageObject(id=doc.new_id(), src=rel, x=0, y=0, width=160.0, height=90.0)
    scene = CanvasScene(doc)
    from PySide6.QtGui import QUndoStack

    stack = QUndoStack()
    scene.set_undo_stack(stack)
    stack.push(AddObjectCommand(scene, obj))

    item = scene.item_for(obj)
    assert isinstance(item, ImageItem)

    item.begin_crop()
    item.set_crop_rect(10.0, 10.0, 40.0, 20.0)
    item.commit_crop()

    assert obj.crop == [10.0, 10.0, 40.0, 20.0]
    # new_width = crop_w * (old_width / src_w) = 40 * (160/80) = 80
    # new_height = crop_h * (old_height / src_h) = 20 * (90/60) = 30
    assert obj.width == pytest.approx(80.0)
    assert obj.height == pytest.approx(30.0)

    stack.undo()
    assert obj.crop is None
    assert obj.width == pytest.approx(160.0)
    assert obj.height == pytest.approx(90.0)


# --------------------------------------------------------------------------
# 5. save/load roundtrip
# --------------------------------------------------------------------------


def test_save_load_roundtrip_image_fields(qapp: Any, project_dir: Path, tmp_path: Path) -> None:
    doc = Document()
    save_document(doc, project_dir)
    src = tmp_path / "rt.png"
    _make_source_image(src, w=40, h=30)
    rel = import_image(doc, str(src))

    obj = ImageObject(
        id=doc.new_id(),
        src=rel,
        x=1.0,
        y=2.0,
        width=40.0,
        height=30.0,
        brightness=0.25,
        contrast=-0.1,
        crop=[1.0, 2.0, 20.0, 15.0],
    )
    doc.add_object(obj)
    save_document(doc, project_dir)

    loaded = load_document(project_dir)
    assert loaded.base_dir == str(project_dir.resolve())
    loaded_obj = loaded.object_by_id(obj.id)
    assert loaded_obj is not None
    assert loaded_obj.src == rel
    assert loaded_obj.brightness == 0.25
    assert loaded_obj.contrast == -0.1
    assert loaded_obj.crop == [1.0, 2.0, 20.0, 15.0]

    abs_path = resolve_asset_path(loaded, loaded_obj.src)
    assert Path(abs_path).exists()


# --------------------------------------------------------------------------
# 6. 存在しない src でプレースホルダかつ paint 非クラッシュ
# --------------------------------------------------------------------------


def test_missing_src_gives_placeholder_and_paint_does_not_crash(
    qapp: Any, project_dir: Path
) -> None:
    doc = Document()
    save_document(doc, project_dir)
    obj = ImageObject(
        id=doc.new_id(), src="assets/does_not_exist.png", x=0, y=0, width=50, height=40
    )
    doc.add_object(obj)

    item = create_item(obj, doc)
    assert item._load_failed is True
    assert item._display_pixmap.isNull()

    image = QImage(64, 64, QImage.Format.Format_ARGB32)
    image.fill(0)
    painter = QPainter(image)
    try:
        item.paint(painter, None, None)  # クラッシュしないこと
    finally:
        painter.end()
