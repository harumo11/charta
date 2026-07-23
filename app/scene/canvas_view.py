"""CanvasView: アートボードを表示する QGraphicsView（ビュー層、契約 §7.2）。

ズーム/パン/ラバーバンド選択を担い、マウスイベントを `tool_manager` へ委譲する。
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from PySide6.QtCore import QEvent, QMimeData, QPointF, QRectF, Qt, Signal
from PySide6.QtGui import (
    QDragEnterEvent,
    QDragMoveEvent,
    QDropEvent,
    QKeyEvent,
    QMouseEvent,
    QPainter,
    QTransform,
    QWheelEvent,
)
from PySide6.QtWidgets import QGraphicsView

from app.scene.canvas_scene import CanvasScene

if TYPE_CHECKING:
    from app.tools.tool_manager import ToolManager


class CanvasView(QGraphicsView):
    """アートボードを表示・操作する QGraphicsView。"""

    MIN_ZOOM = 0.1
    MAX_ZOOM = 20.0

    # 画像 D&D で受け付ける拡張子（import_image_action のファイルフィルタと揃える）
    IMAGE_DROP_EXTENSIONS = (".png", ".jpg", ".jpeg", ".bmp", ".webp")

    #: 画像ファイルがドロップされた（ローカルパスのリスト, ドロップ位置の scene 座標）
    images_dropped = Signal(list, QPointF)

    def __init__(self, scene: CanvasScene) -> None:
        super().__init__(scene)
        self.setAcceptDrops(True)
        self.tool_manager: ToolManager | None = None
        self._zoom: float = 1.0
        self._space_panning = False
        self._middle_panning = False
        self._pre_pan_drag_mode = QGraphicsView.DragMode.RubberBandDrag

        self.setRenderHints(
            QPainter.RenderHint.Antialiasing | QPainter.RenderHint.SmoothPixmapTransform
        )
        self.setTransformationAnchor(QGraphicsView.ViewportAnchor.AnchorUnderMouse)
        self.setResizeAnchor(QGraphicsView.ViewportAnchor.AnchorViewCenter)
        self.setDragMode(QGraphicsView.DragMode.RubberBandDrag)

    def set_tool_manager(self, tm: ToolManager) -> None:
        """MainWindow が生成した ToolManager を後から注入する。"""
        self.tool_manager = tm
        tm.tool_changed.connect(self._on_tool_changed)
        self._update_drag_mode()

    def _on_tool_changed(self, _name: str) -> None:
        self._update_drag_mode()

    def _update_drag_mode(self) -> None:
        if self._space_panning or self._middle_panning:
            return
        if self.tool_manager is not None and self.tool_manager.current_tool() != "select":
            self.setDragMode(QGraphicsView.DragMode.NoDrag)
        else:
            self.setDragMode(QGraphicsView.DragMode.RubberBandDrag)

    # -- ズーム ------------------------------------------------------------

    def wheelEvent(self, event: QWheelEvent) -> None:
        """カーソル中心のズーム（0.1x–20x）。"""
        delta = event.angleDelta().y()
        if delta == 0:
            event.accept()
            return
        factor = 1.25 if delta > 0 else 0.8
        self._apply_zoom_factor(factor)
        event.accept()

    def _apply_zoom_factor(self, factor: float) -> None:
        new_zoom = max(self.MIN_ZOOM, min(self.MAX_ZOOM, self._zoom * factor))
        actual_factor = new_zoom / self._zoom
        if actual_factor == 1.0:
            return
        self._zoom = new_zoom
        self.scale(actual_factor, actual_factor)
        self._refresh_selected_handles()

    def _refresh_selected_handles(self) -> None:
        """ズーム後、選択中アイテムのハンドル（回転ハンドル等）を再配置する。

        回転ハンドルのオフセットは view のスケールに応じて scene 単位へ換算されるため
        (`handles.py` の `_rotate_offset`)、幾何が変わらなくてもズーム直後に
        再計算しないと画面上の隙間が倍率に比例してドリフトする。
        """
        scene = self.scene()
        if scene is None:
            return
        for item in scene.selectedItems():
            refresh = getattr(item, "refresh_handles", None)
            if callable(refresh):
                refresh()

    def fit_to_artboard(self) -> None:
        """アートボード全体が収まるようにフィットさせる。

        「全体表示」は常に全体が見えることが目的のため、MIN_ZOOM の下限クランプは
        掛けない（巨大アートボードで必要倍率が 0.1x を下回っても全体を収める）。
        """
        scene = self.scene()
        if scene is None:
            return
        self.fit_to_rect(scene.sceneRect(), margin_ratio=0.0, clamp_min=False)

    def fit_to_rect(self, rect: QRectF, margin_ratio: float = 0.05, clamp_min: bool = True) -> None:
        """指定 scene 矩形が収まるようにズーム/センタリングする。

        倍率は自前で計算する（`fitInView` は固有の 2px 内部マージンを持ち、
        短辺側で余分に倍率が落ちて「余白なしフィット」ができないため）。
        ズーム上限（MAX_ZOOM）と、`clamp_min=True` なら下限（MIN_ZOOM）にも
        クランプして `_zoom` と同期する。極小矩形でも過剰ズームしない。
        """
        if rect.isEmpty():
            return
        target = rect.adjusted(
            -rect.width() * margin_ratio,
            -rect.height() * margin_ratio,
            rect.width() * margin_ratio,
            rect.height() * margin_ratio,
        )
        viewport = self.viewport()
        zoom = min(viewport.width() / target.width(), viewport.height() / target.height())
        lower = self.MIN_ZOOM if clamp_min else 0.0
        zoom = max(lower, min(self.MAX_ZOOM, zoom))
        self.setTransform(QTransform.fromScale(zoom, zoom))
        self.centerOn(target.center())
        self._zoom = zoom
        self._refresh_selected_handles()

    def zoom_in(self) -> None:
        self._apply_zoom_factor(1.25)

    def zoom_out(self) -> None:
        self._apply_zoom_factor(0.8)

    # -- パン（中ボタン/Space） ---------------------------------------------

    def _begin_temp_pan(self) -> None:
        if self.dragMode() == QGraphicsView.DragMode.ScrollHandDrag:
            return
        self._pre_pan_drag_mode = self.dragMode()
        self.setDragMode(QGraphicsView.DragMode.ScrollHandDrag)

    def _end_temp_pan(self) -> None:
        self.setDragMode(self._pre_pan_drag_mode)

    def keyPressEvent(self, event: QKeyEvent) -> None:
        if event.key() == Qt.Key.Key_Space and not event.isAutoRepeat():
            if not self._space_panning:
                self._space_panning = True
                self._begin_temp_pan()
            event.accept()
            return
        super().keyPressEvent(event)

    def keyReleaseEvent(self, event: QKeyEvent) -> None:
        if event.key() == Qt.Key.Key_Space and not event.isAutoRepeat():
            if self._space_panning:
                self._space_panning = False
                if not self._middle_panning:
                    self._end_temp_pan()
            event.accept()
            return
        super().keyReleaseEvent(event)

    # -- 画像ファイルのドラッグ＆ドロップ -------------------------------------

    def _image_paths_from_mime(self, mime: QMimeData | None) -> list[str]:
        """MIME データからドロップ可能な画像のローカルパスを抽出する。"""
        if mime is None or not mime.hasUrls():
            return []
        paths: list[str] = []
        for url in mime.urls():
            if not url.isLocalFile():
                continue
            path = url.toLocalFile()
            if path.lower().endswith(self.IMAGE_DROP_EXTENSIONS):
                paths.append(path)
        return paths

    @staticmethod
    def _accept_as_copy(event: QDragEnterEvent | QDragMoveEvent | QDropEvent) -> bool:
        """CopyAction で受理する（受理できたら True）。

        取り込みは assets/ への複製でありドラッグ元のデータを消費しない。
        MoveAction を受理するとドラッグ元（ファイルマネージャの Shift ドラッグ等）へ
        「移動成立」が通知され、元ファイルが削除され得る。setDropAction は
        possibleActions に含まれるアクションしか設定できないため、Copy を
        提供しないソース（Move のみ等）からのドラッグは受理しない。
        """
        if not (event.possibleActions() & Qt.DropAction.CopyAction):
            event.ignore()
            return False
        event.setDropAction(Qt.DropAction.CopyAction)
        event.accept()
        return True

    def dragEnterEvent(self, event: QDragEnterEvent) -> None:
        if self._image_paths_from_mime(event.mimeData()):
            self._accept_as_copy(event)
            return
        super().dragEnterEvent(event)

    def dragMoveEvent(self, event: QDragMoveEvent) -> None:
        # QGraphicsView 既定実装は scene 側へ転送し ignore され得るため、自前で accept する。
        if self._image_paths_from_mime(event.mimeData()):
            self._accept_as_copy(event)
            return
        super().dragMoveEvent(event)

    def dropEvent(self, event: QDropEvent) -> None:
        paths = self._image_paths_from_mime(event.mimeData())
        if paths:
            if self._accept_as_copy(event):
                scene_pos = self.mapToScene(event.position().toPoint())
                self.images_dropped.emit(paths, scene_pos)
            return
        super().dropEvent(event)

    # -- マウス: tool_manager への委譲 ---------------------------------------

    def mousePressEvent(self, event: QMouseEvent) -> None:
        if event.button() == Qt.MouseButton.MiddleButton:
            self._middle_panning = True
            self._begin_temp_pan()
            fake = QMouseEvent(
                QEvent.Type.MouseButtonPress,
                event.position(),
                event.globalPosition(),
                Qt.MouseButton.LeftButton,
                Qt.MouseButton.LeftButton,
                event.modifiers(),
            )
            super().mousePressEvent(fake)
            event.accept()
            return

        if self.tool_manager is not None:
            scene_pos = self.mapToScene(event.pos())
            if self.tool_manager.handle_mouse_press(event, scene_pos):
                event.accept()
                return
        super().mousePressEvent(event)

    def mouseMoveEvent(self, event: QMouseEvent) -> None:
        if self._middle_panning:
            fake = QMouseEvent(
                QEvent.Type.MouseMove,
                event.position(),
                event.globalPosition(),
                Qt.MouseButton.NoButton,
                Qt.MouseButton.LeftButton,
                event.modifiers(),
            )
            super().mouseMoveEvent(fake)
            event.accept()
            return

        if self.tool_manager is not None:
            scene_pos = self.mapToScene(event.pos())
            if self.tool_manager.handle_mouse_move(event, scene_pos):
                event.accept()
                return
        super().mouseMoveEvent(event)

    def mouseReleaseEvent(self, event: QMouseEvent) -> None:
        if event.button() == Qt.MouseButton.MiddleButton and self._middle_panning:
            fake = QMouseEvent(
                QEvent.Type.MouseButtonRelease,
                event.position(),
                event.globalPosition(),
                Qt.MouseButton.LeftButton,
                Qt.MouseButton.NoButton,
                event.modifiers(),
            )
            super().mouseReleaseEvent(fake)
            self._middle_panning = False
            if not self._space_panning:
                self._end_temp_pan()
            event.accept()
            return

        if self.tool_manager is not None:
            scene_pos = self.mapToScene(event.pos())
            if self.tool_manager.handle_mouse_release(event, scene_pos):
                event.accept()
                return
        super().mouseReleaseEvent(event)
