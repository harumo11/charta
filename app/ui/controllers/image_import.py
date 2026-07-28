"""ImageImportController: 画像取り込み（メニュー経路・D&D、M2契約 §4）。

MainWindow の `import_image_action`/`_import_dropped_images`/`import_image_file`/
`_ensure_base_dir_for_import` および補助関数 `_clamp_span`/`_object_rect` を移設した
もの（Phase 4契約 Stage 4）。ロジックは `app/ui/main_window.py`（移設前）と同一で、
`self.xxx` の参照付け替えのみ行った。ウィンドウフィット
（`_resize_window_to_fit` + `fit_to_rect`）は MainWindow 側に残るため、取り込み
成功時に呼ぶコールバック `on_imported: Callable[[QRectF], None]` をコンストラクタ
で受け取る（呼び出しタイミング・引数は移設前と同一）。
"""

from __future__ import annotations

from collections.abc import Callable
from typing import TYPE_CHECKING

from PIL import Image
from PySide6.QtCore import QPointF, QRectF
from PySide6.QtWidgets import QFileDialog, QMessageBox

from app.commands.commands import AddObjectCommand
from app.graphics.image_pipeline import compute_default_size
from app.model.objects import BaseObject, ImageObject
from app.model.serialize import import_image

if TYPE_CHECKING:
    from PySide6.QtGui import QUndoStack

    from app.scene.canvas_scene import CanvasScene
    from app.ui.main_window import MainWindow

_IMAGE_FILE_FILTER = "画像ファイル (*.png *.jpg *.jpeg *.bmp *.webp);;すべてのファイル (*)"


def _clamp_span(pos: float, size: float, bound: float) -> float:
    """区間 [0, bound] に長さ size が収まる開始座標を返す（収まらなければ中央寄せ）。"""
    if size >= bound:
        return (bound - size) / 2.0
    return min(max(pos, 0.0), bound - size)


def _object_rect(obj: BaseObject) -> QRectF:
    return QRectF(obj.x, obj.y, obj.width, obj.height)


class ImageImportController:
    """画像ファイルの取り込み（メニュー経路・D&D）を担当する。

    `window`（ダイアログの親・`_temp_autosave_path()` の呼び出し先）、`scene`
    （`document`/選択状態の参照元）、`undo_stack` を保持する。取り込み成功時は
    `on_imported(rect)` を呼び、ウィンドウフィット（MainWindow 側の
    `_resize_window_to_fit` + `view.fit_to_rect`）を移設前と同じタイミング・
    引数で発火させる。
    """

    def __init__(
        self,
        window: MainWindow,
        scene: CanvasScene,
        undo_stack: QUndoStack,
        on_imported: Callable[[QRectF], None],
    ) -> None:
        self._window = window
        self._scene = scene
        self._undo_stack = undo_stack
        self._on_imported = on_imported

    # ------------------------------------------------------------------
    # File: 画像を取り込み（M2 契約 §4）
    # ------------------------------------------------------------------

    def _ensure_base_dir_for_import(self) -> bool:
        """`assets/` の基点を保証する。取り込み続行可なら True。

        未保存プロジェクトでは保存を要求せず、一時ディレクトリ（自動保存と同じ
        `_temp_autosave_path()`）を黙って基点にする。取り込んだ画像は
        「名前を付けて保存」時に assets/ ごと実プロジェクトへ移行される
        （`save_project_as` の `migrate_assets`）。
        """
        document = self._scene.document
        if document.base_dir is None:
            document.base_dir = self._window._temp_autosave_path()
        return True

    def import_image_file(
        self,
        path: str,
        center: tuple[float, float] | None = None,
        errors: list[str] | None = None,
        select: bool = True,
    ) -> ImageObject | None:
        """1 ファイルを `assets/` に複製し `ImageObject` を追加する(成功で当該オブジェクト)。

        `center` はアートボード座標での配置中心（None ならアートボード中央）。
        アートボード外はスクロール到達不能になるため、画像が収まる位置へクランプする。
        `errors` を渡すと失敗をダイアログではなくそこへ蓄積する（複数取り込みの集約用）。

        ダイアログを開かないヘッドレス安全な取り込み経路。外部（エージェント制御
        サーバ `app/agent/`）はこれを `errors=[]` 付きで呼ぶ。事前に
        `_ensure_base_dir_for_import()` を済ませておくこと。
        """
        document = self._scene.document

        def _fail(title: str, exc: Exception) -> None:
            if errors is not None:
                errors.append(f"{path}\n{exc}")
            else:
                QMessageBox.critical(self._window, title, f"{path}\n{exc}")

        # 複製より先に読めることを検証する（assets/ に参照ゼロの孤児複製を残さない）。
        try:
            with Image.open(path) as img:
                src_w, src_h = img.size
        except Exception as exc:  # noqa: BLE001 - ユーザーへのエラー表示のため捕捉
            _fail("画像の読み込みに失敗しました", exc)
            return None

        try:
            rel = import_image(document, path)
        except Exception as exc:  # noqa: BLE001 - ユーザーへのエラー表示のため捕捉
            _fail("取り込みに失敗しました", exc)
            return None

        artboard = document.artboard
        width, height = compute_default_size(
            src_w, src_h, float(artboard.width_px), float(artboard.height_px)
        )
        if center is None:
            center = (artboard.width_px / 2.0, artboard.height_px / 2.0)
        obj = ImageObject(
            id=document.new_id(),
            src=rel,
            x=_clamp_span(center[0] - width / 2.0, width, float(artboard.width_px)),
            y=_clamp_span(center[1] - height / 2.0, height, float(artboard.height_px)),
            width=width,
            height=height,
        )

        self._undo_stack.push(AddObjectCommand(document, obj))

        if select:
            new_item = self._scene.item_for(obj)
            if new_item is not None:
                self._scene.clearSelection()
                new_item.setSelected(True)
        return obj

    def import_image_action(self) -> None:
        """画像ファイルを選択し `assets/` に複製、`ImageObject` を追加する。"""
        if not self._ensure_base_dir_for_import():
            return
        path, _filter = QFileDialog.getOpenFileName(
            self._window, "画像を取り込み", "", _IMAGE_FILE_FILTER
        )
        if not path:
            return
        obj = self.import_image_file(path)
        if obj is not None:
            # 取り込んだ画像がそのまま作業対象になるよう、ウィンドウを画像サイズへ
            # 合わせてからビューを画像へフィットさせる。
            rect = _object_rect(obj)
            self._on_imported(rect)

    def _import_dropped_images(self, paths: list[str], scene_pos: QPointF) -> None:
        """キャンバスへの D&D で画像を取り込む（ドロップ位置を配置中心にする）。"""
        if not self._ensure_base_dir_for_import():
            return

        errors: list[str] = []
        # 先に読めるファイルだけに絞る。壊れたファイルしか無いのに undo マクロを
        # 開くと空マクロ（無操作の undo エントリ）が履歴に残るため。
        valid: list[str] = []
        for path in paths:
            try:
                with Image.open(path):
                    pass
            except Exception as exc:  # noqa: BLE001 - ユーザーへのエラー表示のため捕捉
                errors.append(f"{path}\n{exc}")
            else:
                valid.append(path)

        # 1 回のドロップ操作は 1 undo に集約する（§9.6 の方針）。
        use_macro = len(valid) > 1
        if use_macro:
            self._undo_stack.beginMacro("画像を取り込み")
        imported: list[ImageObject] = []
        try:
            offset = 0.0
            for path in valid:
                # 複数ドロップは重なり切らないよう斜めにずらして配置する。
                obj = self.import_image_file(
                    path,
                    (scene_pos.x() + offset, scene_pos.y() + offset),
                    errors=errors,
                    select=False,
                )
                if obj is not None:
                    imported.append(obj)
                    offset += 24.0
        finally:
            if use_macro:
                self._undo_stack.endMacro()

        if imported:
            # 取り込んだ全画像を選択し、ウィンドウを外接矩形へ合わせてからフィットさせる。
            self._scene.clearSelection()
            rect = _object_rect(imported[0])
            for obj in imported:
                item = self._scene.item_for(obj)
                if item is not None:
                    item.setSelected(True)
                rect = rect.united(_object_rect(obj))
            self._on_imported(rect)

        if errors:
            QMessageBox.critical(self._window, "取り込みに失敗しました", "\n\n".join(errors))
