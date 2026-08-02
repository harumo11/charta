"""qtawesome アイコンのラッパー（契約 §「新設モジュール契約」）。

色はテーマトークン（`current_theme()`）から取り、ハードコードしない。チェック可能な
ツールボタンはチェック時にアクセント色へ切り替える必要があるが、それは QSS の疑似
状態では表現できない（QIcon 側の色は QSS が触れない）ため、qtawesome の
`color_on`/`color_off` 引数（QIcon.State 対応）で表現する。
"""

from __future__ import annotations

import qtawesome as qta
from PySide6.QtGui import QIcon

from app.ui.theme.tokens import current_theme

#: ツール名 → qtawesome アイコン名の対応表（契約 §「新設モジュール契約」）。
TOOL_ICONS: dict[str, str] = {
    "select": "mdi6.cursor-default-outline",
    "rect": "mdi6.rectangle-outline",
    "ellipse": "mdi6.ellipse-outline",
    "line": "mdi6.vector-line",
    "arrow": "mdi6.arrow-top-right",
    "freehand": "mdi6.draw",
    "text": "mdi6.format-text",
    "math": "mdi6.sigma",
    "connector": "mdi6.transit-connection-variant",
}

#: オブジェクト型 → qtawesome アイコン名の対応表（P2契約 §4、レイヤーパネルの型アイコン用）。
#: `image` を除く8種は `TOOL_ICONS` と同じ意匠（対応する作成ツールと見た目を揃える）。
OBJECT_ICONS: dict[str, str] = {
    "rect": TOOL_ICONS["rect"],
    "ellipse": TOOL_ICONS["ellipse"],
    "line": TOOL_ICONS["line"],
    "arrow": TOOL_ICONS["arrow"],
    "freehand": TOOL_ICONS["freehand"],
    "text": TOOL_ICONS["text"],
    "math": TOOL_ICONS["math"],
    "connector": TOOL_ICONS["connector"],
    "image": "mdi6.image-outline",
}


def icon(name: str, *, color: str | None = None) -> QIcon:
    """通常アイコン（トグルしない、undo/redo/取り込み/書き出し等）を返す。"""
    t = current_theme()
    return qta.icon(
        name,
        color=color or t.fg_muted,
        color_active=t.fg,
        color_disabled=t.fg_disabled,
    )


def icon_checkable(name: str) -> QIcon:
    """チェック可能なツールボタン用アイコン。チェック時はアクセント色になる。

    `color_on_active`/`color_on_selected` を指定しないと、チェック済み(on)の
    ボタンにマウスホバー(active)しただけでアクセント色が `color_active`(fg)へ
    戻ってしまう（qtawesome は Mode×State の組で色を引くため、on×active の
    組が未指定だと active 側の色にフォールバックする）。
    """
    t = current_theme()
    return qta.icon(
        name,
        color=t.fg_muted,
        color_active=t.fg,
        color_disabled=t.fg_disabled,
        color_on=t.accent,
        color_off=t.fg_muted,
        color_on_active=t.accent,
        color_on_selected=t.accent,
    )


def icon_pair(on_name: str, off_name: str) -> QIcon:
    """On/Off でグリフ自体が異なる 1 個の `QIcon` を返す（レイヤーパネルの表示/ロックボタン用）。

    `icon_checkable()` の `color_on`/`color_off` は同一グリフの色だけを切り替えるが、
    こちらは qtawesome の `options=[{"off": ...}]`（`_parse_options` が対応する
    `on`/`off` の各アイコン名を個別に解決する）で状態ごとに異なるアイコン名（形）を
    割り当てる。`QToolButton` は checkable なら `isChecked()` に応じて自動的に
    `QIcon.State.On`/`Off` を選んで描画するため、呼び出し側は状態を意識しなくてよい。
    """
    t = current_theme()
    return qta.icon(
        on_name,
        options=[
            {
                "off": off_name,
                "color": t.fg_muted,
                "color_on": t.fg_muted,
                "color_off": t.fg_muted,
                "color_active": t.fg,
                "color_disabled": t.fg_disabled,
            }
        ],
    )
