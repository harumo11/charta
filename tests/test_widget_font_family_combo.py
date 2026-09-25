"""`FontFamilyCombo`（`app/ui/widgets/font_family_combo.py`）の単体テスト（P2契約 D1-5・要望1）。

`QFontComboBox` は `setCurrentFont` が未インストールのフォントを別のフォントへ
黙って置き換えるため使わない（`reports/panel.md` §1）。ここでは
「値をそのまま持ち越す・活性化操作でだけコミットする」という契約を固定する。
"""

from __future__ import annotations

from typing import Any

import pytest
from PySide6.QtGui import QFontDatabase
from PySide6.QtWidgets import QListView

from app.model.fonts import unique_families
from app.ui.widgets import font_family_combo as font_family_combo_module
from app.ui.widgets.font_family_combo import FontFamilyCombo


def test_items_are_deduped_stripped_and_sorted(qapp: Any) -> None:
    combo = FontFamilyCombo()
    expected = unique_families(QFontDatabase.families())
    actual = [combo.itemText(i) for i in range(combo.count())]
    assert actual == expected


def test_item_user_data_is_the_plain_family_name(qapp: Any) -> None:
    combo = FontFamilyCombo()
    for i in range(min(combo.count(), 20)):
        # 一覧はすでに strip_foundry_suffix 済みなので userData もテキストと一致する。
        assert combo.itemData(i) == combo.itemText(i)


def test_set_family_selects_exact_case_insensitive_match(qapp: Any) -> None:
    combo = FontFamilyCombo()
    installed = combo.itemText(0)
    combo.set_family(installed.upper())
    assert combo.family() == installed
    assert combo.currentText() == installed


def test_set_family_with_uninstalled_name_inserts_temporary_item(qapp: Any) -> None:
    combo = FontFamilyCombo()
    before_count = combo.count()
    combo.set_family("TotallyFakeFontXYZ")
    assert combo.count() == before_count + 1
    assert combo.family() == "TotallyFakeFontXYZ"
    assert combo.currentText() == "TotallyFakeFontXYZ（未インストール）"


def test_set_family_does_not_substitute_a_different_installed_font(qapp: Any) -> None:
    """`QFontComboBox` の既知の欠陥（Helvetica→Nimbus Sans 等への黙った置換）を
    このコンボが再現しないこと。"""
    combo = FontFamilyCombo()
    combo.set_family("Helvetica")
    assert combo.family() == "Helvetica"


def test_set_family_replaces_previous_temporary_item_instead_of_accumulating(qapp: Any) -> None:
    combo = FontFamilyCombo()
    before_count = combo.count()
    combo.set_family("FakeOne")
    combo.set_family("FakeTwo")
    assert combo.count() == before_count + 1
    assert combo.family() == "FakeTwo"


def test_set_family_none_is_mixed_state(qapp: Any) -> None:
    combo = FontFamilyCombo()
    combo.set_family(combo.itemText(0))
    combo.set_family(None)
    assert combo.currentIndex() == -1
    assert combo.family() is None
    assert combo.placeholderText() == "混在"


def test_set_family_does_not_emit_family_chosen(qapp: Any) -> None:
    combo = FontFamilyCombo()
    chosen: list[str] = []
    combo.family_chosen.connect(chosen.append)
    combo.set_family(combo.itemText(0))
    combo.set_family("Uninstalled Name")
    combo.set_family(None)
    assert chosen == []


def test_family_chosen_emits_only_on_activated_with_stripped_value(qapp: Any) -> None:
    combo = FontFamilyCombo()
    chosen: list[str] = []
    combo.family_chosen.connect(chosen.append)

    index = combo.findText(combo.itemText(0))
    combo.activated.emit(index)

    assert chosen == [combo.itemText(0)]


def test_family_chosen_strips_foundry_suffix_from_the_stored_value(qapp: Any) -> None:
    combo = FontFamilyCombo()
    chosen: list[str] = []
    combo.family_chosen.connect(chosen.append)
    # 未インストールの一時項目に人為的にファウンドリ接尾辞を持たせて確認する。
    combo.insertItem(0, "Weird [xyz]", userData="Weird [xyz]")
    combo.setCurrentIndex(0)
    combo.activated.emit(0)
    assert chosen == ["Weird"]


def test_is_not_editable(qapp: Any) -> None:
    combo = FontFamilyCombo()
    assert combo.isEditable() is False


def test_minimum_contents_length_is_clamped_to_keep_the_panel_narrow(qapp: Any) -> None:
    combo = FontFamilyCombo()
    assert combo.minimumContentsLength() == 8


# --------------------------------------------------------------------------
# レビュー finding #3: ファウンドリ接尾辞つきのインストール済みフォント名を
# set_family に渡しても「未インストール」扱いにならないこと。
# --------------------------------------------------------------------------


def test_set_family_matches_installed_family_carrying_foundry_suffix(qapp: Any) -> None:
    from app.model.fonts import strip_foundry_suffix

    combo = FontFamilyCombo()
    families = unique_families(QFontDatabase.families())
    suffixed = next((f for f in QFontDatabase.families() if strip_foundry_suffix(f) != f), None)
    if suffixed is None:
        # このマシンにファウンドリ接尾辞つきフォントが1つも無ければ合成する。
        suffixed = f"{families[0]} [xyz]"
    before_count = combo.count()

    combo.set_family(suffixed)

    assert "未インストール" not in combo.currentText()
    assert combo.currentText() == strip_foundry_suffix(suffixed)
    assert combo.count() == before_count, "接尾辞違いの一時項目を挿入してはいけない"


def test_set_family_with_foundry_suffix_emits_no_signal(qapp: Any) -> None:
    combo = FontFamilyCombo()
    chosen: list[str] = []
    combo.family_chosen.connect(chosen.append)
    combo.set_family("C059 [UKWN]")
    assert chosen == []


# --------------------------------------------------------------------------
# レビュー finding #4: フォーカスを得た後のホイールでフォントが変わらないこと
# （最大560件のリストを1段ずつ回すのは事故になる。wheel_guard 側は
# tests/test_widget_wheel_guard.py で forward_when_focused を直接固定する）。
# --------------------------------------------------------------------------


def test_wheel_never_changes_the_current_family_even_when_focused(qapp: Any) -> None:
    from PySide6.QtCore import QPoint, QPointF, Qt
    from PySide6.QtGui import QWheelEvent
    from PySide6.QtWidgets import QApplication, QVBoxLayout, QWidget

    host = QWidget()
    layout = QVBoxLayout(host)
    combo = FontFamilyCombo(host)
    layout.addWidget(combo)
    host.show()
    qapp.processEvents()
    combo.set_family(combo.itemText(2))
    combo.setFocus()
    qapp.processEvents()
    assert combo.hasFocus()
    before = combo.currentText()

    event = QWheelEvent(
        QPointF(5, 5),
        QPointF(5, 5),
        QPoint(0, 0),
        QPoint(0, -120),
        Qt.MouseButton.NoButton,
        Qt.KeyboardModifier.NoModifier,
        Qt.ScrollPhase.NoScrollPhase,
        False,
    )
    for _ in range(5):
        QApplication.sendEvent(combo, event)

    assert combo.currentText() == before, "フォーカスがあってもホイールでフォントが変わらないこと"
    host.close()


# --------------------------------------------------------------------------
# レビュー finding #8/#9: 一覧の行高が全項目で同じであること（symbol系フォントの
# メトリクスに引きずられて行が伸び縮みしない）、かつ symbol 系フォントの名前が
# 読める（そのフォント自身の書体ではなく UI フォントで描かれる）こと。
# --------------------------------------------------------------------------


def test_view_uses_uniform_item_sizes(qapp: Any) -> None:
    combo = FontFamilyCombo()
    view = combo.view()
    assert isinstance(view, QListView)
    assert view.uniformItemSizes() is True


def test_all_rows_report_the_same_size_hint(qapp: Any) -> None:
    combo = FontFamilyCombo()
    view = combo.view()
    model = combo.model()
    heights = {view.sizeHintForIndex(model.index(i, 0)).height() for i in range(combo.count())}
    assert len(heights) == 1, f"行高が揃っていない: {heights}"


class _FakeFontDatabase:
    """symbol系フォントを模した最小限の `QFontDatabase` 代替（テスト専用）。"""

    WritingSystem = QFontDatabase.WritingSystem

    @staticmethod
    def families() -> list[str]:
        return ["DejaVu Sans", "cmex10"]

    @staticmethod
    def writingSystems(name: str) -> list[Any]:
        if name == "cmex10":
            return [QFontDatabase.WritingSystem.Symbol]
        return [QFontDatabase.WritingSystem.Latin]


def test_symbol_font_name_is_not_drawn_in_its_own_unreadable_face(
    qapp: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(font_family_combo_module, "QFontDatabase", _FakeFontDatabase)
    combo = FontFamilyCombo()
    index = combo.findText("cmex10")
    assert index != -1
    delegate = combo.itemDelegate()
    model_index = combo.model().index(index, 0)

    name_font = delegate._name_font_for(model_index)

    assert name_font.family() != "cmex10", "フォント名は常に UI フォントで描くこと"
