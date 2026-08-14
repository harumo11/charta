"""環境設定ダイアログ（B-1契約）。

house style は `math_item.edit_latex` のダイアログ（QDialog + QDialogButtonBox、
親なし）。`app.prefs.Preferences` のコピーを編集し、OK で確定した内容を
`edited_prefs()` で取り出す。ダイアログ自身は `Document`/`MainWindow` に一切
触れない（見た目キーの束をプロジェクトへ登録する処理は `on_register_styles`
コールバックへ委譲する）。
"""

from __future__ import annotations

from collections.abc import Callable

from PySide6.QtGui import QColor, QFont, QIcon, QPixmap
from PySide6.QtWidgets import (
    QCheckBox,
    QColorDialog,
    QComboBox,
    QDialog,
    QDialogButtonBox,
    QDoubleSpinBox,
    QFontComboBox,
    QFormLayout,
    QFrame,
    QGroupBox,
    QHBoxLayout,
    QLabel,
    QPushButton,
    QSpinBox,
    QVBoxLayout,
    QWidget,
)

from app.model.palettes import PALETTES, Palette, palette_by_id
from app.prefs import Preferences

#: パレット未選択を表す QComboBox の userData（"" = palette_id 上も「なし」と一致）。
_NO_PALETTE_DATA = ""
#: カラーチップ・アイコンの一辺(px)。
_SWATCH_SIZE = 24
_ICON_SIZE = 16


def _color_icon(color: str, size: int = _ICON_SIZE) -> QIcon:
    """`color`（"#RRGGBB"）で塗りつぶした正方形の `QIcon` を作る。"""
    pixmap = QPixmap(size, size)
    pixmap.fill(QColor(color))
    return QIcon(pixmap)


class PrefsDialog(QDialog):
    """環境設定ダイアログ（4セクション・スクロールバー無し）。"""

    def __init__(
        self,
        prefs: Preferences,
        on_register_styles: Callable[[Palette], None] | None = None,
    ) -> None:
        super().__init__()
        self.setWindowTitle("環境設定")
        # 設定ダイアログに出さない自動記憶フィールド(version/window_geometry/
        # grid_visible/snap_enabled)を accept() 時にそのまま持ち越すための控え。
        self._initial_prefs = prefs
        self._edited_prefs = prefs
        self._on_register_styles = on_register_styles

        layout = QVBoxLayout(self)
        layout.addWidget(self._build_palette_group(prefs))
        layout.addWidget(self._build_object_defaults_group(prefs))
        layout.addWidget(self._build_artboard_defaults_group(prefs))
        layout.addWidget(self._build_autosave_export_group(prefs))

        buttons = QDialogButtonBox(
            QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel
        )
        buttons.accepted.connect(self.accept)
        buttons.rejected.connect(self.reject)
        layout.addWidget(buttons)

    # ------------------------------------------------------------------
    # セクション1: カラーパレット
    # ------------------------------------------------------------------

    def _build_palette_group(self, prefs: Preferences) -> QGroupBox:
        group = QGroupBox("カラーパレット")
        outer = QVBoxLayout(group)

        self._palette_combo = QComboBox()
        self._palette_combo.addItem("（なし）", _NO_PALETTE_DATA)
        for palette in PALETTES:
            self._palette_combo.addItem(palette.name, palette.id)
        idx = self._palette_combo.findData(prefs.palette_id)
        self._palette_combo.setCurrentIndex(idx if idx >= 0 else 0)

        combo_form = QFormLayout()
        combo_form.addRow("パレット", self._palette_combo)
        outer.addLayout(combo_form)

        swatch_container = QWidget()
        self._swatch_layout = QHBoxLayout(swatch_container)
        self._swatch_layout.setContentsMargins(0, 0, 0, 0)
        outer.addWidget(swatch_container)

        self._initial_color_combo = QComboBox()
        self._rebuild_initial_color_combo(self._current_palette(), prefs.initial_color)
        color_form = QFormLayout()
        color_form.addRow("新規図形の初期色", self._initial_color_combo)
        outer.addLayout(color_form)

        self._register_button = QPushButton("このプロジェクトに styles として登録")
        self._register_button.setEnabled(
            self._on_register_styles is not None and self._current_palette() is not None
        )
        self._register_button.clicked.connect(self._on_register_clicked)
        outer.addWidget(self._register_button)

        # 初期プレビューの構築（初期化順の都合で最後にまとめて行う）。
        self._update_swatch_preview(self._current_palette())
        # ここでの接続は初期表示を組み終えた後（プログラム的な setCurrentIndex で
        # ハンドラが余計に走らないようにするため）。
        self._palette_combo.currentIndexChanged.connect(self._on_palette_changed)

        return group

    def _current_palette_id(self) -> str:
        idx = self._palette_combo.currentIndex()
        data = self._palette_combo.itemData(idx)
        return data if isinstance(data, str) else _NO_PALETTE_DATA

    def _current_palette(self) -> Palette | None:
        return palette_by_id(self._current_palette_id())

    def _update_swatch_preview(self, palette: Palette | None) -> None:
        layout = self._swatch_layout
        while layout.count():
            item = layout.takeAt(0)
            widget = item.widget()
            if widget is not None:
                widget.deleteLater()
        for color in palette.colors if palette is not None else ():
            chip = QFrame()
            chip.setFixedSize(_SWATCH_SIZE, _SWATCH_SIZE)
            chip.setStyleSheet(f"background-color: {color}; border: 1px solid #00000033;")
            layout.addWidget(chip)
        layout.addStretch(1)

    def _rebuild_initial_color_combo(
        self, palette: Palette | None, current_value: str | None
    ) -> None:
        combo = self._initial_color_combo
        combo.blockSignals(True)
        combo.clear()
        combo.addItem("既定（黒）", None)
        if palette is not None:
            for color in palette.colors:
                combo.addItem(_color_icon(color), color, color)
        combo.setEnabled(palette is not None)
        index = 0
        if palette is not None and current_value is not None:
            try:
                index = 1 + palette.colors.index(current_value)
            except ValueError:
                index = 0
        combo.setCurrentIndex(index)
        combo.blockSignals(False)

    def _on_palette_changed(self, _index: int) -> None:
        """パレットの選択が変わったら、初期色コンボと登録ボタンを追従させる。

        別のパレットに切り替えた時点で、前のパレットの色を指していた「初期色」の
        選択は意味を失うため既定（黒）に戻す。登録ボタンの「登録済み」表示も
        新しい選択に対してはまだ登録していないので元の文言に戻す。
        """
        palette = self._current_palette()
        self._update_swatch_preview(palette)
        self._rebuild_initial_color_combo(palette, None)
        self._register_button.setEnabled(
            self._on_register_styles is not None and palette is not None
        )
        self._register_button.setText("このプロジェクトに styles として登録")

    def _on_register_clicked(self) -> None:
        if self._on_register_styles is None:
            return
        palette = self._current_palette()
        if palette is None:
            return
        self._on_register_styles(palette)
        self._register_button.setText("登録済み")

    # ------------------------------------------------------------------
    # セクション2: 新規オブジェクトの既定
    # ------------------------------------------------------------------

    def _build_object_defaults_group(self, prefs: Preferences) -> QGroupBox:
        group = QGroupBox("新規オブジェクトの既定")
        form = QFormLayout(group)

        self._font_combo = QFontComboBox()
        self._font_combo.setCurrentFont(QFont(prefs.default_font_family))
        form.addRow("フォント", self._font_combo)

        self._font_size_spin = QDoubleSpinBox()
        self._font_size_spin.setRange(6.0, 128.0)
        self._font_size_spin.setValue(prefs.default_font_size)
        form.addRow("サイズ", self._font_size_spin)

        self._stroke_width_spin = QDoubleSpinBox()
        self._stroke_width_spin.setRange(0.0, 50.0)
        self._stroke_width_spin.setSingleStep(0.5)
        self._stroke_width_spin.setValue(prefs.default_stroke_width)
        form.addRow("線幅", self._stroke_width_spin)

        self._routing_combo = QComboBox()
        self._routing_combo.addItem("直角折れ線 (orthogonal)", "orthogonal")
        self._routing_combo.addItem("直線 (straight)", "straight")
        idx = self._routing_combo.findData(prefs.default_connector_routing)
        self._routing_combo.setCurrentIndex(idx if idx >= 0 else 0)
        form.addRow("コネクタ routing", self._routing_combo)

        return group

    # ------------------------------------------------------------------
    # セクション3: 新規アートボードの既定
    # ------------------------------------------------------------------

    def _build_artboard_defaults_group(self, prefs: Preferences) -> QGroupBox:
        group = QGroupBox("新規アートボードの既定")
        form = QFormLayout(group)

        self._artboard_width_spin = QDoubleSpinBox()
        self._artboard_width_spin.setDecimals(0)
        self._artboard_width_spin.setRange(1.0, 20000.0)
        self._artboard_width_spin.setValue(prefs.artboard_width_px)
        form.addRow("幅 (px)", self._artboard_width_spin)

        self._artboard_height_spin = QDoubleSpinBox()
        self._artboard_height_spin.setDecimals(0)
        self._artboard_height_spin.setRange(1.0, 20000.0)
        self._artboard_height_spin.setValue(prefs.artboard_height_px)
        form.addRow("高さ (px)", self._artboard_height_spin)

        self._artboard_mm_spin = QDoubleSpinBox()
        self._artboard_mm_spin.setRange(1.0, 2000.0)
        self._artboard_mm_spin.setValue(prefs.artboard_width_mm)
        form.addRow("幅 (mm)", self._artboard_mm_spin)

        self._artboard_dpi_spin = QSpinBox()
        self._artboard_dpi_spin.setRange(72, 1200)
        self._artboard_dpi_spin.setValue(prefs.artboard_dpi)
        form.addRow("DPI", self._artboard_dpi_spin)

        self._bg_color_value = prefs.artboard_background
        self._bg_button = QPushButton()
        self._apply_bg_button_color(self._bg_color_value)
        self._bg_button.clicked.connect(self._on_bg_button_clicked)
        form.addRow("背景色", self._bg_button)

        note = QLabel("新規作成時に適用（現在のドキュメントは変わりません）")
        note.setStyleSheet("color: gray;")
        form.addRow(note)

        return group

    def _apply_bg_button_color(self, color: str) -> None:
        self._bg_color_value = color
        self._bg_button.setText(color)
        self._bg_button.setStyleSheet(f"background-color: {color};")

    def _on_bg_button_clicked(self) -> None:
        color = QColorDialog.getColor(QColor(self._bg_color_value), self, "背景色")
        if not color.isValid():
            return
        self._apply_bg_button_color(color.name())

    # ------------------------------------------------------------------
    # セクション4: 自動保存・書き出し
    # ------------------------------------------------------------------

    def _build_autosave_export_group(self, prefs: Preferences) -> QGroupBox:
        group = QGroupBox("自動保存・書き出し")
        form = QFormLayout(group)

        self._autosave_spin = QSpinBox()
        self._autosave_spin.setRange(0, 600)
        self._autosave_spin.setSuffix(" 秒")
        self._autosave_spin.setSpecialValueText("OFF")
        self._autosave_spin.setValue(prefs.autosave_interval_s)
        form.addRow("自動保存間隔", self._autosave_spin)

        self._export_confirm_check = QCheckBox("書き出し時に毎回確認する")
        self._export_confirm_check.setChecked(prefs.export_confirm)
        form.addRow(self._export_confirm_check)

        tooltip = "確認ダイアログを出す設定のときに、ダイアログの既定選択になります。"
        self._outline_check = QCheckBox("テキストをアウトライン化")
        self._outline_check.setChecked(prefs.export_outline_text)
        self._outline_check.setToolTip(tooltip)
        form.addRow(self._outline_check)

        self._transparent_check = QCheckBox("PNG の背景を透過")
        self._transparent_check.setChecked(prefs.export_transparent_png)
        self._transparent_check.setToolTip(tooltip)
        form.addRow(self._transparent_check)

        return group

    # ------------------------------------------------------------------
    # 確定
    # ------------------------------------------------------------------

    def _collect_prefs(self) -> Preferences:
        """ウィジェットの現在値から新しい `Preferences` を組み立てる。

        設定ダイアログに出さないフィールド(version/window_geometry/grid_visible/
        snap_enabled)は、ダイアログに渡された時点の値をそのまま持ち越す。
        """
        palette = self._current_palette()
        color_index = self._initial_color_combo.currentIndex()
        initial_color = (
            palette.colors[color_index - 1] if palette is not None and color_index > 0 else None
        )
        base = self._initial_prefs
        return Preferences(
            version=base.version,
            palette_id=self._current_palette_id(),
            initial_color=initial_color,
            default_font_family=self._font_combo.currentFont().family(),
            default_font_size=self._font_size_spin.value(),
            default_stroke_width=self._stroke_width_spin.value(),
            default_connector_routing=self._routing_combo.currentData(),
            artboard_width_px=self._artboard_width_spin.value(),
            artboard_height_px=self._artboard_height_spin.value(),
            artboard_width_mm=self._artboard_mm_spin.value(),
            artboard_dpi=self._artboard_dpi_spin.value(),
            artboard_background=self._bg_color_value,
            autosave_interval_s=self._autosave_spin.value(),
            export_outline_text=self._outline_check.isChecked(),
            export_transparent_png=self._transparent_check.isChecked(),
            export_confirm=self._export_confirm_check.isChecked(),
            window_geometry=base.window_geometry,
            grid_visible=base.grid_visible,
            snap_enabled=base.snap_enabled,
        )

    def accept(self) -> None:  # noqa: D102 (Qt override、docstring はクラス docstring 参照)
        self._edited_prefs = self._collect_prefs()
        super().accept()

    def edited_prefs(self) -> Preferences:
        """編集後の `Preferences`。OK 前に呼ばれた場合は渡された初期値と同じ内容を返す。"""
        return self._edited_prefs
