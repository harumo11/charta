"""Sam3MaskController: SAM3 選択的マスキングのメニュー配線・確定処理（契約 D-1）。

「SAM3 マスク…」メニュー項目の有効/無効判定、ダイアログの起動、および
ダイアログに依存しないヘッドレステスト可能な確定経路 `commit_mask()` を提供する。
SAM3 推論そのものは `app.ai.sam3`（Qt 非依存層）と `Sam3MaskDialog` の責務であり、
本コントローラは確定処理（`assets/` へのマスク保存 + `SetPropertyCommand` の
undo マクロ化）のみを担う。
"""

from __future__ import annotations

from typing import TYPE_CHECKING

import numpy as np
from PySide6.QtGui import QAction, QUndoStack
from PySide6.QtWidgets import QDialog, QMenu, QMessageBox

from app.ai import sam3
from app.commands.commands import SetPropertyCommand
from app.model.objects import ImageObject
from app.model.serialize import save_mask_png
from app.ui.sam3_mask_dialog import Sam3MaskDialog

if TYPE_CHECKING:
    from app.scene.canvas_scene import CanvasScene
    from app.ui.main_window import MainWindow

_MENU_LABEL = "SAM3 マスク…"
_DISABLED_TOOLTIP = "uv sync --group sam で torch/transformers を導入すると使えます"
_MACRO_TEXT = "SAM3 マスク"


class Sam3MaskController:
    """SAM3 マスクダイアログの起動とマスク確定（undo マクロ）を担当する。"""

    def __init__(self, window: MainWindow, scene: CanvasScene, undo_stack: QUndoStack) -> None:
        self._window = window
        self._scene = scene
        self._undo_stack = undo_stack

    def make_menu_action(self, menu: QMenu) -> QAction:
        """「SAM3 マスク…」QAction を menu に追加して返す。

        `app.ai.sam3.is_available()` が False（torch/transformers 未導入）の場合は
        無効化しツールチップで導入方法を案内する。
        """
        action = menu.addAction(_MENU_LABEL, self.open_dialog_action)
        if not sam3.is_available():
            action.setEnabled(False)
            action.setToolTip(_DISABLED_TOOLTIP)
        return action

    def open_dialog_action(self) -> None:
        """単一の ImageObject 選択を確認し、ダイアログを開いて確定する。

        単一選択でない、または画像でない場合は案内のみで return する。対象画像が
        crop モード中の場合は、ダイアログのプレビュー（crop 前の原画）と齟齬が
        出ないよう先に `commit_crop()` で確定させる。
        """
        selected = self._scene.selected_objects()
        if len(selected) != 1 or not isinstance(selected[0], ImageObject):
            QMessageBox.information(self._window, "SAM3 マスク", "画像を 1 つ選択してください")
            return
        obj = selected[0]

        active_crop_item_getter = getattr(self._scene, "active_crop_item", None)
        active_crop_item = active_crop_item_getter() if callable(active_crop_item_getter) else None
        if active_crop_item is not None and getattr(active_crop_item, "obj", None) is obj:
            commit_crop = getattr(active_crop_item, "commit_crop", None)
            if callable(commit_crop):
                commit_crop()

        document = self._scene.document
        dialog = Sam3MaskDialog(self._window, document, obj)
        if dialog.exec() == QDialog.DialogCode.Accepted:
            mask, color, opacity = dialog.result_payload()
            self.commit_mask(obj, mask, color, opacity)

    def commit_mask(
        self,
        obj: ImageObject,
        mask: np.ndarray | None,
        color: str | None,
        opacity: float,
    ) -> None:
        """マスクを確定する（ダイアログ非依存の公開 API・ヘッドレステスト可能）。

        `mask` が None ならマスク解除（`mask_src` を None にする単発の
        `SetPropertyCommand` のみ。他フィールドは保持する）。マスクありの場合は
        `document.base_dir` が未設定なら `ImageImportController` と同じ方針で
        一時ディレクトリを割り当ててから `save_mask_png()` で保存し、
        `mask_src`→`mask_color`→`mask_opacity`→`mask_enabled(True)` の順に
        `SetPropertyCommand` を 1 つの undo マクロへまとめて push する。
        """
        document = self._scene.document

        if mask is None:
            if obj.mask_src is None:
                return
            self._undo_stack.push(SetPropertyCommand(document, obj, "mask_src", None, obj.mask_src))
            return

        if document.base_dir is None:
            document.base_dir = self._window._temp_autosave_path()

        mask_src = save_mask_png(document, mask)

        old_mask_src = obj.mask_src
        old_mask_color = obj.mask_color
        old_mask_opacity = obj.mask_opacity
        old_mask_enabled = obj.mask_enabled

        self._undo_stack.beginMacro(_MACRO_TEXT)
        try:
            self._undo_stack.push(
                SetPropertyCommand(document, obj, "mask_src", mask_src, old_mask_src)
            )
            self._undo_stack.push(
                SetPropertyCommand(document, obj, "mask_color", color, old_mask_color)
            )
            self._undo_stack.push(
                SetPropertyCommand(document, obj, "mask_opacity", opacity, old_mask_opacity)
            )
            self._undo_stack.push(
                SetPropertyCommand(document, obj, "mask_enabled", True, old_mask_enabled)
            )
        finally:
            self._undo_stack.endMacro()
