"""QSS ビルダー（契約 §「新設モジュール契約」）。PySide6 を import しない・文字列生成のみ。

`build_qss(theme)` はアプリ全体に適用する 1 枚の QSS 文字列を返す。状態表現は QSS の
動的プロパティ（`:checked` 等の疑似状態）に任せ、`setStyleSheet` の頻繁な呼び出しは
しない（呼び出し側である `apply_theme` が起動時に 1 度だけ適用する）。
"""

from __future__ import annotations

from app.ui.theme.tokens import Theme


def build_qss(t: Theme) -> str:
    """テーマトークンから QSS 文字列を生成する。"""
    return f"""
/* ---- QMenuBar ---------------------------------------------------- */
QMenuBar {{
    background: {t.s1};
    border: none;
    padding: 0px;
}}
QMenuBar::item {{
    background: transparent;
    padding: 3px 10px;
    border-radius: {t.radius_sm}px;
}}
QMenuBar::item:selected {{
    background: {t.hover_bg};
}}

/* ---- QMenu --------------------------------------------------------- */
QMenu {{
    background: {t.s2};
    border: 1px solid {t.border};
    border-radius: {t.radius}px;
    padding: 4px;
}}
QMenu::item {{
    padding: 4px 24px 4px 12px;
    border-radius: {t.radius_sm}px;
}}
QMenu::item:selected {{
    background: {t.accent_bg};
    color: {t.fg};
}}
QMenu::separator {{
    height: 1px;
    background: {t.border};
    margin: 4px 8px;
}}

/* ---- QToolButton（ツール/ヘッダーバー用） --------------------------- */
QToolButton {{
    background: transparent;
    border: none;
    border-radius: {t.radius}px;
    padding: 4px;
}}
QToolButton:hover {{
    background: {t.hover_bg};
}}
QToolButton:checked {{
    background: {t.accent_bg};
}}

/* ---- QToolTip ------------------------------------------------------- */
QToolTip {{
    background: {t.s2};
    color: {t.fg};
    border: 1px solid {t.border};
    padding: 3px 6px;
}}

/* ---- QPushButton ----------------------------------------------------- */
QPushButton {{
    background: {t.s2};
    color: {t.fg};
    border: 1px solid {t.border};
    border-radius: {t.radius}px;
    padding: 5px 14px;
}}
QPushButton:hover {{
    border: 1px solid {t.border_strong};
}}
QPushButton[variant="primary"] {{
    background: {t.accent};
    color: {t.on_accent};
    border: none;
}}
QPushButton[variant="primary"]:hover {{
    background: {t.accent_hover};
}}

/* ---- 入力欄（QLineEdit/QSpinBox/QDoubleSpinBox/QComboBox） ---------- */
QLineEdit, QSpinBox, QDoubleSpinBox, QComboBox {{
    background: {t.s2};
    color: {t.fg};
    border: 1px solid {t.border};
    border-radius: {t.radius}px;
    padding: 3px 8px;
}}
QLineEdit:focus, QSpinBox:focus, QDoubleSpinBox:focus, QComboBox:focus {{
    border: 1px solid {t.accent};
}}
QSpinBox::up-button, QSpinBox::down-button,
QDoubleSpinBox::up-button, QDoubleSpinBox::down-button {{
    /* QSS を当てた時点で Fusion 既定の▲▼描画が失われ「角括弧状の線」に
       化けるため、幅 0 で完全に非表示にする（矢印キー/ホイール/直接入力は
       生きたまま。P2 でスクラブ入力を導入する方針とも整合するミニマル方針）。 */
    width: 0px;
    border: none;
}}
QComboBox QAbstractItemView {{
    background: {t.s2};
    border: 1px solid {t.border};
    selection-background-color: {t.accent_bg};
    selection-color: {t.fg};
}}

/* ---- QScrollBar ------------------------------------------------------- */
QScrollBar:vertical {{
    background: transparent;
    width: 10px;
    margin: 0px;
}}
QScrollBar::handle:vertical {{
    background: #C9CDD2;
    border-radius: 5px;
    min-height: 20px;
}}
QScrollBar::handle:vertical:hover {{
    background: #B4B9BF;
}}
QScrollBar::add-line:vertical, QScrollBar::sub-line:vertical {{
    width: 0px;
    height: 0px;
}}
QScrollBar:horizontal {{
    background: transparent;
    height: 10px;
    margin: 0px;
}}
QScrollBar::handle:horizontal {{
    background: #C9CDD2;
    border-radius: 5px;
    min-width: 20px;
}}
QScrollBar::handle:horizontal:hover {{
    background: #B4B9BF;
}}
QScrollBar::add-line:horizontal, QScrollBar::sub-line:horizontal {{
    width: 0px;
    height: 0px;
}}

/* ---- QDockWidget ------------------------------------------------------- */
QDockWidget::title {{
    background: {t.s1};
    padding: 6px 10px;
}}

/* ---- QListWidget ------------------------------------------------------- */
QListWidget {{
    background: {t.s1};
    border: none;
}}
QListWidget::item {{
    padding: 4px;
}}
QListWidget::item:selected {{
    background: {t.accent_bg};
    color: {t.fg};
}}

/* ---- QStatusBar ------------------------------------------------------- */
QStatusBar {{
    background: {t.s0};
    color: {t.fg_muted};
    border-top: 1px solid {t.border};
}}
QStatusBar::item {{
    border: none;
}}

/* ---- QSplitter ------------------------------------------------------- */
QSplitter::handle {{
    background: {t.border};
}}

/* ---- HeaderBar（統合バー、objectName="headerBar"） --------------------- */
#headerBar {{
    background: {t.s1};
    border-bottom: 1px solid {t.border};
}}
/* HeaderBar 内の縦区切り線（objectName="headerSep"）。ウィジェット単位の
   setStyleSheet だと色がテーマから切り離されて焼き込まれるため、他の部品と
   同様にここへ一本化する。 */
#headerSep {{
    border: none;
    background: {t.border};
}}

/* ---- PropertyPanel セクション見出し（role="section"、P2契約 §3.4/§3.5） ---- */
QLabel[role="section"] {{
    color: {t.fg_muted};
    font-size: 9pt;
    padding-top: 8px;
}}

/* ---- ZoomPill（objectName="zoomPill"、P3契約 §2/§5） --------------------- */
#zoomPill QLabel {{
    color: {t.fg};
    font-size: 9pt;
}}
#zoomPill QToolButton {{
    border-radius: 4px;
}}

/* ---- ステータスバー恒常ラベルの余白（P3契約 §3.2/§5） --------------------- */
QStatusBar QLabel {{
    padding: 0 6px;
}}
""".strip()
