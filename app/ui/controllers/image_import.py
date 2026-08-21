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

from app.commands.commands import AddObjectCommand, SetArtboardCommand
from app.graphics.image_pipeline import compute_default_size
from app.model.document import ARTBOARD_PX_MAX, artboard_with_pixel_size
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


def _autofit_pixel_size(width_px: float, height_px: float) -> tuple[float, float]:
    """自動フィット用に画像 px をアートボード上限内へ収める（縦横比を維持）。

    `artboard_with_pixel_size` 内部の `clamp_artboard_px` は縦横を独立にクランプ
    するため、片方だけ上限を超える巨大画像（例: 40000x20000）を渡すと比が崩れる
    （20000x20000 になってしまう）。自動フィットは「取り込んだ画像に見た目を
    合わせる」操作である以上ここだけは縦横比維持を優先し、渡す前に
    `scale = min(MAX/w, MAX/h)` で縮小しておく。
    """
    if width_px <= ARTBOARD_PX_MAX and height_px <= ARTBOARD_PX_MAX:
        return width_px, height_px
    scale = min(ARTBOARD_PX_MAX / width_px, ARTBOARD_PX_MAX / height_px)
    return width_px * scale, height_px * scale


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

    def _should_autofit_artboard(self) -> bool:
        """「まだ何も始まっていない空ドキュメント」だけで自動調整する。

          - len(document.objects) == 0      … 図がまだ無い
          - document.next_id == 1           … id を 1 つも払い出していない（消した跡も無い）
          - undo_stack.index() == 0         … アートボード設定・スタイル登録すら未実施
          - project_io.project_dir is None  … 保存済みプロジェクトを開いたのではない

        `document.base_dir` は判定に使えない（未保存でも `_ensure_base_dir_for_import` が
        一時ディレクトリを黙って入れる）。dirty フラグは存在しないため
        「まだ何も操作していない」の代理として undo スタックの状態を使う。

        `count()` ではなく `index()` を見るのは、`QUndoStack.beginMacro()` が
        まだ 1 つも子コマンドを push していない時点で `count()` を直ちに 1 個分
        予約してしまうため（前提3・実測確認済み）。`_import_dropped_images` は
        複数ファイル同時ドロップを 1 undo に集約するため呼び出し前に外側マクロを
        開いており、`count()==0` のままだと 1 枚目の自動フィットが常に不発になる。
        `index()` はマクロが確定（`endMacro()` 完了）するまで動かないため、
        「外側マクロを開いただけでまだ何も確定していない」を正しく 0 のまま読める。
        一方で実際に確定済みのコマンドが 1 つでもあれば `index()` は必ず動くため、
        アートボード設定・スタイル登録済みのドキュメントは変わらず対象外になる。
        保存済みの空プロジェクトを除外するのは、ユーザーが意図して設定して保存した
        アートボードを黙って書き換えないため。

        `self._window._project_dir`（`ProjectIOController.project_dir` への
        委譲プロパティ）を読むのは、本コントローラが `project_io` 自体を
        保持していないため（`ExportController` も同型のニーズを
        `lambda: self._window._project_dir` で解決しており、同じ流儀に揃えた
        レビュー所見）。
        """
        document = self._scene.document
        return (
            len(document.objects) == 0
            and document.next_id == 1
            and self._undo_stack.index() == 0
            and self._window._project_dir is None
        )

    def import_image_file(
        self,
        path: str,
        center: tuple[float, float] | None = None,
        errors: list[str] | None = None,
        select: bool = True,
        *,
        autofit_artboard: bool = False,
    ) -> ImageObject | None:
        """1 ファイルを `assets/` に複製し `ImageObject` を追加する(成功で当該オブジェクト)。

        `center` はアートボード座標での配置中心（None ならアートボード中央）。
        アートボード外はスクロール到達不能になるため、画像が収まる位置へクランプする。
        `errors` を渡すと失敗をダイアログではなくそこへ蓄積する（複数取り込みの集約用）。

        `autofit_artboard` は「まだ何も始まっていない空ドキュメント」への取り込み時に
        アートボードを画像の px 寸法へ合わせるかどうか（項目9）。**既定 False が必須**:
        この関数は `app/agent/api.py` の `place_image` からも呼ばれており、エージェントが
        黙ってアートボードを書き換えると明示 `set_artboard` を持つクライアントの期待と
        衝突し MCP パリティも壊れる。`True` を渡すのは人間経路の `import_image_action` /
        `_import_dropped_images` の 2 箇所だけでよい（実際に自動調整するかは
        `_should_autofit_artboard()` の 4 条件で最終判定する）。

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

        # マクロを開くのは import_image() 成功の後（空マクロ＝無操作の undo エントリを
        # 残さないため）。判定は import_image 成功後の document 状態で行う
        # （id 払い出し前なのでこの時点ではまだ 4 条件は変化しない）。
        autofit = autofit_artboard and self._should_autofit_artboard()
        if autofit:
            self._undo_stack.beginMacro("画像を取り込み（アートボードを合わせる）")
        try:
            if autofit:
                fit_w, fit_h = _autofit_pixel_size(float(src_w), float(src_h))
                old_artboard = document.artboard
                self._undo_stack.push(
                    SetArtboardCommand(
                        document,
                        artboard_with_pixel_size(old_artboard, fit_w, fit_h),
                        old_artboard,
                        text="アートボードを画像に合わせる",
                    )
                )

            # ★ autofit で document.artboard が差し替わっている可能性があるため
            # 必ず読み直す（読み直さないと縮小前の旧アートボード基準で配置してしまう）。
            artboard = document.artboard
            # 前提5: compute_default_size はアートボードに収まるなら原寸を返す。
            # autofit 済みならアートボードは画像寸法ちょうどに揃っているため、ここで
            # 原寸がそのまま返る（=収まる）。そのためバイパス分岐は不要。
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
        finally:
            if autofit:
                self._undo_stack.endMacro()

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
        obj = self.import_image_file(path, autofit_artboard=True)
        if obj is not None:
            # 取り込んだ画像がそのまま作業対象になるよう、ウィンドウを画像サイズへ
            # 合わせてからビューを画像へフィットさせる。
            # ★ push（アートボード変更含む）より後でなければならない: _on_imported
            # （_resize_window_to_fit + fit_to_rect）は新しい sceneRect を見るため、
            # 逆にすると autofit 前の旧アートボード基準でフィットしてしまう。
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
                    # autofit_artboard=True を渡すだけでよい。複数同時ドロップの
                    # 分岐は書かない: 2 枚目以降は最初の画像追加で
                    # len(objects) == 1 になり `_should_autofit_artboard()` が
                    # 自然に False を返す。
                    autofit_artboard=True,
                )
                if obj is not None:
                    imported.append(obj)
                    offset += 24.0
        finally:
            if use_macro:
                self._undo_stack.endMacro()

        if imported:
            # 取り込んだ全画像を選択し、ウィンドウを外接矩形へ合わせてからフィットさせる。
            # ★ ループ（push を含む）の外＝すべての push が終わった後で呼ぶこと。
            # autofit でアートボードが変わった場合、_on_imported が新しい sceneRect を
            # 見る必要があるため、push より前に動かしてはならない。
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
