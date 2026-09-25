"""`ColorSwatchButton`（`app/ui/widgets/color_swatch_button.py`）の単体テスト
（P2契約 D1-4・要望8/11）。

ボタン背景を `setStyleSheet` で塗ると子の `QMenu`/ツールチップまで着色されて
読めなくなる、という要望8の根本原因（`reports/color.md` §1）を再発させないこと、
および常設メニューの構成・チェック状態・`color_chosen` の発火条件を固定する。
"""

from __future__ import annotations

from typing import Any

import pytest
from PySide6.QtWidgets import QMenu, QPushButton

from app.model.palettes import Palette, dropdown_colors, palette_by_id
from app.ui.widgets.color_swatch_button import ColorSwatchButton
from app.ui.widgets.simple_color_dialog import SimpleColorDialog


def _menu_actions(button: ColorSwatchButton) -> list[Any]:
    menu = button.menu()
    assert menu is not None
    return [a for a in menu.actions() if not a.isSeparator()]


def test_is_a_qpushbutton_subclass(qapp: Any) -> None:
    button = ColorSwatchButton()
    assert isinstance(button, QPushButton)


def test_never_sets_a_widget_stylesheet(qapp: Any) -> None:
    """要望8の直接原因（背景色を setStyleSheet で塗る）を再発させない。"""
    button = ColorSwatchButton()
    button.set_value("#000000")
    assert button.styleSheet() == ""
    button.set_value(None)
    assert button.styleSheet() == ""
    button.set_mixed(True)
    assert button.styleSheet() == ""


def test_menu_is_readable_with_a_black_value(qapp: Any) -> None:
    """`reports/color.md` §1 の実測再現: 旧実装は色を `QPushButton.setStyleSheet(
    "background-color: #000000;")` で塗っていたため、それを親に持つ `QMenu` と
    ツールチップまで黒一色を継承し（グラブした 1500px 中 1413px が黒）、メニュー
    文字列が読めなくなっていた。`ColorSwatchButton` は `setStyleSheet` を一切
    使わないので、黒を選んでもメニューは黒一色にならないことをピクセルで固定する。
    """
    button = ColorSwatchButton(nullable=True)
    button.set_palette(palette_by_id("tableau10"))
    button.set_value("#000000")
    button.resize(button.sizeHint())
    button.show()
    qapp.processEvents()

    menu = button.menu()
    assert menu is not None
    menu.show()
    qapp.processEvents()

    image = menu.grab().toImage()
    assert image.width() > 0 and image.height() > 0
    total = 0
    near_black = 0
    for x in range(0, image.width(), 2):
        for y in range(0, image.height(), 2):
            color = image.pixelColor(x, y)
            total += 1
            if color.red() < 20 and color.green() < 20 and color.blue() < 20:
                near_black += 1
    assert total > 0
    # 旧実装は 1413/1500（≈94%）が黒だった。ここでは大半（8割超）が黒でないことを
    # 要求する（アイコン/テキストのごく一部が黒くても構わない）。
    assert near_black / total < 0.2, f"menu is mostly black ({near_black}/{total})"
    menu.close()
    button.close()


def test_menu_is_always_attached_even_when_not_nullable(qapp: Any) -> None:
    button = ColorSwatchButton(nullable=False)
    assert isinstance(button.menu(), QMenu)


def test_menu_lists_palette_colors_then_pick_action_when_not_nullable(qapp: Any) -> None:
    palette = palette_by_id("tableau10")
    button = ColorSwatchButton(nullable=False)
    button.set_palette(palette)
    actions = _menu_actions(button)
    texts = [a.text() for a in actions]
    assert texts == [*dropdown_colors(palette), "色を選択…"]
    assert all(a.isCheckable() for a in actions[:-1])
    assert actions[-1].isCheckable() is False


def test_menu_adds_null_action_only_when_nullable(qapp: Any) -> None:
    button = ColorSwatchButton(nullable=True, null_label="なし")
    actions = _menu_actions(button)
    texts = [a.text() for a in actions]
    assert "なし" in texts
    assert texts[-1] == "色を選択…"
    assert texts[-2] == "なし"


def test_null_label_is_customizable_for_mask_semantics(qapp: Any) -> None:
    button = ColorSwatchButton(nullable=True, null_label="透明（切り取り）")
    texts = [a.text() for a in _menu_actions(button)]
    assert "透明（切り取り）" in texts
    assert "なし" not in texts


def test_checked_action_matches_current_value_case_insensitively(qapp: Any) -> None:
    palette = palette_by_id("tableau10")
    button = ColorSwatchButton(nullable=True)
    button.set_palette(palette)
    button.set_value(palette.colors[2].lower())

    checked = [a for a in _menu_actions(button) if a.isCheckable() and a.isChecked()]
    assert len(checked) == 1
    assert checked[0].text() == palette.colors[2]


def test_null_action_checked_when_value_is_none(qapp: Any) -> None:
    button = ColorSwatchButton(nullable=True, null_label="なし")
    button.set_value(None)
    null_action = next(a for a in _menu_actions(button) if a.text() == "なし")
    assert null_action.isChecked()


def test_nothing_checked_when_mixed(qapp: Any) -> None:
    button = ColorSwatchButton(nullable=True)
    button.set_palette(palette_by_id("tableau10"))
    button.set_value(palette_by_id("tableau10").colors[0])
    button.set_mixed(True)
    assert all(not a.isChecked() for a in _menu_actions(button) if a.isCheckable())


def test_set_value_and_value_roundtrip_uppercased(qapp: Any) -> None:
    button = ColorSwatchButton()
    button.set_value("#abcdef")
    assert button.value() == "#ABCDEF"
    assert button.text() == "#ABCDEF"


def test_set_value_none(qapp: Any) -> None:
    button = ColorSwatchButton(nullable=True, null_label="なし")
    button.set_value(None)
    assert button.value() is None
    assert button.text() == "なし"


def test_set_value_and_set_mixed_do_not_emit_color_chosen(qapp: Any) -> None:
    button = ColorSwatchButton(nullable=True)
    chosen: list[Any] = []
    button.color_chosen.connect(chosen.append)
    button.set_value("#123456")
    button.set_value(None)
    button.set_mixed(True)
    button.set_mixed(False)
    assert chosen == []


def test_choosing_a_palette_action_emits_color_chosen(qapp: Any) -> None:
    palette = palette_by_id("tableau10")
    button = ColorSwatchButton()
    button.set_palette(palette)
    chosen: list[Any] = []
    button.color_chosen.connect(chosen.append)

    action = next(a for a in _menu_actions(button) if a.text() == palette.colors[3])
    action.trigger()

    assert chosen == [palette.colors[3]]
    assert button.value() == palette.colors[3]


def test_choosing_the_same_color_again_does_not_emit(qapp: Any) -> None:
    palette = palette_by_id("tableau10")
    button = ColorSwatchButton()
    button.set_palette(palette)
    button.set_value(palette.colors[0])
    chosen: list[Any] = []
    button.color_chosen.connect(chosen.append)

    action = next(a for a in _menu_actions(button) if a.text() == palette.colors[0])
    action.trigger()

    assert chosen == []


def test_choosing_the_same_color_case_insensitively_does_not_emit(qapp: Any) -> None:
    button = ColorSwatchButton()
    button.set_value("#4e79a7")
    chosen: list[Any] = []
    button.color_chosen.connect(chosen.append)
    button._choose("#4E79A7")
    assert chosen == []


def test_choosing_null_action_emits_none(qapp: Any) -> None:
    button = ColorSwatchButton(nullable=True, null_label="なし")
    button.set_value("#000000")
    chosen: list[Any] = []
    button.color_chosen.connect(chosen.append)

    null_action = next(a for a in _menu_actions(button) if a.text() == "なし")
    null_action.trigger()

    assert chosen == [None]
    assert button.value() is None


def test_pick_action_opens_simple_color_dialog_with_current_value_and_palette(
    qapp: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    captured: dict[str, Any] = {}

    def fake_get_color(
        initial: str | None, palette: Palette | None, parent: Any = None, title: str = "色を選択"
    ) -> str | None:
        captured["initial"] = initial
        captured["palette"] = palette
        captured["title"] = title
        return "#334455"

    monkeypatch.setattr(SimpleColorDialog, "get_color", staticmethod(fake_get_color))

    palette = palette_by_id("apple")
    button = ColorSwatchButton(dialog_title="線色を選択")
    button.set_palette(palette)
    button.set_value("#000000")
    chosen: list[Any] = []
    button.color_chosen.connect(chosen.append)

    pick_action = next(a for a in _menu_actions(button) if a.text() == "色を選択…")
    pick_action.trigger()

    assert captured["initial"] == "#000000"
    assert captured["palette"] is palette
    assert captured["title"] == "線色を選択"
    assert chosen == ["#334455"]
    assert button.value() == "#334455"


def test_pick_action_cancelled_leaves_value_unchanged(
    qapp: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(SimpleColorDialog, "get_color", staticmethod(lambda *a, **k: None))
    button = ColorSwatchButton()
    button.set_value("#000000")
    chosen: list[Any] = []
    button.color_chosen.connect(chosen.append)

    pick_action = next(a for a in _menu_actions(button) if a.text() == "色を選択…")
    pick_action.trigger()

    assert chosen == []
    assert button.value() == "#000000"


def test_set_palette_rebuilds_menu_with_new_colors(qapp: Any) -> None:
    button = ColorSwatchButton()
    button.set_palette(palette_by_id("material"))
    first_texts = [a.text() for a in _menu_actions(button)]
    button.set_palette(palette_by_id("seaborn_deep"))
    second_texts = [a.text() for a in _menu_actions(button)]
    assert first_texts != second_texts
    assert second_texts[:8] == list(palette_by_id("seaborn_deep").colors)


def test_size_hint_is_independent_of_color_value(qapp: Any) -> None:
    button = ColorSwatchButton(nullable=True, null_label="透明（切り取り）")
    hint_black = button.sizeHint()
    button.set_value("#000000")
    hint_after_black = button.sizeHint()
    button.set_value("#FFFFFF")
    hint_after_white = button.sizeHint()
    button.set_value(None)
    hint_after_null = button.sizeHint()
    button.set_mixed(True)
    hint_after_mixed = button.sizeHint()

    assert hint_black == hint_after_black == hint_after_white == hint_after_null == hint_after_mixed


def test_size_hint_can_differ_between_two_button_instances_with_different_null_labels(
    qapp: Any,
) -> None:
    """sizeHint は「その瞬間の値」には依存しないが、コンストラクタで固定される
    null_label（例: マスクの「透明（切り取り）」）には依存してよい。"""
    short = ColorSwatchButton(nullable=True, null_label="なし")
    long = ColorSwatchButton(nullable=True, null_label="透明（切り取り）")
    assert long.sizeHint().width() >= short.sizeHint().width()
