"""ColorSwatchButton: 色プロパティ用の単一スウォッチボタン（P2契約 D1-4・要望8/11）。

**面（要望8）**: ボタン背景はテーマの通常色のまま（`setStyleSheet` を一切使わない）。
以前はボタン背景色を `setStyleSheet("background-color: ...")` で塗っていたため、
子の `QMenu` とツールチップまで同じ背景を継承して黒一色になり読めなかった
（要望8の直接原因、`reports/color.md` §1 で実測）。色は左のチップ（`QIcon`）で
示し、テキストは大文字 hex / `null_label` / 混在時「混在」、右端に自前描画の
山形（▾）を出す。

**メニュー（要望11）**: `setMenu()` で常に付ける（null 不可の色行にも）。
パレット8色（`dropdown_colors`）＋区切り＋（nullable のときだけ）null_label＋
区切り＋「色を選択…」。パレット変更時にメニューを作り直すのでテストが
`button.menu().actions()` を直接叩いても最新の状態を見られる。
"""

from __future__ import annotations

from PySide6.QtCore import QRect, QSize, Qt, Signal
from PySide6.QtGui import QColor, QIcon, QPainter, QPaintEvent, QPen, QPixmap
from PySide6.QtWidgets import QMenu, QPushButton, QStyle, QStyleOption, QWidget

from app.model.palettes import Palette, dropdown_colors
from app.ui.widgets.simple_color_dialog import SimpleColorDialog

_CHIP_SIZE = QSize(20, 14)
_CHEVRON_SIZE = 8


def _chip_icon(color: str | None) -> QIcon:
    """色（`None` なら「なし」の斜線）を描いた小さな `QIcon` を作る。"""
    pixmap = QPixmap(_CHIP_SIZE)
    pixmap.fill(Qt.GlobalColor.transparent)
    painter = QPainter(pixmap)
    painter.setRenderHint(QPainter.RenderHint.Antialiasing)
    rect = pixmap.rect().adjusted(1, 1, -1, -1)
    painter.setPen(QPen(QColor(0, 0, 0, 90), 1))
    if color is None:
        painter.setBrush(QColor("#FFFFFF"))
        painter.drawRoundedRect(rect, 2, 2)
        painter.setPen(QPen(QColor("#D9534F"), 1.5))
        painter.drawLine(rect.topLeft(), rect.bottomRight())
    else:
        painter.setBrush(QColor(color))
        painter.drawRoundedRect(rect, 2, 2)
    painter.end()
    return QIcon(pixmap)


class ColorSwatchButton(QPushButton):
    """色プロパティ1行ぶんのスウォッチボタン（クリックでメニュー、null 可）。"""

    # ユーザー操作（メニュー項目・ダイアログ OK）でだけ発火。値は大文字 hex か None。
    color_chosen = Signal(object)

    def __init__(
        self,
        parent: QWidget | None = None,
        *,
        nullable: bool = False,
        null_label: str = "なし",
        dialog_title: str = "色を選択",
    ) -> None:
        super().__init__(parent)
        self._nullable = nullable
        self._null_label = null_label
        self._dialog_title = dialog_title
        self._value: str | None = None
        self._mixed = False
        self._palette: Palette | None = None
        self._menu = QMenu(self)
        self.setMenu(self._menu)
        self._rebuild_menu()
        self._refresh_face()

    # ------------------------------------------------------------------
    # 公開 API（P2契約 D1-4）
    # ------------------------------------------------------------------
    def set_value(self, color: str | None) -> None:
        """シグナルを出さずに現在値を設定する。"""
        self._mixed = False
        self._value = color.upper() if color is not None else None
        self._rebuild_menu()
        self._refresh_face()

    def value(self) -> str | None:
        return self._value

    def set_mixed(self, mixed: bool) -> None:
        """複数選択で値が混在していることを示す（シグナルは出さない）。"""
        self._mixed = mixed
        self._rebuild_menu()
        self._refresh_face()

    def set_palette(self, palette: Palette | None) -> None:
        """メニューとダイアログで使うパレットを差し替える。"""
        self._palette = palette
        self._rebuild_menu()

    # ------------------------------------------------------------------
    # メニュー構築・選択
    # ------------------------------------------------------------------
    def _rebuild_menu(self) -> None:
        self._menu.clear()
        current = self._value if self._value else None
        for color in dropdown_colors(self._palette):
            action = self._menu.addAction(_chip_icon(color), color)
            action.setCheckable(True)
            action.setChecked(not self._mixed and current == color)
            action.triggered.connect(lambda _checked=False, c=color: self._choose(c))
        self._menu.addSeparator()
        if self._nullable:
            null_action = self._menu.addAction(_chip_icon(None), self._null_label)
            null_action.setCheckable(True)
            null_action.setChecked(not self._mixed and current is None)
            null_action.triggered.connect(lambda _checked=False: self._choose(None))
            self._menu.addSeparator()
        pick_action = self._menu.addAction("色を選択…")
        pick_action.triggered.connect(self._open_dialog)

    def _choose(self, color: str | None) -> None:
        new_value = color.upper() if color is not None else None
        old_value = self._value if self._value else None
        if not self._mixed and new_value == old_value:
            return
        self._mixed = False
        self._value = new_value
        self._rebuild_menu()
        self._refresh_face()
        self.color_chosen.emit(new_value)

    def _open_dialog(self) -> None:
        result = SimpleColorDialog.get_color(
            self._value, self._palette, self.window(), self._dialog_title
        )
        if result is None:
            return
        self._choose(result)

    # ------------------------------------------------------------------
    # 面
    # ------------------------------------------------------------------
    def _refresh_face(self) -> None:
        if self._mixed:
            self.setIcon(QIcon())
            self.setText("混在")
            self.setToolTip("（混在）")
        elif self._value is None:
            self.setIcon(_chip_icon(None))
            self.setText(self._null_label)
            self.setToolTip(self._null_label)
        else:
            self.setIcon(_chip_icon(self._value))
            self.setText(self._value)
            self.setToolTip(self._value)

    def sizeHint(self) -> QSize:  # noqa: N802 (Qt override)
        # 値（hex/null_label/「混在」の文字数）で sizeHint が動くと、色を変える
        # たびに行の幅・高さが揺れる（`tests/test_panel_row_metrics.py` が
        # 固定する不変条件）。実際に取りうる表示文字列のうち最長のものを基準に
        # 固定し、現在の表示内容には依存しない。
        metrics = self.fontMetrics()
        candidates = ("#FFFFFF", "混在", self._null_label)
        text_width = max(metrics.horizontalAdvance(c) for c in candidates)
        content_w = _CHIP_SIZE.width() + 6 + text_width + _CHEVRON_SIZE + 16
        content_h = max(metrics.height(), _CHIP_SIZE.height()) + 12
        return QSize(content_w, content_h)

    def paintEvent(self, event: QPaintEvent) -> None:  # noqa: N802 (Qt override)
        super().paintEvent(event)
        # 山形（▾）を自前で描く。`#propertyPanelForm QPushButton::menu-indicator`
        # の `image:none` が既定のメニュー矢印を消しているため（色面が矢印で
        # 食い込まれないようにする既存方針）、その内側で自分の矢印を出す。
        painter = QPainter(self)
        opt = QStyleOption()
        opt.initFrom(self)
        x = self.width() - _CHEVRON_SIZE - 8
        y = (self.height() - _CHEVRON_SIZE) // 2
        opt.rect = QRect(x, y, _CHEVRON_SIZE, _CHEVRON_SIZE)
        self.style().drawPrimitive(
            QStyle.PrimitiveElement.PE_IndicatorArrowDown, opt, painter, self
        )
        painter.end()
