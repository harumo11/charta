"""ZoomPill: キャンバス右下に浮かぶズームコントロール（契約 §2）。

`CanvasView.viewport()` の子ウィジェットとして浮遊表示する。シーンには入れない
（`QGraphicsProxyWidget` は使わない）。
"""

from __future__ import annotations

from PySide6.QtCore import QEvent, QObject, QSize, Qt
from PySide6.QtGui import QColor, QFont, QMoveEvent, QPainter, QPaintEvent, QPen
from PySide6.QtWidgets import QHBoxLayout, QLabel, QToolButton, QWidget

from app.scene.canvas_view import CanvasView
from app.ui.theme import icons
from app.ui.theme.tokens import current_theme

_MARGIN = 12
_ICON_SIZE = 14
_BUTTON_SIZE = 24
_LABEL_MIN_WIDTH = 44


class ZoomPill(QWidget):
    """キャンバス右下に浮かぶズームコントロール（viewport 子ウィジェット）。"""

    def __init__(self, view: CanvasView) -> None:
        super().__init__(view.viewport())
        self.setObjectName("zoomPill")
        self._view = view
        self.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground)

        layout = QHBoxLayout(self)
        layout.setContentsMargins(6, 3, 6, 3)
        layout.setSpacing(2)

        self._minus = self._make_button("mdi6.minus", "縮小")
        self._label = self._make_label()
        self._plus = self._make_button("mdi6.plus", "拡大")
        self._fit = self._make_button("mdi6.fit-to-screen-outline", "全体表示")

        layout.addWidget(self._minus)
        layout.addWidget(self._label)
        layout.addWidget(self._plus)
        layout.addWidget(self._fit)

        self._minus.clicked.connect(view.zoom_out)
        self._plus.clicked.connect(view.zoom_in)
        self._fit.clicked.connect(view.fit_to_artboard)
        view.zoom_changed.connect(self._on_zoom_changed)

        view.viewport().installEventFilter(self)
        self.adjustSize()
        self._reposition()
        self.show()

    def _make_button(self, icon_name: str, tooltip: str) -> QToolButton:
        button = QToolButton(self)
        button.setIcon(icons.icon(icon_name))
        button.setIconSize(QSize(_ICON_SIZE, _ICON_SIZE))
        button.setFixedSize(_BUTTON_SIZE, _BUTTON_SIZE)
        button.setFocusPolicy(Qt.FocusPolicy.NoFocus)
        button.setToolTip(tooltip)
        button.setAutoRaise(True)
        return button

    def _make_label(self) -> QLabel:
        label = QLabel("100%", self)
        label.setAlignment(Qt.AlignmentFlag.AlignCenter)
        label.setMinimumWidth(_LABEL_MIN_WIDTH)
        # 桁が変わってもガタつかないよう、tabular figures(OpenType "tnum")を有効にする。
        font = label.font()
        font.setFeature(QFont.Tag("tnum"), 1)
        label.setFont(font)
        return label

    def _on_zoom_changed(self, zoom: float) -> None:
        self._label.setText(f"{round(zoom * 100)}%")
        self._reposition()

    def eventFilter(self, watched: QObject, event: QEvent) -> bool:
        if watched is self._view.viewport() and event.type() == QEvent.Type.Resize:
            self._reposition()
        return super().eventFilter(watched, event)

    def moveEvent(self, event: QMoveEvent) -> None:
        """右下アンカーへ補正し直す。

        `QGraphicsView` はリサイズ時の再センタリング等で `viewport().scroll()` を内部的に
        使うことがあり、それは viewport の子ウィジェット（このピル）ごと平行移動させて
        しまう（`Resize` イベントより後に起こり得るため `_reposition()` だけでは追い切れ
        ない）。`_reposition()` が既に右下アンカー位置なら `move()` は無変化で
        再帰しない（Qt は同一座標への move を no-op にする）。
        """
        super().moveEvent(event)
        self._reposition()

    def _reposition(self) -> None:
        self.adjustSize()
        viewport = self._view.viewport()
        x = viewport.width() - self.width() - _MARGIN
        y = viewport.height() - self.height() - _MARGIN
        self.move(max(0, x), max(0, y))

    def paintEvent(self, event: QPaintEvent) -> None:  # noqa: ARG002 - Qt override signature
        """疑似ガラス風の角丸背景（ブラーなし）を描く。"""
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        theme = current_theme()
        radius = self.height() / 2.0
        rect = self.rect().adjusted(0, 0, -1, -1)
        painter.setPen(QPen(QColor(theme.border), 1))
        painter.setBrush(QColor(255, 255, 255, 235))
        painter.drawRoundedRect(rect, radius, radius)
        painter.end()
