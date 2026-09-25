"""環境設定ダイアログ（`app/ui/prefs_dialog.py`）と `MainWindow` 統合（B契約 §D）。

`_isolated_prefs`（`tests/conftest.py`、autouse）により、実ユーザーの
`~/.config/charta/prefs.json` には一切触れない。
"""

from __future__ import annotations

from typing import Any

import pytest
import shiboken6
from PySide6.QtGui import QGuiApplication

from app.commands.commands import SetPropertyCommand
from app.model.document import Document
from app.model.palettes import PALETTES, palette_by_id
from app.prefs import Preferences, load_prefs, prefs_path, save_prefs
from app.ui.main_window import MainWindow
from app.ui.prefs_dialog import PrefsDialog
from app.ui.widgets import SimpleColorDialog

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
    # `copy_transparent` もダイアログに UI を持たない「素通り」フィールド
    # （レビュー所見対応: `_collect_prefs` は全フィールドを明示列挙するため、
    # ここでカバーしないと将来削っても誰も気づかず、環境設定で OK を押すたびに
    # dataclass 既定 False へ黙って戻る回帰が再発する）。
    prefs = Preferences(
        window_geometry=[1, 2, 3, 4],
        grid_visible=True,
        snap_enabled=False,
        copy_transparent=True,
    )
    dialog = PrefsDialog(prefs)
    dialog.accept()
    edited = dialog.edited_prefs()
    assert edited.window_geometry == [1, 2, 3, 4]
    assert edited.grid_visible is True
    assert edited.snap_enabled is False
    assert edited.copy_transparent is True


def test_background_color_button_updates_edited_prefs(
    qapp: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    """2026-09-25（要望8/11/12）: 背景色ボタンは `ColorSwatchButton` に統一した。
    `.click()` は（メニュー付きボタンなので）メニューを開くだけで `clicked` を
    発火しない。「色を選択…」アクションを直接 trigger する。
    """
    monkeypatch.setattr(SimpleColorDialog, "get_color", staticmethod(lambda *a, **k: "#123456"))
    dialog = PrefsDialog(Preferences())
    pick_action = next(a for a in dialog._bg_button.menu().actions() if a.text() == "色を選択…")
    pick_action.trigger()
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
    # 所見(#11): 2回目のクリックで無変化のコマンドを積ませないよう、
    # 登録後はボタン自体を無効化しておく。
    assert not dialog._register_button.isEnabled()


# --------------------------------------------------------------------------
# フォントコンボのダーティフラグ（所見 S7: 未操作でもフォントが書き換わる）
# --------------------------------------------------------------------------


def test_font_untouched_preserves_original_family_even_if_widget_normalizes(qapp: Any) -> None:
    """フォント欄を一切操作しなければ、`QFontComboBox` が内部的にどう正規化
    していても元の `default_font_family` がそのまま持ち越される。"""
    dialog = PrefsDialog(Preferences(default_font_family="NoSuchFontXYZ"))
    dialog._font_size_spin.setValue(20.0)  # 他フィールドの操作は無関係
    dialog.accept()
    assert dialog.edited_prefs().default_font_family == "NoSuchFontXYZ"


def test_font_touched_marks_dirty_and_adopts_new_family(qapp: Any) -> None:
    """2026-09-25（要望1）: フォント欄は `FontFamilyCombo` に統一した。値の確定は
    `activated`（ユーザー操作）でだけ起きるため、`setCurrentFont` の代わりに
    実際のユーザー操作を模して `activated` を発火させる。
    """
    dialog = PrefsDialog(Preferences(default_font_family="Noto Sans CJK JP"))
    assert dialog._font_dirty is False
    combo = dialog._font_combo
    assert combo.count() > 0
    combo.setCurrentIndex(0)
    combo.activated.emit(0)
    assert dialog._font_dirty is True
    dialog.accept()
    assert dialog.edited_prefs().default_font_family == combo.family()


def test_collect_font_family_strips_foundry_suffix_when_dirty(qapp: Any) -> None:
    """`FontFamilyCombo.family_chosen` は `app/model/fonts.py::strip_foundry_suffix`
    済みの値をユーザー操作（`activated`）でだけ運ぶため、この責務はコンボ自身に
    移った（D1 の `tests/test_widget_font_family_combo.py` が単体で固定する）。
    ここではダイアログ側がその値をそのまま採用するだけであることを固定する。
    """
    dialog = PrefsDialog(Preferences())
    dialog._font_dirty = True
    dialog._font_value = "Nimbus Sans"  # `family_chosen` が運んできた想定の値
    assert dialog._collect_font_family("ignored") == "Nimbus Sans"


def test_collect_font_family_returns_unchanged_value_when_not_dirty(qapp: Any) -> None:
    dialog = PrefsDialog(Preferences())
    assert dialog._collect_font_family("kept-as-is") == "kept-as-is"


# --------------------------------------------------------------------------
# 数式フォント（項目6-wiring契約）
# --------------------------------------------------------------------------


def test_math_fontset_combo_round_trips_through_edited_prefs(qapp: Any) -> None:
    dialog = PrefsDialog(Preferences(math_fontset="cm"))
    idx = dialog._math_fontset_combo.findData("stix")
    assert idx >= 0
    dialog._math_fontset_combo.setCurrentIndex(idx)

    dialog.accept()

    assert dialog.edited_prefs().math_fontset == "stix"


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


def test_property_panel_and_mask_panel_get_the_saved_palette_on_construction(qapp: Any) -> None:
    """2026-09-25（要望8/11/12）: `QColorDialog.setCustomColor`（プロセス全体の
    static state）は廃止し、`PropertyPanel`/`MaskEditPanel` へ直接 `prefs` を
    渡す方式に置き換えた（`MainWindow._apply_palette_swatches` は削除済み）。
    構築時点で両パネルが保存済みのパレットを認識していることを固定する。
    """
    save_prefs(Preferences(palette_id=_MATERIAL.id))
    w = MainWindow()
    try:
        assert w.property_panel._palette is not None
        assert w.property_panel._palette.id == _MATERIAL.id
        assert w.mask_edit_panel._color_button._palette is not None
        assert w.mask_edit_panel._color_button._palette.id == _MATERIAL.id
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
    # `_apply_palette_swatches`（`QColorDialog.setCustomColor`）は廃止済み。
    # 新しいパレットは `refresh_palette()` 経由で両パネルへ直接反映される。
    assert window.property_panel._palette is not None
    assert window.property_panel._palette.id == _MATERIAL.id
    assert window.mask_edit_panel._color_button._palette.id == _MATERIAL.id
    # `self.prefs` は参照を差し替えず中身だけ更新するため、コンストラクタで同じ
    # オブジェクトを受け取った ToolManager/ExportController からも新しい値が
    # 即座に見える（`_update_prefs_in_place` の存在理由そのもの）。
    assert window.tool_manager.prefs is window.prefs
    assert window._export.prefs is window.prefs


def test_open_preferences_applies_math_fontset_without_touching_the_undo_stack(
    qapp: Any, window: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    """数式フォントセットの変更はプロセス全体設定の切り替えであり、モデルを
    一切書き換えない（undo エントリを作らない。項目6-wiring契約）。"""
    from app.math.mathtext_render import current_math_fontset

    assert current_math_fontset() == "cm"
    undo_count_before = window.undo_stack.count()

    class _FakeDialog:
        def __init__(self, prefs: Preferences, on_register_styles: Any = None) -> None:
            self._edited = Preferences(**{**prefs.to_dict(), "math_fontset": "stix"})

        def exec(self) -> int:
            return 1  # QDialog.DialogCode.Accepted

        def edited_prefs(self) -> Preferences:
            return self._edited

    monkeypatch.setattr("app.ui.main_window.PrefsDialog", _FakeDialog)

    window.open_preferences()

    assert current_math_fontset() == "stix"
    assert window.prefs.math_fontset == "stix"
    assert load_prefs().math_fontset == "stix"
    assert window.undo_stack.count() == undo_count_before


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


def test_register_palette_styles_second_call_is_noop(window: Any) -> None:
    """所見(#11): 同じ内容で 2 回登録しても 2 個目の undo ステップは積まない
    （空コマンドを Ctrl+Z すると図が変わらないのに 1 手戻るように見えるため）。"""
    window._register_palette_styles(_MATERIAL)
    count_after_first = window.undo_stack.count()

    window._register_palette_styles(_MATERIAL)

    assert window.undo_stack.count() == count_after_first


def test_open_preferences_clears_style_memory_when_creation_defaults_change(
    qapp: Any, window: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    """所見(#2/S2): rect を 1 個描いた直後に環境設定で初期色/線幅を変えても、
    従来は sticky defaults(style memory)が永久に勝ち続けていた。
    `open_preferences` が作成既定の変化を検知して `clear_style_memory()` を
    呼ぶことで、変更後の最初の生成から新しい既定が効くようにする。

    rect/ellipse は 2026-09-25 決定で stroke の既定が None（線なし）になり、
    `initial_color` の影響を受けなくなった（`reports/rectdefault.md` §3。
    `_apply_pref_defaults` は dataclass 既定が None のフィールドを色で
    埋めない）。この検証テストの主題は「style memory のリセット」であって
    rect 固有の初期色挙動ではないので、`initial_color` が従来どおり効く
    line で検証する。
    """
    from PySide6.QtCore import QPointF, Qt

    class _FakeEvent:
        def button(self) -> Qt.MouseButton:
            return Qt.MouseButton.LeftButton

    tm = window.tool_manager
    tm.set_tool("line")
    tm.handle_mouse_press(_FakeEvent(), QPointF(10, 10))
    tm.handle_mouse_move(_FakeEvent(), QPointF(110, 90))
    tm.handle_mouse_release(_FakeEvent(), QPointF(110, 90))
    first = window.scene.document.objects[0]
    assert first.stroke == "#000000"  # 変更前の既定

    class _FakeDialog:
        def __init__(self, prefs: Preferences, on_register_styles: Any = None) -> None:
            self._edited = Preferences(
                **{**prefs.to_dict(), "initial_color": "#F44336", "default_stroke_width": 8.0}
            )

        def exec(self) -> int:
            return 1

        def edited_prefs(self) -> Preferences:
            return self._edited

    monkeypatch.setattr("app.ui.main_window.PrefsDialog", _FakeDialog)
    window.open_preferences()

    tm.set_tool("line")
    tm.handle_mouse_press(_FakeEvent(), QPointF(200, 200))
    tm.handle_mouse_move(_FakeEvent(), QPointF(260, 240))
    tm.handle_mouse_release(_FakeEvent(), QPointF(260, 240))
    second = window.scene.document.objects[-1]
    assert second.stroke == "#F44336"
    assert second.stroke_width == 8.0


def test_open_preferences_without_default_change_keeps_style_memory(
    qapp: Any, window: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    """作成既定に関わらない設定（自動保存間隔等）だけを変えた場合は、
    育てた sticky defaults を無意味に消さない。"""
    from PySide6.QtCore import QPointF, Qt

    class _FakeEvent:
        def button(self) -> Qt.MouseButton:
            return Qt.MouseButton.LeftButton

    tm = window.tool_manager
    tm.set_tool("rect")
    tm.handle_mouse_press(_FakeEvent(), QPointF(10, 10))
    tm.handle_mouse_move(_FakeEvent(), QPointF(110, 90))
    tm.handle_mouse_release(_FakeEvent(), QPointF(110, 90))
    first = window.scene.document.objects[0]
    window.undo_stack.push(
        SetPropertyCommand(window.scene.document, first, "stroke", "#ABCDEF", first.stroke)
    )

    class _FakeDialog:
        def __init__(self, prefs: Preferences, on_register_styles: Any = None) -> None:
            self._edited = Preferences(**{**prefs.to_dict(), "autosave_interval_s": 5})

        def exec(self) -> int:
            return 1

        def edited_prefs(self) -> Preferences:
            return self._edited

    monkeypatch.setattr("app.ui.main_window.PrefsDialog", _FakeDialog)
    window.open_preferences()

    tm.set_tool("rect")
    tm.handle_mouse_press(_FakeEvent(), QPointF(200, 200))
    tm.handle_mouse_move(_FakeEvent(), QPointF(260, 240))
    tm.handle_mouse_release(_FakeEvent(), QPointF(260, 240))
    second = window.scene.document.objects[-1]
    assert second.stroke == "#ABCDEF"


# --------------------------------------------------------------------------
# ウィンドウジオメトリ復元のクランプ（所見 S6）
# --------------------------------------------------------------------------


def test_oversized_saved_geometry_is_clamped_to_screen(qapp: Any) -> None:
    save_prefs(Preferences(window_geometry=[0, 0, 100000, 100000]))
    w = MainWindow()
    try:
        screen = w.screen() or QGuiApplication.primaryScreen()
        avail = screen.availableGeometry()
        geo = w.geometry()
        assert geo.width() <= avail.width()
        assert geo.height() <= avail.height()
    finally:
        w.close()


def test_tiny_saved_geometry_falls_back_to_default_logic(qapp: Any) -> None:
    save_prefs(Preferences(window_geometry=[10, 10, 1, 1]))
    w = MainWindow()
    try:
        geo = w.geometry()
        assert geo.width() > 1
        assert geo.height() > 1
    finally:
        w.close()


def test_offscreen_titlebar_geometry_falls_back_to_default_logic(qapp: Any) -> None:
    """タイトルバー相当の帯が画面外なら、本体が多少重なっていても復元しない。"""
    save_prefs(Preferences(window_geometry=[-100000, -100000, 800, 600]))
    w = MainWindow()
    try:
        geo = w.geometry()
        assert geo.x() != -100000
        assert geo.y() != -100000
    finally:
        w.close()


# --------------------------------------------------------------------------
# 色ダイアログ: 唯一の経路が SimpleColorDialog であること（要望12）
# --------------------------------------------------------------------------


def test_bg_button_pick_action_opens_the_simple_color_dialog(
    qapp: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    """`grep -rn QColorDialog app/` が 0 件になった（`QColorDialog` はアプリから
    完全に消えた）ことの一角。背景色ボタンの「色を選択…」も
    `SimpleColorDialog.get_color` の1関数だけを通ることを固定する。
    """
    captured: dict[str, Any] = {}

    def _fake_get_color(*args: Any, **kwargs: Any) -> None:
        captured["called"] = True
        return None  # キャンセル相当 → ボタンの色は変えない

    monkeypatch.setattr(SimpleColorDialog, "get_color", staticmethod(_fake_get_color))
    dialog = PrefsDialog(Preferences())
    pick_action = next(a for a in dialog._bg_button.menu().actions() if a.text() == "色を選択…")
    pick_action.trigger()

    assert captured.get("called") is True


def test_qcolordialog_is_not_used_anywhere_in_the_app() -> None:
    """色ダイアログの唯一の経路が `SimpleColorDialog` であること（要望8/11/12）を、
    `app/` 配下のどのファイルにも `QColorDialog`/`setCustomColor` という文字列が
    一切現れないことで固定する（契約 §5 の「`grep -rn QColorDialog app/` が
    0 件」を文字どおり満たす。コメント・docstring 内の「かつて使っていた」という
    説明も、正規表現の抜け道になる import 文の書き方（複数行 import・エイリアス・
    `getattr` 経由の間接参照）を見逃さないためにも、2026-09-25 の doc パスで
    すべて「Qt 標準の色ダイアログ」等の言い換えへ改めた。以前はコメント/docstring
    行をスキップする緩い正規表現だったため、複数行 import やエイリアス経由の
    呼び出しをすり抜けていた——このテストはそれ自体の回帰でもある）。
    """
    import pathlib

    repo_root = pathlib.Path(__file__).resolve().parent.parent
    offenders: list[str] = []
    for path in (repo_root / "app").rglob("*.py"):
        for lineno, line in enumerate(path.read_text(encoding="utf-8").splitlines(), start=1):
            if "QColorDialog" in line or "setCustomColor" in line:
                offenders.append(f"{path}:{lineno}:{line.strip()}")
    assert offenders == [], f"QColorDialog/setCustomColor がまだ言及されている: {offenders}"
