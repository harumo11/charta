"""パネル共通の小さなカスタムウィジェット群（P2契約 §1・D1）。"""

from __future__ import annotations

from app.ui.widgets.color_swatch_button import ColorSwatchButton
from app.ui.widgets.font_family_combo import FontFamilyCombo
from app.ui.widgets.scrub_label import ScrubLabel
from app.ui.widgets.simple_color_dialog import SimpleColorDialog
from app.ui.widgets.wheel_guard import install_wheel_guard

__all__ = [
    "ColorSwatchButton",
    "FontFamilyCombo",
    "ScrubLabel",
    "SimpleColorDialog",
    "install_wheel_guard",
]
