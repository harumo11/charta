"""Sam3MaskDialog: SAM3 選択的マスキングのプロンプト入力・プレビューダイアログ（契約 D-2）。

テキスト/正負ボックスプロンプトで対象物を指定し、`Sam3Worker`（常駐 QThread）経由で
`app.ai.sam3.Sam3Engine.detect()` を呼び出す。検出候補はプレビュー上にティント表示され、
クリックで採否をトグルできる。OK で確定すると採用候補の論理和マスク（元画像座標・
uint8 [H,W]）と、覆い色/不透明度を `result_payload()` で返す。

推論本体は Qt 非依存の `app.ai.sam3`（`Sam3Engine`/`MaskCandidate`/`combined_mask_u8`）
に委譲し、本ファイルは UI とスレッド配線のみを担う。
"""

from __future__ import annotations

from typing import TYPE_CHECKING

import numpy as np
from PIL import Image
from PySide6.QtCore import QObject, QPointF, QRectF, Qt, QThread, Signal, Slot
from PySide6.QtGui import (
    QColor,
    QImage,
    QMouseEvent,
    QPainter,
    QPen,
    QPixmap,
    QResizeEvent,
    QShowEvent,
)
from PySide6.QtWidgets import (
    QCheckBox,
    QColorDialog,
    QDialog,
    QDialogButtonBox,
    QDoubleSpinBox,
    QGraphicsPixmapItem,
    QGraphicsRectItem,
    QGraphicsScene,
    QGraphicsView,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QPushButton,
    QVBoxLayout,
    QWidget,
)

from app.ai.sam3 import BoxPrompt, MaskCandidate, combined_mask_u8, get_engine
from app.graphics.image_pipeline import load_source_rgba

if TYPE_CHECKING:
    from app.model.document import Document
    from app.model.objects import ImageObject

_ERROR_STYLE = "color:#B00020;"
_POSITIVE_COLOR = QColor("#00C853")
_NEGATIVE_COLOR = QColor("#B00020")
_CANDIDATE_COLOR = QColor("#00B8D4")
_ACCEPTED_ALPHA = 90
_REJECTED_ALPHA = 25
_DEFAULT_MASK_COLOR = "#FFFFFF"
_DRAG_THRESHOLD_PX = 4.0
_MIN_BOX_SIZE_PX = 1.0
_LOADING_STATUS = "SAM3 モデルをロード中…（初回は数 GB のダウンロード）"


def _candidate_tint(mask: np.ndarray, accepted: bool) -> np.ndarray:
    """候補マスクから RGBA ティント配列を作る（ベクトル化・for ループ無し）。"""
    alpha = _ACCEPTED_ALPHA if accepted else _REJECTED_ALPHA
    out = np.zeros((*mask.shape, 4), dtype=np.uint8)
    out[mask, 0] = _CANDIDATE_COLOR.red()
    out[mask, 1] = _CANDIDATE_COLOR.green()
    out[mask, 2] = _CANDIDATE_COLOR.blue()
    out[mask, 3] = alpha
    return np.ascontiguousarray(out)


def _rgba_to_qimage(arr: np.ndarray) -> tuple[QImage, np.ndarray]:
    """RGBA numpy 配列から `QImage` を作る。バッファは呼び出し側で生存させること。

    `image_item.py` の `_set_display_buffer` と同じ方式（`QImage` はバッファへの
    ポインタを保持するのみでコピーしないため、Python 側の参照を保持し続ける必要がある）。
    """
    buffer = np.ascontiguousarray(arr)
    h, w = buffer.shape[0], buffer.shape[1]
    qimage = QImage(buffer.data, w, h, w * 4, QImage.Format.Format_RGBA8888)
    return qimage, buffer


class _PreviewView(QGraphicsView):
    """原画像 + ボックスプロンプト + 検出候補ティントを表示するプレビュー（契約 D-2）。

    左ドラッグ = 正ボックス、右ドラッグ = 負ボックス。ボックス枠のクリックで
    当該ボックスを削除し、それ以外の位置のクリックは直下の検出候補（最前面優先）の
    採否をトグルする。座標系はアイテム座標 = 画像 px（pixmap item を原点に原寸で
    配置し、`fitInView` はビュー変換のみで行うため）。
    """

    candidate_toggled = Signal(int)

    def __init__(self, image_rgba: np.ndarray, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setContextMenuPolicy(Qt.ContextMenuPolicy.NoContextMenu)
        self.setDragMode(QGraphicsView.DragMode.NoDrag)
        self.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        self.setVerticalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        self.setRenderHint(QPainter.RenderHint.Antialiasing)

        self._gscene = QGraphicsScene(self)
        self.setScene(self._gscene)

        h, w = image_rgba.shape[0], image_rgba.shape[1]
        self._image_size = (w, h)
        qimage, self._source_buffer = _rgba_to_qimage(image_rgba)
        self._pixmap_item = QGraphicsPixmapItem(QPixmap.fromImage(qimage))
        self._gscene.addItem(self._pixmap_item)
        self._gscene.setSceneRect(0.0, 0.0, float(w), float(h))

        self._box_items: list[tuple[QGraphicsRectItem, int]] = []
        self._candidates: list[MaskCandidate] = []
        self._candidate_buffers: list[np.ndarray] = []
        self._candidate_items: list[QGraphicsPixmapItem] = []

        self._press_pos: QPointF | None = None
        self._press_label: int | None = None
        self._drag_item: QGraphicsRectItem | None = None

    # ------------------------------------------------------------------
    # フィット
    # ------------------------------------------------------------------

    def showEvent(self, event: QShowEvent) -> None:  # noqa: N802 (Qt override)
        super().showEvent(event)
        self._fit()

    def resizeEvent(self, event: QResizeEvent) -> None:  # noqa: N802 (Qt override)
        super().resizeEvent(event)
        self._fit()

    def _fit(self) -> None:
        self.fitInView(self._pixmap_item, Qt.AspectRatioMode.KeepAspectRatio)

    # ------------------------------------------------------------------
    # ボックスプロンプト
    # ------------------------------------------------------------------

    def boxes(self) -> list[BoxPrompt]:
        """現在のボックスプロンプト（元画像 px の xyxy, label）を返す。"""
        result: list[BoxPrompt] = []
        for rect_item, label in self._box_items:
            r = rect_item.rect()
            result.append(((r.x(), r.y(), r.x() + r.width(), r.y() + r.height()), label))
        return result

    @staticmethod
    def _pen_for(label: int) -> QPen:
        color = _POSITIVE_COLOR if label == 1 else _NEGATIVE_COLOR
        pen = QPen(color)
        pen.setStyle(Qt.PenStyle.DashLine)
        pen.setWidth(2)
        pen.setCosmetic(True)
        return pen

    def mousePressEvent(self, event: QMouseEvent) -> None:  # noqa: N802 (Qt override)
        if event.button() not in (Qt.MouseButton.LeftButton, Qt.MouseButton.RightButton):
            super().mousePressEvent(event)
            return
        self._press_pos = self.mapToScene(event.pos())
        self._press_label = 1 if event.button() == Qt.MouseButton.LeftButton else 0
        self._drag_item = None
        event.accept()

    def mouseMoveEvent(self, event: QMouseEvent) -> None:  # noqa: N802 (Qt override)
        if self._press_pos is None:
            super().mouseMoveEvent(event)
            return
        current = self.mapToScene(event.pos())
        if self._drag_item is None:
            if (current - self._press_pos).manhattanLength() < _DRAG_THRESHOLD_PX:
                event.accept()
                return
            self._drag_item = QGraphicsRectItem()
            self._drag_item.setPen(self._pen_for(self._press_label or 0))
            self._gscene.addItem(self._drag_item)
        self._drag_item.setRect(QRectF(self._press_pos, current).normalized())
        event.accept()

    def mouseReleaseEvent(self, event: QMouseEvent) -> None:  # noqa: N802 (Qt override)
        if self._press_pos is None:
            super().mouseReleaseEvent(event)
            return
        if self._drag_item is not None:
            rect = self._drag_item.rect()
            if rect.width() >= _MIN_BOX_SIZE_PX and rect.height() >= _MIN_BOX_SIZE_PX:
                self._box_items.append((self._drag_item, self._press_label or 0))
            else:
                self._gscene.removeItem(self._drag_item)
        else:
            self._handle_click(self._press_pos)
        self._press_pos = None
        self._press_label = None
        self._drag_item = None
        event.accept()

    def _handle_click(self, scene_pos: QPointF) -> None:
        for rect_item, _label in list(self._box_items):
            if rect_item.contains(rect_item.mapFromScene(scene_pos)):
                self._remove_box(rect_item)
                return
        self._toggle_candidate_at(scene_pos)

    def _remove_box(self, rect_item: QGraphicsRectItem) -> None:
        self._box_items = [
            (item, label) for item, label in self._box_items if item is not rect_item
        ]
        self._gscene.removeItem(rect_item)

    def _toggle_candidate_at(self, scene_pos: QPointF) -> None:
        x, y = int(scene_pos.x()), int(scene_pos.y())
        w, h = self._image_size
        if not (0 <= x < w and 0 <= y < h):
            return
        for index in reversed(range(len(self._candidates))):
            if bool(self._candidates[index].mask[y, x]):
                candidate = self._candidates[index]
                candidate.accepted = not candidate.accepted
                self._refresh_candidate_tint(index)
                self.candidate_toggled.emit(index)
                return

    # ------------------------------------------------------------------
    # 検出候補
    # ------------------------------------------------------------------

    def set_candidates(self, candidates: list[MaskCandidate]) -> None:
        """検出候補を差し替え、ティント表示を再構築する。"""
        for item in self._candidate_items:
            self._gscene.removeItem(item)
        self._candidate_items = []
        self._candidate_buffers = []
        self._candidates = candidates
        for candidate in candidates:
            self._candidate_items.append(self._make_candidate_item(candidate))

    def _make_candidate_item(self, candidate: MaskCandidate) -> QGraphicsPixmapItem:
        qimage, buffer = _rgba_to_qimage(_candidate_tint(candidate.mask, candidate.accepted))
        self._candidate_buffers.append(buffer)
        item = QGraphicsPixmapItem(QPixmap.fromImage(qimage))
        item.setToolTip(f"score={candidate.score:.3f}")
        self._gscene.addItem(item)
        return item

    def _refresh_candidate_tint(self, index: int) -> None:
        candidate = self._candidates[index]
        qimage, buffer = _rgba_to_qimage(_candidate_tint(candidate.mask, candidate.accepted))
        self._candidate_buffers[index] = buffer
        self._candidate_items[index].setPixmap(QPixmap.fromImage(qimage))


class Sam3Worker(QObject):
    """SAM3 推論をワーカースレッド上で実行する（契約 D-2）。"""

    status_changed = Signal(str)
    detected = Signal(object)  # list[MaskCandidate]
    failed = Signal(str)

    @Slot(object, object, object)
    def run_detect(self, pil_image: Image.Image, text: str | None, boxes: list[BoxPrompt]) -> None:
        """`engine.load()`→`set_image()`→`detect()` を実行し結果を emit する。"""
        try:
            engine = get_engine()
            if not engine.loaded:
                self.status_changed.emit(_LOADING_STATUS)
            engine.load()
            engine.set_image(pil_image)
            candidates = engine.detect(text, boxes or None)
        except Exception as exc:  # noqa: BLE001 - ワーカースレッドの例外は signal で伝える
            self.failed.emit(str(exc))
            return
        self.detected.emit(candidates)


class Sam3MaskDialog(QDialog):
    """SAM3 選択的マスキングのプロンプト入力・プレビューダイアログ。"""

    #: ワーカーへの検出要求（メインスレッド→ワーカースレッド、QueuedConnection 自動）。
    detect_requested = Signal(object, object, object)

    def __init__(self, parent: QWidget | None, document: Document, obj: ImageObject) -> None:
        super().__init__(parent)
        self.setWindowTitle("SAM3 マスク")
        self._document = document
        self._obj = obj
        self._remove_requested = False
        self._candidates: list[MaskCandidate] = []

        source_rgba = load_source_rgba(document, obj)
        load_failed = source_rgba is None
        if source_rgba is None:
            source_rgba = np.zeros((1, 1, 4), dtype=np.uint8)
        self._image_size = (int(source_rgba.shape[1]), int(source_rgba.shape[0]))
        self._pil_image = Image.fromarray(source_rgba).convert("RGB")

        # プロンプト行
        self._text_edit = QLineEdit()
        self._text_edit.setPlaceholderText("対象物（英語推奨）: 例 cat")
        self._detect_button = QPushButton("検出")
        self._detect_button.clicked.connect(self._on_detect_clicked)
        self._status_label = QLabel("")

        prompt_row = QHBoxLayout()
        prompt_row.addWidget(self._text_edit)
        prompt_row.addWidget(self._detect_button)

        # プレビュー
        self._preview = _PreviewView(source_rgba, self)
        self._preview.candidate_toggled.connect(lambda _index: self._update_ok_enabled())

        # 覆い行
        self._transparent_check = QCheckBox("透明（切り取り）")
        self._color_button = QPushButton()
        self._color_button.clicked.connect(self._pick_color)
        self._opacity_spin = QDoubleSpinBox()
        self._opacity_spin.setRange(0.0, 1.0)
        self._opacity_spin.setSingleStep(0.05)
        self._opacity_spin.setDecimals(2)
        self._opacity_spin.setValue(float(obj.mask_opacity))

        self._selected_color = obj.mask_color or _DEFAULT_MASK_COLOR
        self._transparent_check.setChecked(obj.mask_color is None)
        self._color_button.setEnabled(obj.mask_color is not None)
        self._apply_button_color(self._selected_color)
        self._transparent_check.toggled.connect(self._on_transparent_toggled)

        overlay_row = QHBoxLayout()
        overlay_row.addWidget(self._transparent_check)
        overlay_row.addWidget(self._color_button)
        overlay_row.addWidget(QLabel("不透明度"))
        overlay_row.addWidget(self._opacity_spin)

        # ボタン行
        button_row = QHBoxLayout()
        if obj.mask_src:
            self._remove_button: QPushButton | None = QPushButton("マスクを解除")
            self._remove_button.clicked.connect(self._on_remove_clicked)
            button_row.addWidget(self._remove_button)
        else:
            self._remove_button = None
        button_row.addStretch(1)
        self._buttons = QDialogButtonBox(
            QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel
        )
        self._buttons.accepted.connect(self.accept)
        self._buttons.rejected.connect(self.reject)
        button_row.addWidget(self._buttons)

        layout = QVBoxLayout(self)
        layout.addLayout(prompt_row)
        layout.addWidget(self._preview, 1)
        layout.addWidget(self._status_label)
        layout.addLayout(overlay_row)
        layout.addLayout(button_row)
        self.resize(720, 640)

        self._update_ok_enabled()
        if load_failed:
            self._detect_button.setEnabled(False)
            self._set_status("元画像の読み込みに失敗しました", error=True)

        # スレッド配線（契約 D-2）。
        self._thread = QThread(self)
        self._worker = Sam3Worker()
        self._worker.moveToThread(self._thread)
        self.detect_requested.connect(self._worker.run_detect)
        self._worker.status_changed.connect(self._on_status)
        self._worker.detected.connect(self._on_detected)
        self._worker.failed.connect(self._on_failed)
        self._thread.start()

    # ------------------------------------------------------------------
    # 検出
    # ------------------------------------------------------------------

    def _on_detect_clicked(self) -> None:
        text = self._text_edit.text().strip() or None
        boxes = self._preview.boxes()
        if text is None and not boxes:
            self._set_status("テキストか矩形で対象を指定してください", error=True)
            return
        self._detect_button.setEnabled(False)
        self._set_status("検出中…")
        self.detect_requested.emit(self._pil_image, text, boxes)

    def _on_status(self, message: str) -> None:
        self._set_status(message)

    def _on_detected(self, candidates: list[MaskCandidate]) -> None:
        self._detect_button.setEnabled(True)
        if candidates:
            self._set_status(f"{len(candidates)} 件検出")
        else:
            self._set_status("候補が見つかりませんでした")
        self._apply_candidates(candidates)

    def _on_failed(self, message: str) -> None:
        self._detect_button.setEnabled(True)
        self._set_status(message, error=True)

    def _apply_candidates(self, candidates: list[MaskCandidate]) -> None:
        self._candidates = candidates
        self._preview.set_candidates(candidates)
        self._update_ok_enabled()

    def set_candidates_for_test(self, candidates: list[MaskCandidate]) -> None:
        """ヘッドレステスト用: ワーカーを介さず候補を注入する（`_on_detected` と同経路）。"""
        self._apply_candidates(candidates)

    def _set_status(self, message: str, error: bool = False) -> None:
        self._status_label.setText(message)
        self._status_label.setStyleSheet(_ERROR_STYLE if error else "")

    # ------------------------------------------------------------------
    # 覆い行
    # ------------------------------------------------------------------

    def _on_transparent_toggled(self, checked: bool) -> None:
        self._color_button.setEnabled(not checked)

    def _pick_color(self) -> None:
        color = QColorDialog.getColor(QColor(self._selected_color), self, "マスク覆い色")
        if not color.isValid():
            return
        self._selected_color = color.name()
        self._apply_button_color(self._selected_color)

    def _apply_button_color(self, color: str) -> None:
        self._color_button.setText(color)
        self._color_button.setStyleSheet(f"background-color: {color};")

    # ------------------------------------------------------------------
    # OK 有効化・結果
    # ------------------------------------------------------------------

    def _update_ok_enabled(self) -> None:
        has_accepted = any(candidate.accepted for candidate in self._candidates)
        ok_button = self._buttons.button(QDialogButtonBox.StandardButton.Ok)
        if ok_button is not None:
            ok_button.setEnabled(has_accepted)

    def _on_remove_clicked(self) -> None:
        self._remove_requested = True
        self.accept()

    def result_payload(self) -> tuple[np.ndarray | None, str | None, float]:
        """(採用候補の論理和マスク or None, 覆い色, 不透明度) を返す。accept 後に呼ぶ。"""
        opacity = float(self._opacity_spin.value())
        color = None if self._transparent_check.isChecked() else self._selected_color
        if self._remove_requested:
            return None, color, opacity
        mask = combined_mask_u8(self._candidates, self._image_size)
        return mask, color, opacity

    # ------------------------------------------------------------------
    # スレッド終了処理
    # ------------------------------------------------------------------

    def done(self, result: int) -> None:  # noqa: N802 (Qt override)
        """exec 終了後にスレッドを確実に止める（残留・GC クラッシュ防止、契約 D-2）。"""
        self._thread.quit()
        self._thread.wait()
        super().done(result)
