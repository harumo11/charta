"""`Sam3MaskController.commit_mask` のヘッドレステスト（contract_sam3_v2 J-1）。

オンキャンバス編集モード化（contract_sam3_v2）に伴い、旧ダイアログ
（`Sam3MaskDialog`）関連のテストは削除した。本ファイルはダイアログに依存しない
確定経路（`commit_mask()`）・メニュー有効/無効判定のみを検証する。推論
（SAM3 モデル）は使わない。

`Sam3MaskController` はコンストラクタで `MaskEditPanel`（オンキャンバス編集
モードの操作パネル）を要求するようになったため、各テストで生成して渡す。
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import numpy as np
import pytest
import shiboken6
from PIL import Image
from PySide6.QtWidgets import QMenu

from app.model.objects import ImageObject
from app.model.serialize import import_image, save_document
from app.ui.controllers.sam3_masking import Sam3MaskController
from app.ui.main_window import MainWindow
from app.ui.mask_edit_panel import MaskEditPanel


def _make_source_image(path: Path, w: int = 20, h: int = 16) -> None:
    x = np.linspace(0, 255, w, dtype=np.uint8)
    rgb = np.stack([np.tile(x, (h, 1))] * 3, axis=-1).astype(np.uint8)
    Image.fromarray(rgb, mode="RGB").save(path)


@pytest.fixture
def project_dir(tmp_path: Path) -> Path:
    return tmp_path / "proj"


@pytest.fixture
def window(qapp: Any) -> Any:
    """`Sam3MaskController` が要求する `_temp_autosave_path()` 等を持つ実 MainWindow。"""
    w = MainWindow()
    yield w
    if shiboken6.isValid(w):
        w.close()


def _make_controller(window: Any) -> Sam3MaskController:
    """`MaskEditPanel` を渡して `Sam3MaskController` を生成する（契約 H-3 の新コンストラクタ）。"""
    return Sam3MaskController(window, window.scene, window.undo_stack, MaskEditPanel())


# --------------------------------------------------------------------------
# commit_mask: マスクあり（マクロ push・undo/redo・prompt 反映）
# --------------------------------------------------------------------------


def test_commit_mask_creates_file_and_macro_undo_redo(
    window: Any, project_dir: Path, tmp_path: Path
) -> None:
    doc = window.scene.document
    save_document(doc, project_dir)

    src = tmp_path / "src.png"
    _make_source_image(src, w=20, h=16)
    rel = import_image(doc, str(src))

    obj = ImageObject(id=doc.new_id(), src=rel, x=0.0, y=0.0, width=20.0, height=16.0)
    doc.add_object(obj)

    controller = _make_controller(window)

    mask = np.zeros((16, 20), dtype=np.uint8)
    mask[:, 10:] = 255

    count_before = window.undo_stack.count()
    controller.commit_mask(obj, mask, "#00FF00", 0.7, prompt="cat")

    assert (project_dir / "assets" / "mask_001.png").exists()
    assert obj.mask_src == "assets/mask_001.png"
    assert obj.mask_color == "#00FF00"
    assert obj.mask_opacity == pytest.approx(0.7)
    assert obj.mask_enabled is True
    assert obj.mask_prompt == "cat"
    assert window.undo_stack.count() == count_before + 1, "マクロで1コマンドにまとまること"

    window.undo_stack.undo()
    assert obj.mask_src is None
    assert obj.mask_color == "#FFFFFF"
    assert obj.mask_opacity == pytest.approx(0.8)
    assert obj.mask_enabled is True
    assert obj.mask_prompt == "", "undo 1 回で mask_prompt を含む全フィールドが復元されること"

    window.undo_stack.redo()
    assert obj.mask_src == "assets/mask_001.png"
    assert obj.mask_color == "#00FF00"
    assert obj.mask_opacity == pytest.approx(0.7)
    assert obj.mask_enabled is True
    assert obj.mask_prompt == "cat"


def test_commit_mask_prompt_defaults_to_empty_string(
    window: Any, project_dir: Path, tmp_path: Path
) -> None:
    """`prompt` を省略した場合は `mask_prompt` が空文字列になること。"""
    doc = window.scene.document
    save_document(doc, project_dir)

    src = tmp_path / "src_noprompt.png"
    _make_source_image(src, w=12, h=10)
    rel = import_image(doc, str(src))

    obj = ImageObject(id=doc.new_id(), src=rel, x=0.0, y=0.0, width=12.0, height=10.0)
    doc.add_object(obj)

    controller = _make_controller(window)
    mask = np.zeros((10, 12), dtype=np.uint8)
    controller.commit_mask(obj, mask, "#0000FF", 0.5)

    assert obj.mask_prompt == ""


# --------------------------------------------------------------------------
# commit_mask: マスク解除（mask=None）
# --------------------------------------------------------------------------


def test_commit_mask_none_clears_and_undo_restores(
    window: Any, project_dir: Path, tmp_path: Path
) -> None:
    doc = window.scene.document
    save_document(doc, project_dir)

    src = tmp_path / "src2.png"
    _make_source_image(src, w=12, h=10)
    rel = import_image(doc, str(src))

    obj = ImageObject(id=doc.new_id(), src=rel, x=0.0, y=0.0, width=12.0, height=10.0)
    doc.add_object(obj)

    controller = _make_controller(window)
    mask = np.zeros((10, 12), dtype=np.uint8)
    controller.commit_mask(obj, mask, "#0000FF", 0.5, prompt="dog")
    assert obj.mask_src == "assets/mask_001.png"
    assert obj.mask_prompt == "dog"

    controller.commit_mask(obj, None, "#0000FF", 0.5)
    assert obj.mask_src is None

    window.undo_stack.undo()
    assert obj.mask_src == "assets/mask_001.png"
    assert obj.mask_prompt == "dog"


# --------------------------------------------------------------------------
# commit_mask: base_dir 未設定 → 一時ディレクトリを自動設定
# --------------------------------------------------------------------------


def test_commit_mask_sets_temp_base_dir_when_missing(window: Any) -> None:
    doc = window.scene.document
    assert doc.base_dir is None

    obj = ImageObject(id=doc.new_id(), src="assets/dummy.png", x=0.0, y=0.0, width=8.0, height=8.0)
    doc.add_object(obj)

    controller = _make_controller(window)
    mask = np.zeros((8, 8), dtype=np.uint8)
    controller.commit_mask(obj, mask, "#FFFFFF", 0.8)

    assert doc.base_dir is not None
    assert Path(doc.base_dir, "assets", "mask_001.png").exists()
    assert obj.mask_src == "assets/mask_001.png"
    assert obj.mask_prompt == ""


# --------------------------------------------------------------------------
# make_menu_action: sam 未導入時は無効化
# --------------------------------------------------------------------------


def test_make_menu_action_disabled_when_sam3_unavailable(
    window: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr("app.ai.sam3.is_available", lambda: False)

    controller = _make_controller(window)
    menu = QMenu()
    action = controller.make_menu_action(menu)

    assert action.isEnabled() is False
    assert action.toolTip() != ""


def test_make_menu_action_enabled_when_sam3_available(
    window: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr("app.ai.sam3.is_available", lambda: True)

    controller = _make_controller(window)
    menu = QMenu()
    action = controller.make_menu_action(menu)

    assert action.isEnabled() is True
