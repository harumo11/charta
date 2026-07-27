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
    stack.push(AddObjectCommand(scene.document, obj))

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
    stack.push(AddObjectCommand(scene.document, obj))

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


# --------------------------------------------------------------------------
# 7. crop モードの UI 配線（Enter/Esc・外側クリック・ツール切替・scene 追跡）
# --------------------------------------------------------------------------


def _scene_with_image(
    project_dir: Path,
    tmp_path: Path,
    x: float = 0.0,
    y: float = 0.0,
    width: float = 80.0,
    height: float = 60.0,
    rotation: float = 0.0,
) -> tuple[CanvasScene, Any, Any, ImageItem]:
    """80x60 px の画像 1 枚を指定ジオメトリで配置した (scene, undo_stack, obj, item) を作る。"""
    from PySide6.QtGui import QUndoStack

    doc = Document()
    save_document(doc, project_dir)
    src = tmp_path / "crop_ui_src.png"
    _make_source_image(src, w=80, h=60)
    rel = import_image(doc, str(src))

    obj = ImageObject(
        id=doc.new_id(), src=rel, x=x, y=y, width=width, height=height, rotation=rotation
    )
    scene = CanvasScene(doc)
    stack = QUndoStack()
    scene.set_undo_stack(stack)
    stack.push(AddObjectCommand(scene.document, obj))
    item = scene.item_for(obj)
    assert isinstance(item, ImageItem)
    return scene, stack, obj, item


def _key_event(key: Any) -> Any:
    from PySide6.QtCore import QEvent, Qt
    from PySide6.QtGui import QKeyEvent

    return QKeyEvent(QEvent.Type.KeyPress, key, Qt.KeyboardModifier.NoModifier)


def _press_event_at(view: Any, scene_x: float, scene_y: float) -> Any:
    from PySide6.QtCore import QEvent, QPointF, Qt
    from PySide6.QtGui import QMouseEvent

    local = QPointF(view.mapFromScene(QPointF(scene_x, scene_y)))
    return QMouseEvent(
        QEvent.Type.MouseButtonPress,
        local,
        local,
        Qt.MouseButton.LeftButton,
        Qt.MouseButton.LeftButton,
        Qt.KeyboardModifier.NoModifier,
    )


def test_crop_enter_key_commits_via_view(qapp: Any, project_dir: Path, tmp_path: Path) -> None:
    from PySide6.QtCore import Qt

    from app.scene.canvas_view import CanvasView

    scene, stack, obj, item = _scene_with_image(project_dir, tmp_path)
    view = CanvasView(scene)

    item.begin_crop()
    assert scene.active_crop_item() is item
    item.set_crop_rect(10.0, 5.0, 30.0, 20.0)

    view.keyPressEvent(_key_event(Qt.Key.Key_Return))
    assert obj.crop == [10.0, 5.0, 30.0, 20.0]
    assert item._crop_mode is False
    assert scene.active_crop_item() is None


def test_crop_escape_key_cancels_via_view(qapp: Any, project_dir: Path, tmp_path: Path) -> None:
    from PySide6.QtCore import Qt

    from app.scene.canvas_view import CanvasView

    scene, stack, obj, item = _scene_with_image(project_dir, tmp_path)
    view = CanvasView(scene)
    undo_count_before = stack.count()

    item.begin_crop()
    item.set_crop_rect(10.0, 5.0, 30.0, 20.0)

    view.keyPressEvent(_key_event(Qt.Key.Key_Escape))
    assert obj.crop is None
    assert item._crop_mode is False
    assert scene.active_crop_item() is None
    assert stack.count() == undo_count_before, "キャンセルでは undo 履歴が増えないこと"


def test_crop_outside_click_commits(qapp: Any, project_dir: Path, tmp_path: Path) -> None:
    from app.scene.canvas_view import CanvasView

    scene, stack, obj, item = _scene_with_image(project_dir, tmp_path)
    view = CanvasView(scene)
    view.resize(400, 300)

    item.begin_crop()
    item.set_crop_rect(10.0, 5.0, 30.0, 20.0)

    view.mousePressEvent(_press_event_at(view, 500.0, 500.0))  # 画像（80x60）の外側
    assert obj.crop == [10.0, 5.0, 30.0, 20.0]
    assert item._crop_mode is False
    assert scene.active_crop_item() is None


def test_crop_press_inside_does_not_commit(qapp: Any, project_dir: Path, tmp_path: Path) -> None:
    from app.scene.canvas_view import CanvasView

    scene, stack, obj, item = _scene_with_image(project_dir, tmp_path)
    view = CanvasView(scene)
    view.resize(400, 300)

    item.begin_crop()
    item.set_crop_rect(10.0, 5.0, 30.0, 20.0)

    view.mousePressEvent(_press_event_at(view, 20.0, 10.0))  # 画像内（オーバーレイ上）
    assert item._crop_mode is True, "crop 対象上の押下ではモードを終了しないこと"
    assert obj.crop is None


def test_crop_tool_switch_commits(qapp: Any, project_dir: Path, tmp_path: Path) -> None:
    from app.tools.tool_manager import ToolManager

    scene, stack, obj, item = _scene_with_image(project_dir, tmp_path)
    tm = ToolManager(scene)

    item.begin_crop()
    item.set_crop_rect(10.0, 5.0, 30.0, 20.0)

    tm.set_tool("rect")
    assert obj.crop == [10.0, 5.0, 30.0, 20.0]
    assert item._crop_mode is False
    assert scene.active_crop_item() is None


def test_crop_handle_drag_stays_in_image_local_coords(
    qapp: Any, project_dir: Path, tmp_path: Path
) -> None:
    """原点以外に置いた画像の crop ハンドルをドラッグしても矩形が飛ばないこと。

    リグレッション: `BoxHandleSet._drag_resize` が新矩形位置を scene 座標へ
    写像していたため、ImageItem の子である CropOverlay（画像ローカル座標を
    期待）では画像の scene 位置ぶんずれて「四角が飛ぶ」バグがあった。
    """
    from PySide6.QtCore import QPointF

    scene, stack, obj, item = _scene_with_image(project_dir, tmp_path, x=100.0, y=50.0)

    item.begin_crop()
    overlay = item._crop_overlay
    assert overlay is not None and overlay._handles is not None
    handles = overlay._handles

    # 右下ハンドルを画像右下 scene(180,110) から scene(140,80) へドラッグ → crop 40x30
    handles.begin_drag("br", QPointF(180.0, 110.0))
    handles.drag_to("br", QPointF(140.0, 80.0))
    handles.end_drag("br")
    assert item._crop_overlay_px == [0.0, 0.0, 40.0, 30.0]
    assert overlay.pos().x() == pytest.approx(0.0), "オーバーレイが画像ローカル座標に留まること"
    assert overlay.pos().y() == pytest.approx(0.0)

    # 左上ハンドルを scene(100,50) から scene(110,60) へドラッグ → crop [10,10,30,20]
    handles.begin_drag("tl", QPointF(100.0, 50.0))
    handles.drag_to("tl", QPointF(110.0, 60.0))
    handles.end_drag("tl")
    assert item._crop_overlay_px == [10.0, 10.0, 30.0, 20.0]
    assert overlay.pos().x() == pytest.approx(10.0)
    assert overlay.pos().y() == pytest.approx(10.0)

    item.commit_crop()
    assert obj.crop == [10.0, 10.0, 30.0, 20.0]


def test_crop_handle_drag_out_of_bounds_is_clamped(
    qapp: Any, project_dir: Path, tmp_path: Path
) -> None:
    """ハンドルを画像境界の外までドラッグしても crop が元画像内にクランプされること。

    リグレッション（レビュー所見 major）: `CropOverlay.set_live_rect` が
    クランプせず `_crop_overlay_px` を直接書いていたため、境界外ドラッグで
    実画像より大きい crop が確定され、出力が引き伸ばされて歪んだ。
    """
    from PySide6.QtCore import QPointF

    scene, stack, obj, item = _scene_with_image(project_dir, tmp_path, x=100.0, y=50.0)

    item.begin_crop()
    handles = item._crop_overlay._handles

    # まず内側へ: tl を scene(100,50)→(110,60) で crop [10,10,70,50]
    handles.begin_drag("tl", QPointF(100.0, 50.0))
    handles.drag_to("tl", QPointF(110.0, 60.0))
    handles.end_drag("tl")
    assert item._crop_overlay_px == [10.0, 10.0, 70.0, 50.0]

    # br を画像右下 scene(180,110) から大きく外側 scene(500,500) へ → クランプで不変
    handles.begin_drag("br", QPointF(180.0, 110.0))
    handles.drag_to("br", QPointF(500.0, 500.0))
    handles.end_drag("br")
    assert item._crop_overlay_px == [10.0, 10.0, 70.0, 50.0]

    item.commit_crop()
    assert obj.crop == [10.0, 10.0, 70.0, 50.0]
    assert obj.width == pytest.approx(70.0)
    assert obj.height == pytest.approx(50.0)


def test_crop_handle_drag_on_scaled_image(qapp: Any, project_dir: Path, tmp_path: Path) -> None:
    """表示サイズが原画 px と異なる（x2, x1.5 スケール済み）画像でのハンドルドラッグ。

    ローカル座標→原画 px の換算（`_local_to_px`）が軸ごとに正しいことを固定する。
    """
    from PySide6.QtCore import QPointF

    scene, stack, obj, item = _scene_with_image(
        project_dir, tmp_path, x=100.0, y=50.0, width=160.0, height=90.0
    )

    item.begin_crop()
    handles = item._crop_overlay._handles

    # br を画像右下 scene(260,140) から scene(180,110) へ → ローカル(80,60) = px(40,40)
    handles.begin_drag("br", QPointF(260.0, 140.0))
    handles.drag_to("br", QPointF(180.0, 110.0))
    handles.end_drag("br")
    assert item._crop_overlay_px == [0.0, 0.0, 40.0, 40.0]

    item.commit_crop()
    assert obj.crop == [0.0, 0.0, 40.0, 40.0]
    # 軸ごとの表示スケール維持: width = 40*(160/80) = 80, height = 40*(90/60) = 60
    assert obj.width == pytest.approx(80.0)
    assert obj.height == pytest.approx(60.0)


def test_crop_handle_drag_on_rotated_image(qapp: Any, project_dir: Path, tmp_path: Path) -> None:
    """180 度回転した画像でもハンドルドラッグが画像ローカル座標で正しく動くこと。"""
    from PySide6.QtCore import QPointF

    scene, stack, obj, item = _scene_with_image(
        project_dir, tmp_path, x=100.0, y=50.0, rotation=180.0
    )

    item.begin_crop()
    handles = item._crop_overlay._handles

    # 180 度回転では scene = (180 - lx, 110 - ly)。br（ローカル(80,60)）は
    # scene(100,50) にあり、scene(140,80)（画像中心）へ動かすとローカル(40,30)。
    handles.begin_drag("br", QPointF(100.0, 50.0))
    handles.drag_to("br", QPointF(140.0, 80.0))
    handles.end_drag("br")
    assert item._crop_overlay_px == [0.0, 0.0, 40.0, 30.0]

    item.commit_crop()
    assert obj.crop == [0.0, 0.0, 40.0, 30.0]


def test_crop_disables_image_move_while_active(
    qapp: Any, project_dir: Path, tmp_path: Path
) -> None:
    """crop モード中は ItemIsMovable が外れ、終了で復元されること（レビュー所見 minor）。"""
    from PySide6.QtWidgets import QGraphicsItem

    scene, stack, obj, item = _scene_with_image(project_dir, tmp_path)
    movable = QGraphicsItem.GraphicsItemFlag.ItemIsMovable

    assert item.flags() & movable
    item.begin_crop()
    assert not (item.flags() & movable)

    # crop 中のモデル変更（sync_from_model 経由）でも移動禁止が維持されること
    scene.document.set_values(obj, {"opacity": 0.5})
    assert not (item.flags() & movable)

    item.cancel_crop()
    assert item.flags() & movable


def test_crop_same_tool_reselect_does_not_commit(
    qapp: Any, project_dir: Path, tmp_path: Path
) -> None:
    """既にアクティブなツールの再選択では crop を確定しないこと（レビュー所見 nit）。"""
    from app.tools.tool_manager import ToolManager

    scene, stack, obj, item = _scene_with_image(project_dir, tmp_path)
    tm = ToolManager(scene)
    assert tm.current_tool() == "select"

    item.begin_crop()
    item.set_crop_rect(10.0, 5.0, 30.0, 20.0)

    tm.set_tool("select")  # 同一ツールの再選択 → 確定しない
    assert item._crop_mode is True
    assert obj.crop is None

    tm.set_tool("rect")  # 実際のツール変更 → 確定する
    assert obj.crop == [10.0, 5.0, 30.0, 20.0]
    assert item._crop_mode is False


def test_crop_second_image_begin_commits_first(
    qapp: Any, project_dir: Path, tmp_path: Path
) -> None:
    """crop モード中に別画像で crop を開始すると、先の画像は確定されて取り残されないこと。"""
    scene, stack, obj_a, item_a = _scene_with_image(project_dir, tmp_path)
    obj_b = ImageObject(
        id=scene.document.new_id(), src=obj_a.src, x=200.0, y=0.0, width=80.0, height=60.0
    )
    stack.push(AddObjectCommand(scene.document, obj_b))
    item_b = scene.item_for(obj_b)
    assert isinstance(item_b, ImageItem)

    item_a.begin_crop()
    item_a.set_crop_rect(10.0, 5.0, 30.0, 20.0)

    item_b.begin_crop()
    assert item_a._crop_mode is False, "先行の crop が確定されてモード終了していること"
    assert obj_a.crop == [10.0, 5.0, 30.0, 20.0]
    assert scene.active_crop_item() is item_b

    item_b.cancel_crop()
    assert scene.active_crop_item() is None


def test_crop_tracking_cleared_on_object_removal(
    qapp: Any, project_dir: Path, tmp_path: Path
) -> None:
    scene, stack, obj, item = _scene_with_image(project_dir, tmp_path)

    item.begin_crop()
    assert scene.active_crop_item() is item

    scene.document.remove_object(obj)  # crop モード中の削除で stale 参照を残さない
    assert scene.active_crop_item() is None
