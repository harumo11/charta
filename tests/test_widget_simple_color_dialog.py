"""`SimpleColorDialog`（`app/ui/widgets/simple_color_dialog.py`）の単体テスト
（P2契約 D1-3・要望12）。

スペクトル/明度ピッカー・HSV/RGB スピン・「カスタム色に追加」を持たない、
パレット8色・基本色8色・HEX 入力だけの簡素なダイアログであることを固定する。
"""

from __future__ import annotations

from typing import Any

import pytest
from PySide6.QtWidgets import QDialog, QDialogButtonBox

from app.model.palettes import BASIC_COLORS, Palette, palette_by_id
from app.ui.widgets.simple_color_dialog import SimpleColorDialog, _normalize_hex

# --------------------------------------------------------------------------
# _normalize_hex
# --------------------------------------------------------------------------


@pytest.mark.parametrize(
    "text,expected",
    [
        ("ff0000", "#FF0000"),
        ("#ff0000", "#FF0000"),
        ("AbCdEf", "#ABCDEF"),
        ("  #123456  ", "#123456"),
    ],
)
def test_normalize_hex_accepts_valid_values(text: str, expected: str) -> None:
    assert _normalize_hex(text) == expected


@pytest.mark.parametrize("text", ["", "zzzzzz", "#fff", "12345", "#1234567", "red"])
def test_normalize_hex_rejects_invalid_values(text: str) -> None:
    assert _normalize_hex(text) is None


# --------------------------------------------------------------------------
# ダイアログの構成
# --------------------------------------------------------------------------


def test_dialog_has_no_ok_cancel_layout_with_palette_and_basic_rows(qapp: Any) -> None:
    palette = palette_by_id("tableau10")
    assert palette is not None
    dialog = SimpleColorDialog(None, palette)
    # パレット8 + 基本色8 = 16 スウォッチ（+ プレビューは別枠で self._preview）。
    assert len(dialog._swatches) == 16
    assert dialog._buttons.button(QDialogButtonBox.StandardButton.Ok) is not None
    assert dialog._buttons.button(QDialogButtonBox.StandardButton.Cancel) is not None


def test_palette_row_is_omitted_when_palette_is_none(qapp: Any) -> None:
    dialog = SimpleColorDialog(None, None)
    # パレット行が無いので、スウォッチは基本色8個だけ。
    assert len(dialog._swatches) == 8
    assert [s.color() for s in dialog._swatches] == list(BASIC_COLORS)


def test_dialog_never_uses_qcolordialog() -> None:
    """`QColorDialog` を import/参照していないこと（コメント中の言及は許す）。"""
    import ast
    import inspect

    from app.ui.widgets import simple_color_dialog

    source = inspect.getsource(simple_color_dialog)
    tree = ast.parse(source)
    imported_names: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom):
            imported_names.update(alias.asname or alias.name for alias in node.names)
        elif isinstance(node, ast.Import):
            imported_names.update(alias.asname or alias.name for alias in node.names)
    assert "QColorDialog" not in imported_names
    assert not hasattr(simple_color_dialog, "QColorDialog")


def test_dialog_has_no_spectrum_or_hsv_widgets(qapp: Any) -> None:
    from PySide6.QtWidgets import QSlider, QSpinBox

    dialog = SimpleColorDialog(None, palette_by_id("apple"))
    assert dialog.findChildren(QSlider) == []
    assert dialog.findChildren(QSpinBox) == []


# --------------------------------------------------------------------------
# 初期値・OK の有効/無効
# --------------------------------------------------------------------------


def test_initial_value_populates_hex_field(qapp: Any) -> None:
    dialog = SimpleColorDialog("#4e79a7", None)
    assert dialog._hex_edit.text() == "#4E79A7"
    ok_button = dialog._buttons.button(QDialogButtonBox.StandardButton.Ok)
    assert ok_button.isEnabled()


def test_none_initial_value_leaves_hex_empty_and_ok_disabled(qapp: Any) -> None:
    dialog = SimpleColorDialog(None, None)
    assert dialog._hex_edit.text() == ""
    ok_button = dialog._buttons.button(QDialogButtonBox.StandardButton.Ok)
    assert not ok_button.isEnabled()


def test_ok_disabled_while_hex_input_is_invalid(qapp: Any) -> None:
    dialog = SimpleColorDialog(None, None)
    ok_button = dialog._buttons.button(QDialogButtonBox.StandardButton.Ok)
    dialog._hex_edit.setText("zzzzzz")
    assert not ok_button.isEnabled()
    dialog._hex_edit.setText("112233")
    assert ok_button.isEnabled()


# --------------------------------------------------------------------------
# スウォッチのクリック / ダブルクリック
# --------------------------------------------------------------------------


def test_clicking_a_swatch_updates_hex_field_and_checks_it(qapp: Any) -> None:
    dialog = SimpleColorDialog(None, None)
    swatch = dialog._swatches[4]  # #FF0000
    swatch.click()
    assert dialog._hex_edit.text() == swatch.color()
    assert swatch.isChecked()


def test_double_clicking_a_swatch_accepts_the_dialog(qapp: Any) -> None:
    dialog = SimpleColorDialog(None, None)
    swatch = dialog._swatches[0]
    dialog._on_swatch_double_clicked(swatch.color())
    assert dialog.result() == QDialog.DialogCode.Accepted
    assert dialog._result_color == swatch.color()


def test_reclicking_the_already_selected_swatch_keeps_it_checked(qapp: Any) -> None:
    """finding #5/#13: `_Swatch` は checkable なので、素の QAbstractButton なら
    クリック自体がチェック状態を自分でトグルしてしまう。同じ色を再選択しても
    テキストが変わらず textChanged が発火しないため、それに頼る
    `_on_hex_changed` の再同期も起きず、選択中なのに枠が消える。"""
    dialog = SimpleColorDialog("#000000", None)
    swatch = dialog._swatches[0]  # BASIC_COLORS[0] == "#000000"
    assert swatch.isChecked()

    swatch.click()
    assert swatch.isChecked(), "選択中の色を再クリックしても選択枠が消えてはいけない"
    assert dialog._hex_edit.text() == "#000000"

    swatch.click()
    assert swatch.isChecked(), "何度クリックしても交互にトグルしないこと"


def test_reclicking_selected_swatch_keeps_it_checked_with_no_initial_value(qapp: Any) -> None:
    dialog = SimpleColorDialog(None, None)
    red = dialog._swatches[4]  # BASIC_COLORS[4] == "#FF0000"
    red.click()
    assert red.isChecked()

    red.click()
    assert red.isChecked()
    assert dialog._hex_edit.text() == "#FF0000"


def test_typing_hex_checks_the_matching_swatch(qapp: Any) -> None:
    dialog = SimpleColorDialog(None, None)
    dialog._hex_edit.setText(BASIC_COLORS[2].lstrip("#"))
    checked = [s for s in dialog._swatches if s.isChecked()]
    assert len(checked) == 1
    assert checked[0].color() == BASIC_COLORS[2]


# --------------------------------------------------------------------------
# 静的な差し替え口 get_color
# --------------------------------------------------------------------------


def test_get_color_returns_uppercase_hex_on_accept(
    qapp: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    def fake_exec(self: SimpleColorDialog) -> int:
        self._hex_edit.setText("abcdef")
        return QDialog.DialogCode.Accepted

    monkeypatch.setattr(SimpleColorDialog, "exec", fake_exec)
    result = SimpleColorDialog.get_color(None, None)
    assert result == "#ABCDEF"


def test_get_color_returns_none_on_reject(qapp: Any, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(SimpleColorDialog, "exec", lambda self: QDialog.DialogCode.Rejected)
    result = SimpleColorDialog.get_color("#000000", None)
    assert result is None


def test_get_color_accepts_a_palette_argument(qapp: Any, monkeypatch: pytest.MonkeyPatch) -> None:
    captured: dict[str, Any] = {}

    original_init = SimpleColorDialog.__init__

    def spy_init(
        self: SimpleColorDialog,
        initial: str | None,
        palette: Palette | None,
        parent: Any = None,
        title: str = "色を選択",
    ) -> None:
        captured["palette"] = palette
        original_init(self, initial, palette, parent, title)

    monkeypatch.setattr(SimpleColorDialog, "__init__", spy_init)
    monkeypatch.setattr(SimpleColorDialog, "exec", lambda self: QDialog.DialogCode.Rejected)
    palette = palette_by_id("kusumi")
    SimpleColorDialog.get_color(None, palette)
    assert captured["palette"] is palette
