"""Sam3MaskController: SAM3 選択的マスキングのオンキャンバス編集モード配線（契約 sam3_v2 §H）。

「SAM3 マスク…」メニュー項目の有効/無効判定と、キャンバス上での直接編集モード
（`MaskEditSession`）の開始・切替を担う。SAM3 推論そのものは `app.ai.sam3`
（Qt 非依存層）の責務で、推論スレッド配線は本ファイルの `Sam3Worker` が担う。

確定処理（`assets/` へのマスク保存 + `SetPropertyCommand` の undo マクロ化）は
ダイアログ時代から変わらず `Sam3MaskController.commit_mask()` が一手に引き受け、
`MaskEditSession` はそれを呼び出すだけのヘッドレステスト可能な構成を保つ。
"""

from __future__ import annotations

import queue
import threading
from typing import TYPE_CHECKING

import numpy as np
import shiboken6
from PIL import Image
from PySide6.QtCore import QObject, Signal, Slot
from PySide6.QtGui import QAction, QUndoStack
from PySide6.QtWidgets import QMenu, QMessageBox

from app.ai import sam3
from app.ai.sam3 import BoxPrompt, MaskCandidate, combined_mask_u8, get_engine
from app.commands.commands import SetPropertyCommand
from app.graphics.image_pipeline import load_source_rgba
from app.model.objects import ImageObject
from app.model.serialize import save_mask_png
from app.scene.items.mask_edit_overlay import MaskEditOverlay

if TYPE_CHECKING:
    from app.scene.canvas_scene import CanvasScene
    from app.scene.items.image_item import ImageItem
    from app.ui.main_window import MainWindow
    from app.ui.mask_edit_panel import MaskEditPanel

_MENU_LABEL = "SAM3 マスク…"
_DISABLED_TOOLTIP = "uv sync --group sam で torch/transformers を導入すると使えます"
_MACRO_TEXT = "SAM3 マスク"
_LOADING_STATUS = "SAM3 モデルをロード中…（初回は数 GB のダウンロード）"
_NO_PROMPT_STATUS = "テキストか矩形で対象を指定してください"


class Sam3Worker(QObject):
    """SAM3 推論を Python 標準スレッド + キューで実行する。

    要求・結果に generation 番号を付け、セッション側は最新 generation 以外の結果を
    破棄する（テキスト/ボックスを素早く変更した際に古い検出結果が紛れ込むのを防ぐ）。

    **QThread を使わないこと**: `Sam3Model.from_pretrained()` を QThread のスロット内で
    実行すると、そのスレッドがイベントループへ戻った後の次のスロット呼び出しで
    ヒープ破壊による segfault が起きる（transformers 5.14 + torch 2.11 + PySide6 6.11 で
    2026-07-27 に最小再現で確認。python threading.Thread では同一手順でも安定）。
    結果シグナルは python スレッドから emit し、メインスレッドの受信側へは Qt が
    自動で queued 配送する。
    """

    status_changed = Signal(str)
    detected = Signal(int, object)  # (generation, list[MaskCandidate])
    failed = Signal(int, str)

    def __init__(self) -> None:
        super().__init__()
        self._queue: queue.Queue[tuple | None] = queue.Queue()
        self._thread = threading.Thread(target=self._loop, name="sam3-worker", daemon=True)
        self._thread.start()

    @Slot(int, object, object, object)
    def submit(
        self, generation: int, pil_image: Image.Image, text: str | None, boxes: list[BoxPrompt]
    ) -> None:
        """検出要求をキューへ積む（メインスレッドから呼ぶ。ブロックしない）。"""
        self._queue.put((generation, pil_image, text, boxes))

    def stop(self) -> None:
        """ワーカースレッドを停止する。推論実行中は完了まで（最大 30 秒）待つ。

        デーモンスレッドのため、タイムアウトしてもアプリ終了は妨げない。停止後に
        届き得る古いシグナルは受信側（セッション）の `_closed`/generation ガードが破棄する。
        """
        self._queue.put(None)
        self._thread.join(timeout=30.0)

    def _loop(self) -> None:
        while True:
            request = self._queue.get()
            # 後続の要求が既に積まれていれば古い要求は処理せず飛ばす
            # （どうせ generation ガードで捨てられる推論に GPU 時間を使わない）。
            while True:
                try:
                    request = self._queue.get_nowait()
                except queue.Empty:
                    break
            if request is None:
                return
            self.run_detect(*request)

    def run_detect(
        self, generation: int, pil_image: Image.Image, text: str | None, boxes: list[BoxPrompt]
    ) -> None:
        """`engine.load()`→`set_image()`→`detect()` を実行し結果を emit する（スレッド上）。"""
        try:
            engine = get_engine()
            if not engine.loaded:
                self.status_changed.emit(_LOADING_STATUS)
            engine.load()
            engine.set_image(pil_image)
            candidates = engine.detect(text, boxes or None)
        except Exception as exc:  # noqa: BLE001 - ワーカースレッドの例外は signal で伝える
            self.failed.emit(generation, str(exc))
            return
        self.detected.emit(generation, candidates)


class MaskEditSession(QObject):
    """1 枚の画像に対するオンキャンバスの SAM3 マスク編集セッション（契約 sam3_v2 モジュール H）。

    生成 = モード開始（`ImageItem.begin_mask_edit()` 呼び出し・オーバーレイ/ワーカー
    スレッド配線・パネル表示・`scene.set_active_mask_session(self)` 登録）。
    `commit()`/`cancel()`/`remove_mask()` のいずれかで終了し、`_teardown()` は
    何度呼んでも安全（冪等）かつ item が既に破棄済みでも例外を出さない（防御的）。
    """

    detect_requested = Signal(int, object, object, object)

    def __init__(
        self,
        controller: Sam3MaskController,
        scene: CanvasScene,
        item: ImageItem,
        panel: MaskEditPanel,
    ) -> None:
        super().__init__()
        self._controller = controller
        self._scene = scene
        self._panel = panel
        self._closed = False
        self._generation = 0
        self._candidates: list[MaskCandidate] = []
        self._last_prompt = ""

        if not item.begin_mask_edit():
            raise RuntimeError("マスク編集モードを開始できませんでした")

        self.image_item: ImageItem = item
        self.obj: ImageObject = item.obj

        document = scene.document
        source_rgba = load_source_rgba(document, self.obj)
        if source_rgba is None:
            item.end_mask_edit()
            raise RuntimeError("元画像の読み込みに失敗しました")
        self._image_size = (int(source_rgba.shape[1]), int(source_rgba.shape[0]))
        self._pil_image = Image.fromarray(source_rgba).convert("RGB")

        self._overlay = MaskEditOverlay(item, self._on_boxes_changed, self._on_candidate_toggled)
        item._mask_geometry_listener = self._overlay.sync_size

        self._worker = Sam3Worker()
        self.detect_requested.connect(self._worker.submit)
        self._worker.status_changed.connect(self._panel.set_status)
        self._worker.detected.connect(self._on_detected)
        self._worker.failed.connect(self._on_failed)

        self._panel.text_committed.connect(self._on_text_committed)
        self._panel.commit_clicked.connect(self.commit)
        self._panel.cancel_clicked.connect(self.cancel)
        self._panel.remove_clicked.connect(self.remove_mask)
        self._panel.open_session(
            prompt=self.obj.mask_prompt,
            color=self.obj.mask_color,
            opacity=float(self.obj.mask_opacity),
            can_remove=bool(self.obj.mask_src),
        )

        scene.set_active_mask_session(self)

        # パネル初期値（prompt_text() と同じ strip 済み表現）を「直近確定テキスト」として
        # 記録しておく。こうしないと無変化の editingFinished（フォーカスアウトのみ等）でも
        # _on_text_committed が毎回 _request_detect() を呼んでしまう。
        self._last_prompt = (self.obj.mask_prompt or "").strip()

        if self.obj.mask_prompt:
            # 再編集 UX: 既存プロンプトがあれば開始直後に検出をかけておく。
            self._request_detect()

    # ------------------------------------------------------------------
    # 終了
    # ------------------------------------------------------------------
    @property
    def closed(self) -> bool:
        """終了済み（commit/cancel/remove_mask 後）なら True。"""
        return self._closed

    def commit(self) -> None:
        """採用候補があればマスクを確定して終了する。無ければモデル変更なしで終了する。冪等。"""
        if self._closed:
            return
        mask = combined_mask_u8(self._candidates, self._image_size)
        if mask is not None:
            self._controller.commit_mask(
                self.obj,
                mask,
                self._panel.overlay_color(),
                self._panel.overlay_opacity(),
                prompt=self._panel.prompt_text(),
            )
        self._teardown()

    def cancel(self) -> None:
        """モデル変更なしで終了する（冪等・防御的。例外を外へ出さない）。"""
        try:
            self._teardown()
        except Exception:  # noqa: BLE001 - キャンセル経路は例外を伝播させない
            pass

    def remove_mask(self) -> None:
        """既存マスクを解除して終了する（パネルの「マスクを解除」ボタン用）。冪等。"""
        if self._closed:
            return
        self._controller.commit_mask(
            self.obj, None, self._panel.overlay_color(), self._panel.overlay_opacity()
        )
        self._teardown()

    def _teardown(self) -> None:
        """後始末を行う（冪等）。item が破棄済みでも panel/scene の後始末は完遂する。"""
        if self._closed:
            return
        self._closed = True

        self._panel.text_committed.disconnect(self._on_text_committed)
        self._panel.commit_clicked.disconnect(self.commit)
        self._panel.cancel_clicked.disconnect(self.cancel)
        self._panel.remove_clicked.disconnect(self.remove_mask)

        self._worker.stop()

        item = self.image_item
        if shiboken6.isValid(item):
            self._overlay.clear_all()
            item_scene = item.scene()
            if item_scene is not None:
                item_scene.removeItem(self._overlay)
            item._mask_geometry_listener = None
            item.end_mask_edit()

        self._panel.close_session()
        self._scene.set_active_mask_session(None)

    # ------------------------------------------------------------------
    # オーバーレイ・パネルからのコールバック
    # ------------------------------------------------------------------
    def _on_boxes_changed(self) -> None:
        self._request_detect()

    def _on_candidate_toggled(self, index: int) -> None:
        candidate = self._candidates[index]
        candidate.accepted = not candidate.accepted
        self._overlay.refresh_candidate(index)
        self._panel.set_commit_enabled(any(c.accepted for c in self._candidates))

    def _on_text_committed(self) -> None:
        """パネルの `editingFinished` を受ける。テキストが実際に変化した場合のみ検出要求する。

        `QLineEdit.editingFinished` はテキスト無変化のフォーカスアウトでも発火するため、
        直近確定テキストと比較しないと同一プロンプトで検出を再要求してしまう。
        """
        text = self._panel.prompt_text()
        if text == self._last_prompt:
            return
        self._last_prompt = text
        self._request_detect()

    # ------------------------------------------------------------------
    # 検出
    # ------------------------------------------------------------------
    def _request_detect(self) -> None:
        text = self._panel.prompt_text() or None
        boxes = self._overlay.boxes()
        if text is None and not boxes:
            self._candidates = []
            self._overlay.set_candidates([])
            self._panel.set_commit_enabled(False)
            self._panel.set_status(_NO_PROMPT_STATUS)
            return
        self._generation += 1
        self._panel.set_status("検出中…")
        self.detect_requested.emit(self._generation, self._pil_image, text, boxes)

    def _on_detected(self, generation: int, candidates: list[MaskCandidate]) -> None:
        if self._closed or generation != self._generation:
            return
        self._candidates = candidates
        self._overlay.set_candidates(candidates)
        if candidates:
            self._panel.set_status(f"{len(candidates)} 件検出")
        else:
            self._panel.set_status("候補が見つかりませんでした")
        self._panel.set_commit_enabled(bool(candidates))

    def _on_failed(self, generation: int, message: str) -> None:
        if self._closed or generation != self._generation:
            return
        self._panel.set_status(message, error=True)


class Sam3MaskController:
    """SAM3 マスク編集モードの開始とマスク確定（undo マクロ）を担当する。"""

    def __init__(
        self,
        window: MainWindow,
        scene: CanvasScene,
        undo_stack: QUndoStack,
        panel: MaskEditPanel,
    ) -> None:
        self._window = window
        self._scene = scene
        self._undo_stack = undo_stack
        self._panel = panel
        self._session: MaskEditSession | None = None

    def make_menu_action(self, menu: QMenu) -> QAction:
        """「SAM3 マスク…」QAction を menu に追加して返す。

        `app.ai.sam3.is_available()` が False（torch/transformers 未導入）の場合は
        無効化しツールチップで導入方法を案内する。
        """
        action = menu.addAction(_MENU_LABEL, self.start_mask_edit_action)
        if not sam3.is_available():
            action.setEnabled(False)
            action.setToolTip(_DISABLED_TOOLTIP)
        return action

    def start_mask_edit_action(self) -> None:
        """単一の ImageObject 選択を確認し、オンキャンバスのマスク編集モードを開始する。

        既にセッションが開いている場合、同一オブジェクト対象なら no-op、別オブジェクト
        対象なら旧セッションを確定してから新セッションを開始する（crop モードの
        「別画像の crop 開始時に前の crop を確定する」方針と同じ）。
        """
        selected = self._scene.selected_objects()
        if len(selected) != 1 or not isinstance(selected[0], ImageObject):
            QMessageBox.information(self._window, "SAM3 マスク", "画像を 1 つ選択してください")
            return
        obj = selected[0]

        # セッションはキャンバス側の経路（Enter/Esc/外側クリック/ツール切替）でも終了する
        # ため、参照が残っていても closed なら「開いている」とは扱わない（そうしないと
        # 一度確定した画像でメニューから再編集できなくなる）。
        if self._session is not None and not self._session.closed:
            if self._session.obj is obj:
                return
            self._session.commit()
        self._session = None

        item = self._scene.item_for(obj)
        if item is None:
            QMessageBox.information(self._window, "SAM3 マスク", "画像を 1 つ選択してください")
            return

        try:
            self._session = MaskEditSession(self, self._scene, item, self._panel)
        except RuntimeError as exc:
            QMessageBox.information(self._window, "SAM3 マスク", str(exc))
            return

    def commit_mask(
        self,
        obj: ImageObject,
        mask: np.ndarray | None,
        color: str | None,
        opacity: float,
        prompt: str = "",
    ) -> None:
        """マスクを確定する（セッション非依存の公開 API・ヘッドレステスト可能）。

        `mask` が None ならマスク解除（`mask_src` を None にする単発の
        `SetPropertyCommand` のみ。他フィールドは保持する）。マスクありの場合は
        `document.base_dir` が未設定なら `ImageImportController` と同じ方針で
        一時ディレクトリを割り当ててから `save_mask_png()` で保存し、
        `mask_src`→`mask_color`→`mask_opacity`→`mask_enabled(True)`→`mask_prompt`
        の順に `SetPropertyCommand` を 1 つの undo マクロへまとめて push する。
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
        old_mask_prompt = obj.mask_prompt

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
            self._undo_stack.push(
                SetPropertyCommand(document, obj, "mask_prompt", prompt, old_mask_prompt)
            )
        finally:
            self._undo_stack.endMacro()
