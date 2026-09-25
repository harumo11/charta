"""フォント名の正規化（Qt 非依存・model 層）。

`QFontDatabase.families()` が返す名前には、同じ書体の複数の実装を区別するための
ファウンドリ接尾辞（例 `"Nimbus Sans [urw]"`, `"C059 [UKWN]"`）が付くことがある。
プロパティパネルのフォントドロップダウン（`app/ui/widgets/font_family_combo.py`）と
環境設定ダイアログ（`prefs_dialog._collect_font_family`）は、この接尾辞を剥がした
素の名前をモデル（`TextObject.font_family` 等）に保存する。保存値に接尾辞が付いた
ままだと、SVG 書き出しがそれを CSS の `font-family` へそのまま書き出してしまい
（`svg_exporter.py`）、有効な CSS 識別子でなくなって画面/SVG のパリティが崩れる
（`reports/panel.md` §1 で実測）。
"""

from __future__ import annotations

import re
from collections.abc import Iterable

# 末尾の " [xxx]" 形式（大文字小文字・空白は問わない）を接尾辞とみなす。
_FOUNDRY_SUFFIX_RE = re.compile(r"\s*\[[^\[\]]*\]\s*$")


def strip_foundry_suffix(name: str) -> str:
    """末尾のファウンドリ接尾辞（例 `" [urw]"`）を除去し、前後の空白を詰める。"""
    return _FOUNDRY_SUFFIX_RE.sub("", name).strip()


def unique_families(names: Iterable[str]) -> list[str]:
    """フォントファミリ名の一覧を、パネル表示用に正規化する。

    各名前を `strip_foundry_suffix` してから、空文字を除き、大文字小文字を無視
    （casefold）した重複を除去する（最初に出てきたものを残す）。最後に casefold
    キーで整列した一覧を返す。
    """
    seen: set[str] = set()
    result: list[str] = []
    for raw in names:
        stripped = strip_foundry_suffix(raw)
        if not stripped:
            continue
        key = stripped.casefold()
        if key in seen:
            continue
        seen.add(key)
        result.append(stripped)
    result.sort(key=str.casefold)
    return result
