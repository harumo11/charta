"""テーマトークン（契約 §「新設モジュール契約」）。PySide6 を import しない純データ。

配色は Paper Light モックアップ準拠。`Theme` は QPalette/QSS/qtawesome いずれの層からも
参照される単一の真実源であり、色コード直書きは避けここを経由すること。
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class Theme:
    """1 テーマ分の配色・寸法トークン。"""

    name: str  # "light"
    s0: str
    s1: str
    s2: str
    viewport: str
    border: str
    border_strong: str
    fg: str
    fg_muted: str
    fg_disabled: str
    accent: str
    accent_hover: str
    accent_bg: str
    on_accent: str
    danger: str
    success: str
    artboard_border: str
    hover_bg: str
    radius: int = 6
    radius_sm: int = 4


LIGHT: Theme = Theme(
    name="light",
    s0="#F2F3F5",
    s1="#F7F8F9",
    s2="#FFFFFF",
    viewport="#E9EBEE",
    border="#E2E4E8",
    border_strong="#D5D8DC",
    fg="#25282C",
    fg_muted="#71767C",
    fg_disabled="#A5AAB0",
    accent="#3667C9",
    accent_hover="#2F5CB5",
    accent_bg="rgba(54, 103, 201, 28)",
    on_accent="#FFFFFF",
    danger="#B3453F",
    success="#2F9E5F",
    artboard_border="#DCDFE3",
    hover_bg="rgba(0, 0, 0, 11)",
    radius=6,
    radius_sm=4,
)

# 現在適用中のテーマ（既定 LIGHT）。`apply_theme` が `_set_current_theme` 経由で差し替える。
# 将来のダークテーマ追加時もモジュールレベルの単純な変数保持で足りる（GUI スレッド専用・
# マルチテーマ同時表示は想定しない）。
_current: Theme = LIGHT


def current_theme() -> Theme:
    """現在適用中のテーマを返す（既定 LIGHT）。"""
    return _current


def _set_current_theme(theme: Theme) -> None:
    """`apply_theme` からのみ呼ばれる内部 setter。"""
    global _current
    _current = theme
