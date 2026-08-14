"""環境設定ダイアログ（`app/ui/prefs_dialog.py`）と `MainWindow` 統合（B契約 §D）。

`_isolated_prefs`（`tests/conftest.py`、autouse）により、実ユーザーの
`~/.config/charta/prefs.json` には一切触れない。
"""

from __future__ import annotations

from typing import Any

import pytest
import shiboken6
from PySide6.QtGui import QColor
from PySide6.QtWidgets import QColorDialog

from app.model.document import Document
from app.model.palettes import PALETTES, palette_by_id
from app.prefs import Preferences, load_prefs, prefs_path, save_prefs
from app.ui.main_window import MainWindow
from app.ui.prefs_dialog import PrefsDialog

_MATERIAL = palette_by_id("material")
assert _MATERIAL is not None


# --------------------------------------------------------------------------
# PrefsDialog 単体
# --------------------------------------------------------------------------


def test_no_palette_selected_disables_initial_color_combo(qapp: Any) -> None:
    dialog = PrefsDialog(Preferences(palette_id=""))
    assert dialog._palette_combo.currentText() == "（なし）"
    assert not dialog._initial_color_combo.isEnabled()
    assert dialog._initial_color_combo.count() == 1  # 「既定（黒）」のみ


def test_selecting_palette_enables_initial_color_combo_with_8_colors(qapp: Any) -> None:
    dialog = PrefsDialog(Preferences(palette_id=""))
    idx = dialog._palette_combo.findData(_MATERIAL.id)
    assert idx >= 0
    dialog._palette_combo.setCurrentIndex(idx)

    assert dialog._initial_color_combo.isEnabled()
    # 「既定（黒）」+ 8色。
    assert dialog._initial_color_combo.count() == 1 + len(_MATERIAL.colors)


def test_constructing_with_palette_and_initial_color_preselects_it(qapp: Any) -> None:
    prefs = Preferences(palette_id=_MATERIAL.id, initial_color=_MATERIAL.colors[3])
    dialog = PrefsDialog(prefs)
    assert dialog._initial_color_combo.isEnabled()
    assert dialog._initial_color_combo.currentIndex() == 4  # 1始まり(既定) + index3


def test_switching_palette_resets_initial_color_to_default(qapp: Any) -> None:
    prefs = Preferences(palette_id=_MATERIAL.id, initial_color=_MATERIAL.colors[3])
    dialog = PrefsDialog(prefs)
    other = PALETTES[1]
    assert other.id != _MATERIAL.id
    idx = dialog._palette_combo.findData(other.id)
    dialog._palette_combo.setCurrentIndex(idx)
    assert dialog._initial_color_combo.currentIndex() == 0


def test_editing_values_and_accept_updates_edited_prefs(qapp: Any) -> None:
    dialog = PrefsDialog(Preferences())

    dialog._font_size_spin.setValue(30.0)
    dialog._stroke_width_spin.setValue(5.5)
    idx = dialog._routing_combo.findData("straight")
    dialog._routing_combo.setCurrentIndex(idx)
    dialog._artboard_width_spin.setValue(800.0)
    dialog._artboard_height_spin.setValue(600.0)
    dialog._artboard_mm_spin.setValue(85.0)
    dialog._artboard_dpi_spin.setValue(600)
    dialog._autosave_spin.setValue(0)
    dialog._export_confirm_check.setChecked(False)
    dialog._outline_check.setChecked(True)
    dialog._transparent_check.setChecked(True)

    dialog.accept()
    edited = dialog.edited_prefs()

    assert edited.default_font_size == 30.0
    assert edited.default_stroke_width == 5.5
    assert edited.default_connector_routing == "straight"
    assert edited.artboard_width_px == 800.0
    assert edited.artboard_height_px == 600.0
    assert edited.artboard_width_mm == 85.0
    assert edited.artboard_dpi == 600
    assert edited.autosave_interval_s == 0
    assert edited.export_confirm is False
    assert edited.export_outline_text is True
    assert edited.export_transparent_png is True


def test_accept_preserves_fields_not_shown_in_dialog(qapp: Any) -> None:
    prefs = Preferences(window_geometry=[1, 2, 3, 4], grid_visible=True, snap_enabled=False)
    dialog = PrefsDialog(prefs)
    dialog.accept()
    edited = dialog.edited_prefs()
    assert edited.window_geometry == [1, 2, 3, 4]
    assert edited.grid_visible is True
    assert edited.snap_enabled is False


def test_background_color_button_updates_edited_prefs(
    qapp: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(QColorDialog, "getColor", staticmethod(lambda *a, **k: QColor("#123456")))
    dialog = PrefsDialog(Preferences())
    dialog._bg_button.click()
    dialog.accept()
    assert dialog.edited_prefs().artboard_background.lower() == "#123456"


def test_register_button_disabled_when_callback_is_none(qapp: Any) -> None:
    dialog = PrefsDialog(Preferences(palette_id=_MATERIAL.id), on_register_styles=None)
    assert not dialog._register_button.isEnabled()


def test_register_button_disabled_when_no_palette_selected(qapp: Any) -> None:
    dialog = PrefsDialog(Preferences(palette_id=""), on_register_styles=lambda p: None)
    assert not dialog._register_button.isEnabled()


def test_register_button_calls_callback_once_and_relabels(qapp: Any) -> None:
    calls: list[Any] = []
    dialog = PrefsDialog(Preferences(palette_id=_MATERIAL.id), on_register_styles=calls.append)
    assert dialog._register_button.isEnabled()

    dialog._register_button.click()

    assert len(calls) == 1
    assert calls[0].id == _MATERIAL.id
    assert dialog._register_button.text() == "登録済み"


# --------------------------------------------------------------------------
# MainWindow 統合
# --------------------------------------------------------------------------


@pytest.fixture
def window(qapp: Any) -> Any:
    w = MainWindow()
    yield w
    if shiboken6.isValid(w):
        w.close()


def test_main_window_loads_prefs_on_construction(window: Any) -> None:
    assert isinstance(window.prefs, Preferences)


def test_main_window_restores_window_geometry_from_prefs(qapp: Any) -> None:
    save_prefs(Preferences(window_geometry=[10, 20, 640, 480]))
    w = MainWindow()
    try:
        geo = w.geometry()
        assert (geo.x(), geo.y(), geo.width(), geo.height()) == (10, 20, 640, 480)
    finally:
        w.close()


def test_main_window_close_event_writes_prefs_json(window: Any) -> None:
    assert not prefs_path().exists()
    window.close()
    assert prefs_path().exists()


def test_main_window_restores_grid_and_snap_state_from_prefs(qapp: Any) -> None:
    save_prefs(Preferences(grid_visible=True, snap_enabled=False))
    w = MainWindow()
    try:
        assert w._grid_action.isChecked()
        assert w.scene._grid_visible  # QAction の見た目だけでなく scene 実体も揃う
        assert not w._snap_action.isChecked()
        assert not w.scene.snap_enabled
    finally:
        w.close()


def test_main_window_grid_toggle_updates_and_saves_prefs(window: Any) -> None:
    assert window.prefs.grid_visible is False
    window._grid_action.setChecked(True)
    assert window.prefs.grid_visible is True
    assert load_prefs().grid_visible is True


def test_main_window_snap_toggle_updates_and_saves_prefs(window: Any) -> None:
    assert window.prefs.snap_enabled is True
    window._snap_action.setChecked(False)
    assert window.prefs.snap_enabled is False
    assert load_prefs().snap_enabled is False


def test_main_window_autosave_timer_inactive_when_interval_zero(qapp: Any) -> None:
    save_prefs(Preferences(autosave_interval_s=0))
    w = MainWindow()
    try:
        assert not w._autosave_timer.isActive()
    finally:
        w.close()


def test_main_window_autosave_timer_active_by_default(window: Any) -> None:
    assert window._autosave_timer.isActive()


def test_apply_palette_swatches_loads_qcolordialog_custom_colors(qapp: Any) -> None:
    save_prefs(Preferences(palette_id=_MATERIAL.id))
    w = MainWindow()
    try:
        for i, color in enumerate(_MATERIAL.colors):
            assert QColorDialog.customColor(i).name().lower() == color.lower()
    finally:
        w.close()


def test_register_palette_styles_pushes_single_undo_command(window: Any) -> None:
    document: Document = window.scene.document
    assert document.styles == {}

    window._register_palette_styles(_MATERIAL)

    assert len(document.styles) == len(_MATERIAL.colors)
    assert document.styles[f"{_MATERIAL.id}-1"]["fill"] == _MATERIAL.colors[0]

    window.undo_stack.undo()
    assert document.styles == {}

    window.undo_stack.redo()
    assert len(document.styles) == len(_MATERIAL.colors)


def test_open_preferences_accept_persists_and_applies(
    qapp: Any, window: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    """`open_preferences()` は accept 後に save_prefs + 実行時反映を行う。"""

    class _FakeDialog:
        def __init__(self, prefs: Preferences, on_register_styles: Any = None) -> None:
            self._edited = Preferences(
                **{**prefs.to_dict(), "autosave_interval_s": 0, "palette_id": _MATERIAL.id}
            )

        def exec(self) -> int:
            return 1  # QDialog.DialogCode.Accepted

        def edited_prefs(self) -> Preferences:
            return self._edited

    monkeypatch.setattr("app.ui.main_window.PrefsDialog", _FakeDialog)

    window.open_preferences()

    assert window.prefs.autosave_interval_s == 0
    assert not window._autosave_timer.isActive()
    assert load_prefs().palette_id == _MATERIAL.id
    assert QColorDialog.customColor(0).name().lower() == _MATERIAL.colors[0].lower()
    # `self.prefs` は参照を差し替えず中身だけ更新するため、コンストラクタで同じ
    # オブジェクトを受け取った ToolManager/ExportController からも新しい値が
    # 即座に見える（`_update_prefs_in_place` の存在理由そのもの）。
    assert window.tool_manager.prefs is window.prefs
    assert window._export.prefs is window.prefs


def test_open_preferences_reject_leaves_prefs_unchanged(
    qapp: Any, window: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    class _FakeDialog:
        def __init__(self, prefs: Preferences, on_register_styles: Any = None) -> None:
            pass

        def exec(self) -> int:
            return 0  # QDialog.DialogCode.Rejected

        def edited_prefs(self) -> Preferences:  # pragma: no cover - 呼ばれないはず
            raise AssertionError("rejected dialog の edited_prefs は使われない")

    monkeypatch.setattr("app.ui.main_window.PrefsDialog", _FakeDialog)
    before = load_prefs()

    window.open_preferences()

    assert window.prefs == before
