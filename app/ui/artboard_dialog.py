"""アートボード物理サイズプリセットダイアログ（§9.6/§6）。"""

from __future__ import annotations

from PySide6.QtGui import QColor
from PySide6.QtWidgets import (
    QColorDialog,
    QComboBox,
    QDialog,
    QDialogButtonBox,
    QDoubleSpinBox,
    QFormLayout,
    QMainWindow,
    QPushButton,
    QSpinBox,
    QVBoxLayout,
)

from app.model.document import Artboard, Physical

# アートボードプリセット(§9): (表示名, width_mm, target_dpi, アスペクト比w, アスペクト比h)。
ARTBOARD_PRESETS: list[tuple[str, float, int, int, int]] = [
    ("1カラム figure (84mm, 300dpi, 4:3)", 84.0, 300, 4, 3),
    ("2カラム figure (174mm, 300dpi, 4:3)", 174.0, 300, 4, 3),
    ("スライド 16:9 (250mm相当, 150dpi)", 250.0, 150, 16, 9),
]
ARTBOARD_CUSTOM_LABEL = "カスタム"


def preset_px_size(
    width_mm: float, target_dpi: int, aspect_w: int, aspect_h: int
) -> tuple[int, int]:
    """物理サイズプリセットから width_px/height_px を算出する。"""
    width_px = round(width_mm / 25.4 * target_dpi)
    height_px = round(width_px * aspect_h / aspect_w)
    return max(width_px, 1), max(height_px, 1)


class ArtboardDialog(QDialog):
    """アートボード設定ダイアログ（物理サイズプリセット + カスタム、§9）。"""

    def __init__(self, artboard: Artboard, parent: QMainWindow | None = None) -> None:
        super().__init__(parent)
        self.setWindowTitle("アートボード設定")
        self._background = artboard.background

        self.preset_combo = QComboBox()
        for label, *_rest in ARTBOARD_PRESETS:
            self.preset_combo.addItem(label)
        self.preset_combo.addItem(ARTBOARD_CUSTOM_LABEL)

        self.width_mm_spin = QDoubleSpinBox()
        self.width_mm_spin.setRange(1.0, 2000.0)
        self.width_mm_spin.setDecimals(1)
        self.width_mm_spin.setValue(artboard.physical.width_mm)

        self.dpi_spin = QSpinBox()
        self.dpi_spin.setRange(1, 2400)
        self.dpi_spin.setValue(artboard.physical.target_dpi)

        self.width_px_spin = QSpinBox()
        self.width_px_spin.setRange(1, 20000)
        self.width_px_spin.setValue(artboard.width_px)

        self.height_px_spin = QSpinBox()
        self.height_px_spin.setRange(1, 20000)
        self.height_px_spin.setValue(artboard.height_px)

        self.background_button = QPushButton()
        self._refresh_background_button()
        self.background_button.clicked.connect(self._pick_background)

        form = QFormLayout()
        form.addRow("プリセット", self.preset_combo)
        form.addRow("幅 (mm)", self.width_mm_spin)
        form.addRow("解像度 (dpi)", self.dpi_spin)
        form.addRow("幅 (px)", self.width_px_spin)
        form.addRow("高さ (px)", self.height_px_spin)
        form.addRow("背景色", self.background_button)

        buttons = QDialogButtonBox(
            QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel
        )
        buttons.accepted.connect(self.accept)
        buttons.rejected.connect(self.reject)

        layout = QVBoxLayout(self)
        layout.addLayout(form)
        layout.addWidget(buttons)

        # カスタム(現在値のまま): プリセット未選択の初期状態にする。
        self.preset_combo.setCurrentIndex(len(ARTBOARD_PRESETS))
        self.preset_combo.currentIndexChanged.connect(self._apply_preset)

    def _apply_preset(self, index: int) -> None:
        if index < 0 or index >= len(ARTBOARD_PRESETS):
            return  # カスタム選択時はフィールドをそのまま維持する。
        _label, width_mm, dpi, aspect_w, aspect_h = ARTBOARD_PRESETS[index]
        width_px, height_px = preset_px_size(width_mm, dpi, aspect_w, aspect_h)
        self.width_mm_spin.setValue(width_mm)
        self.dpi_spin.setValue(dpi)
        self.width_px_spin.setValue(width_px)
        self.height_px_spin.setValue(height_px)

    def _refresh_background_button(self) -> None:
        self.background_button.setText(self._background)
        self.background_button.setStyleSheet(f"background-color: {self._background};")

    def _pick_background(self) -> None:
        color = QColorDialog.getColor(QColor(self._background), self, "背景色")
        if color.isValid():
            self._background = color.name()
            self._refresh_background_button()

    def result_artboard(self) -> Artboard:
        """ダイアログの入力値から新しい `Artboard` を組み立てる。"""
        return Artboard(
            width_px=self.width_px_spin.value(),
            height_px=self.height_px_spin.value(),
            physical=Physical(
                width_mm=self.width_mm_spin.value(),
                target_dpi=self.dpi_spin.value(),
            ),
            background=self._background,
        )
