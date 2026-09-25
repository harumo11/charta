"""`app/model/fonts.py` の単体テスト（Qt 非依存、P2契約 D1-1）。

`FontFamilyCombo`（`app/ui/widgets/font_family_combo.py`）と
`prefs_dialog._collect_font_family` の双方が使う共有ヘルパを、Qt を起動せずに
純粋な文字列処理として固定する。
"""

from __future__ import annotations

from app.model.fonts import strip_foundry_suffix, unique_families


def test_strip_foundry_suffix_removes_trailing_bracket() -> None:
    assert strip_foundry_suffix("Nimbus Sans [urw]") == "Nimbus Sans"
    assert strip_foundry_suffix("C059 [UKWN]") == "C059"
    # 括弧内・前の空白の有無を問わない。
    assert strip_foundry_suffix("X [URW ]") == "X"


def test_strip_foundry_suffix_leaves_plain_names_unchanged() -> None:
    assert strip_foundry_suffix("Noto Sans CJK JP") == "Noto Sans CJK JP"
    assert strip_foundry_suffix("Arial") == "Arial"


def test_strip_foundry_suffix_only_strips_trailing_bracket() -> None:
    # 末尾以外の括弧（フォント名自体の一部）は接尾辞ではないので残す。
    assert strip_foundry_suffix("Font (Display) Name") == "Font (Display) Name"


def test_unique_families_strips_dedupes_and_sorts_casefold() -> None:
    names = [
        "Nimbus Sans [urw]",
        "nimbus sans",
        "Arial",
        "",
        "  ",
        "arial",
        "Zeta",
        "Beta",
    ]
    result = unique_families(names)
    # 空文字/空白のみは除外、接尾辞は剥がしたうえで casefold 重複除去（最初を残す）、
    # casefold で整列。
    assert result == ["Arial", "Beta", "Nimbus Sans", "Zeta"]


def test_unique_families_keeps_first_occurrence_casing() -> None:
    result = unique_families(["Noto Sans", "NOTO SANS", "noto sans"])
    assert result == ["Noto Sans"]


def test_unique_families_empty_input_returns_empty_list() -> None:
    assert unique_families([]) == []
