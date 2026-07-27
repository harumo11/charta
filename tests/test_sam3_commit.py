"""`Sam3MaskController`・`Sam3MaskDialog` のヘッドレステスト（contract_sam3 E-2）。

推論（SAM3 モデル）は使わず、確定経路（`commit_mask`）・メニュー活性/非活性・
ダイアログへの候補注入（`set_candidates_for_test`）のみを offscreen で検証する。
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import numpy as np
import pytest
import shiboken6
from PIL import Image
from PySide6.QtWidgets import QDialogButtonBox, QMenu

from app.ai.sam3 import MaskCandidate, combined_mask_u8
from app.model.document import Document
from app.model.objects import ImageObject
from app.model.serialize import import_image, save_document
from app.ui.controllers.sam3_masking import Sam3MaskController
from app.ui.main_window import MainWindow
from app.ui.sam3_mask_dialog import Sam3MaskDialog


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


# --------------------------------------------------------------------------
# commit_mask: マスクあり（マクロ push・undo/redo）
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

    controller = Sam3MaskController(window, window.scene, window.undo_stack)

    mask = np.zeros((16, 20), dtype=np.uint8)
    mask[:, 10:] = 255

    count_before = window.undo_stack.count()
    controller.commit_mask(obj, mask, "#00FF00", 0.7)

    assert (project_dir / "assets" / "mask_001.png").exists()
    assert obj.mask_src == "assets/mask_001.png"
    assert obj.mask_color == "#00FF00"
    assert obj.mask_opacity == pytest.approx(0.7)
    assert obj.mask_enabled is True
    assert window.undo_stack.count() == count_before + 1, "マクロで1コマンドにまとまること"

    window.undo_stack.undo()
    assert obj.mask_src is None
    assert obj.mask_color == "#FFFFFF"
    assert obj.mask_opacity == pytest.approx(0.8)
    assert obj.mask_enabled is True

    window.undo_stack.redo()
    assert obj.mask_src == "assets/mask_001.png"
    assert obj.mask_color == "#00FF00"
    assert obj.mask_opacity == pytest.approx(0.7)
    assert obj.mask_enabled is True


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

    controller = Sam3MaskController(window, window.scene, window.undo_stack)
    mask = np.zeros((10, 12), dtype=np.uint8)
    controller.commit_mask(obj, mask, "#0000FF", 0.5)
    assert obj.mask_src == "assets/mask_001.png"

    controller.commit_mask(obj, None, "#0000FF", 0.5)
    assert obj.mask_src is None

    window.undo_stack.undo()
    assert obj.mask_src == "assets/mask_001.png"


# --------------------------------------------------------------------------
# commit_mask: base_dir 未設定 → 一時ディレクトリを自動設定
# --------------------------------------------------------------------------


def test_commit_mask_sets_temp_base_dir_when_missing(window: Any) -> None:
    doc = window.scene.document
    assert doc.base_dir is None

    obj = ImageObject(id=doc.new_id(), src="assets/dummy.png", x=0.0, y=0.0, width=8.0, height=8.0)
    doc.add_object(obj)

    controller = Sam3MaskController(window, window.scene, window.undo_stack)
    mask = np.zeros((8, 8), dtype=np.uint8)
    controller.commit_mask(obj, mask, "#FFFFFF", 0.8)

    assert doc.base_dir is not None
    assert Path(doc.base_dir, "assets", "mask_001.png").exists()
    assert obj.mask_src == "assets/mask_001.png"


# --------------------------------------------------------------------------
# make_menu_action: sam 未導入時は無効化
# --------------------------------------------------------------------------


def test_make_menu_action_disabled_when_sam3_unavailable(
    window: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr("app.ai.sam3.is_available", lambda: False)

    controller = Sam3MaskController(window, window.scene, window.undo_stack)
    menu = QMenu()
    action = controller.make_menu_action(menu)

    assert action.isEnabled() is False
    assert action.toolTip() != ""


def test_make_menu_action_enabled_when_sam3_available(
    window: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr("app.ai.sam3.is_available", lambda: True)

    controller = Sam3MaskController(window, window.scene, window.undo_stack)
    menu = QMenu()
    action = controller.make_menu_action(menu)

    assert action.isEnabled() is True


# --------------------------------------------------------------------------
# Sam3MaskDialog: 推論なしで候補注入 → result_payload() が combined_mask_u8 と一致
# --------------------------------------------------------------------------


def test_dialog_result_payload_matches_combined_mask(
    qapp: Any, project_dir: Path, tmp_path: Path
) -> None:
    doc = Document()
    save_document(doc, project_dir)

    w_px, h_px = 16, 12
    src = tmp_path / "dlg_src.png"
    _make_source_image(src, w=w_px, h=h_px)
    rel = import_image(doc, str(src))

    obj = ImageObject(id=doc.new_id(), src=rel, x=0.0, y=0.0, width=float(w_px), height=float(h_px))
    doc.add_object(obj)

    dialog = Sam3MaskDialog(None, doc, obj)
    try:
        m1 = np.zeros((h_px, w_px), dtype=bool)
        m1[0:4, 0:4] = True
        m2 = np.zeros((h_px, w_px), dtype=bool)
        m2[8:, 8:] = True
        candidates = [
            MaskCandidate(mask=m1, box=(0.0, 0.0, 4.0, 4.0), score=0.9, accepted=True),
            MaskCandidate(
                mask=m2,
                box=(8.0, 8.0, float(w_px), float(h_px)),
                score=0.6,
                accepted=False,
            ),
        ]
        dialog.set_candidates_for_test(candidates)

        button_box = dialog.findChild(QDialogButtonBox)
        if button_box is not None:
            ok_button = button_box.button(QDialogButtonBox.StandardButton.Ok)
            if ok_button is not None:
                assert ok_button.isEnabled() is True, "採用候補が1つ以上あれば OK が有効"

        mask, color, opacity = dialog.result_payload()
        expected = combined_mask_u8(candidates, (w_px, h_px))

        assert mask is not None
        assert np.array_equal(mask, expected)
        assert isinstance(color, (str, type(None)))
        assert isinstance(opacity, float)
    finally:
        dialog.done(0)  # スレッド停止（quit/wait）を確実に発火させる


def test_dialog_result_payload_none_mask_when_no_candidates(
    qapp: Any, project_dir: Path, tmp_path: Path
) -> None:
    doc = Document()
    save_document(doc, project_dir)

    w_px, h_px = 10, 8
    src = tmp_path / "dlg_src_empty.png"
    _make_source_image(src, w=w_px, h=h_px)
    rel = import_image(doc, str(src))

    obj = ImageObject(id=doc.new_id(), src=rel, x=0.0, y=0.0, width=float(w_px), height=float(h_px))
    doc.add_object(obj)

    dialog = Sam3MaskDialog(None, doc, obj)
    try:
        dialog.set_candidates_for_test([])
        mask, _color, _opacity = dialog.result_payload()
        assert mask is None
    finally:
        dialog.done(0)
