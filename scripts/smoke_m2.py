"""Milestone 2 ヘッドレス統合スモークテスト（M2契約 §6）。

`QT_QPA_PLATFORM=offscreen uv run python scripts/smoke_m2.py` で実行する。
契約 §5 の主要経路（import→表示→明るさ→crop→save/load）を通し、
失敗時は例外で非0終了する。`scripts/smoke_m1.py` は変更しない。
"""

from __future__ import annotations

import sys
import tempfile
from pathlib import Path

_REPO_ROOT = str(Path(__file__).resolve().parent.parent)
if _REPO_ROOT not in sys.path:
    sys.path.insert(0, _REPO_ROOT)

import numpy as np  # noqa: E402
from PIL import Image  # noqa: E402
from PySide6.QtGui import QImage, QUndoStack  # noqa: E402
from PySide6.QtWidgets import QApplication  # noqa: E402

from app.commands.commands import AddObjectCommand, SetPropertyCommand  # noqa: E402
from app.model.document import Document  # noqa: E402
from app.model.objects import ImageObject  # noqa: E402
from app.model.serialize import (  # noqa: E402
    import_image,
    load_document,
    resolve_asset_path,
    save_document,
)
from app.scene.canvas_scene import CanvasScene  # noqa: E402
from app.scene.items import create_item  # noqa: E402
from app.scene.items.image_item import ImageItem  # noqa: E402


def _make_source_image(path: Path, w: int = 60, h: int = 40) -> None:
    x = np.linspace(0, 255, w, dtype=np.uint8)
    row = np.tile(x, (h, 1))
    rgb = np.stack([row, row, row], axis=-1).astype(np.uint8)
    Image.fromarray(rgb, mode="RGB").save(path)


def _pixmap_mean(pixmap) -> float:  # noqa: ANN001
    qimage = pixmap.toImage().convertToFormat(QImage.Format.Format_RGBA8888)
    w, h = qimage.width(), qimage.height()
    buf = bytes(qimage.constBits())[: w * h * 4]
    arr = np.frombuffer(buf, dtype=np.uint8).reshape(h, w, 4)
    return float(arr[..., :3].astype(np.float64).mean())


def main() -> None:
    app = QApplication.instance() or QApplication([])
    assert app is not None

    with tempfile.TemporaryDirectory() as tmp:
        project_dir = Path(tmp) / "smoke_m2_project"
        src_path = Path(tmp) / "source.png"

        doc = Document()

        # 1. import_image: base_dir 未設定なら RuntimeError。
        try:
            import_image(doc, str(src_path))
        except RuntimeError:
            pass
        else:
            raise AssertionError("base_dir 未設定なのに import_image が例外を投げなかった")

        # 保存して base_dir を設定 -> import_image -> 表示。
        save_document(doc, project_dir)
        _make_source_image(src_path, w=60, h=40)
        rel = import_image(doc, str(src_path))
        assert rel == "assets/img_001.png", f"想定と異なる相対パス: {rel}"
        assert (project_dir / "assets" / "img_001.png").exists()

        obj = ImageObject(id=doc.new_id(), src=rel, x=10, y=10, width=60, height=40)

        scene = CanvasScene(doc)
        stack = QUndoStack()
        scene.set_undo_stack(stack)
        stack.push(AddObjectCommand(scene, obj))

        item = scene.item_for(obj)
        assert isinstance(item, ImageItem), "item が ImageItem でない"
        assert not item._display_pixmap.isNull(), "_display_pixmap が null"

        # 2. create_item(obj, document) ファクトリ経路も確認。
        standalone_item = create_item(obj, doc)
        assert isinstance(standalone_item, ImageItem)

        # 3. brightness で表示画素平均が上がること。
        base_mean = _pixmap_mean(item._display_pixmap)
        stack.push(SetPropertyCommand(scene, obj, "brightness", 0.5, obj.brightness))
        bright_mean = _pixmap_mean(item._display_pixmap)
        assert bright_mean > base_mean + 10.0, "brightness 変更が反映されていない"

        # 4. crop begin/set/commit -> undo。
        item.begin_crop()
        assert item._crop_mode is True
        item.set_crop_rect(5.0, 5.0, 20.0, 15.0)
        item.commit_crop()
        assert obj.crop == [5.0, 5.0, 20.0, 15.0], f"crop が反映されていない: {obj.crop}"
        stack.undo()
        assert obj.crop is None, "crop の undo が効いていない"
        stack.redo()
        assert obj.crop == [5.0, 5.0, 20.0, 15.0]

        # 5. save/load roundtrip。
        save_document(doc, project_dir)
        loaded = load_document(project_dir)
        loaded_obj = loaded.object_by_id(obj.id)
        assert loaded_obj is not None
        assert loaded_obj.src == obj.src
        assert loaded_obj.brightness == obj.brightness
        assert loaded_obj.crop == obj.crop
        abs_path = resolve_asset_path(loaded, loaded_obj.src)
        assert Path(abs_path).exists(), "resolve_asset_path が実ファイルを指していない"

        # 6. 存在しない src はプレースホルダで例外にならないこと。
        missing_obj = ImageObject(
            id=doc.new_id(), src="assets/nope.png", x=0, y=0, width=20, height=20
        )
        missing_item = create_item(missing_obj, doc)
        assert missing_item._load_failed is True
        assert missing_item._display_pixmap.isNull()

    print("smoke_m2: OK (import/display/brightness/crop/save-load all passed)")


if __name__ == "__main__":
    try:
        main()
    except Exception:
        import traceback

        traceback.print_exc()
        sys.exit(1)
    sys.exit(0)
