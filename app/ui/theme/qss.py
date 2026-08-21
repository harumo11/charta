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
    padding-top: 10px;
    padding-bottom: 2px;
}}

/* ---- PropertyPanel フォームのスクロール領域（objectName="propertyPanelFormScroll"、
   レビュー所見対応） ----
   ドックがフォームの sizeHint 未満の高さしか与えないとき（1366×768/1200×900 の
   ような常用ウィンドウで text/rect フォームは容易にこれを超える）、行を圧縮せず
   縦スクロールに逃がすための QScrollArea を導入した（`PropertyPanel.__init__`）。
   QScrollArea の既定はビューポートに QPalette::Base を敷くため、無地のままだと
   フォームだけ別の箱として浮いて見える。ボーダー無し・背景透過にしてパネルへ
   同化させる（`> QWidget` は直接の子＝ビューポートだけを狙い、その孫である
   `#propertyPanelForm` 自体には影響しない）。 */
#propertyPanelFormScroll, #propertyPanelFormScroll > QWidget {{
    background: transparent;
    border: none;
}}

/* ---- PropertyPanel 行高の統一（objectName="propertyPanelForm"、P2契約 担当A） ----
   QSS の min-height は「内容矩形」の高さで、border/padding は別に加算される。
   種別ごとに内容矩形の自然な高さが違うため、Theme.control_h からその種別固有の
   枠線・padding 分を差し引いた値を min-height に与えることで、どの kind の
   フィールドも最終的な外形高さが control_h に揃う（実測値は QT_QPA_PLATFORM=
   offscreen かつテーマ適用下、tests/test_panel_row_metrics.py で固定）。

   **QSpinBox/QDoubleSpinBox/QCheckBox は QSS 係数だけでは不十分**: 実測では
   Fusion スタイルの min-height 計算結果がウィジェットに割り当てられている
   「幅」次第で変わる（point 行の圧縮や縦スクロールバーの出現/消失で幅が
   変わるたびに 29/32/35/37px のように揺れた、レビュー所見）。そのため
   `app/panels/property_panel.py::_pin_control_height` がこの2種には
   `setFixedHeight(control_h)` を明示的に適用しており、実際の外形高さは
   そちらが決める。ここでの min-height は QSS だけが効く場面（万一
   `_pin_control_height` を適用し忘れた場合の下限）のための保険。

   スコープ鍵は PropertyPanel 自身（#propertyPanel）ではなく、フォーム内容だけを
   包む内側のコンテナ（#propertyPanelForm）にしている。`QColorDialog` は
   `PropertyPanel` 自身を親にとって開かれるため、`#propertyPanel` を鍵にすると
   子孫セレクタがダイアログ内部の OK/Cancel/「画面上の色を選択」ボタンや RGB/HSV
   スピンにまで届いてしまい、幅108px固定でラベル文字がクリップされる（実測で
   確認済みの回帰）。ダイアログは `#propertyPanelForm` の子孫ではないため、
   このスコープなら実際にフォームへ配置されたコントロールだけに効く。 */
#propertyPanelForm QLineEdit, #propertyPanelForm QComboBox {{
    /* border 1px×2 + padding 3px×2 = 8px。control_h - 8 が内容矩形の高さ。 */
    min-height: {t.control_h - 8}px;
}}
#propertyPanelForm QSpinBox, #propertyPanelForm QDoubleSpinBox {{
    /* 実際の外形高さは `_pin_control_height`（`setFixedHeight`）が決める
       （上の注記参照）。ここは保険の下限。border 1px×2 + padding 3px×2 = 8px。 */
    min-height: {t.control_h - 8}px;
}}
#propertyPanelForm QPushButton {{
    /* 色スウォッチ。既定の QPushButton は padding 5px 14px で他コントロールより
       1 行分高くなるため、ここだけ padding を 3px 8px に縮める
       （border 1px×2 + padding 3px×2 = 8px、QLineEdit と同じ内訳に揃える）。
       border はこのセレクタで再度明示しておく（実測では上の汎用 QPushButton
       規則からも継承され描画自体はされているが、`setStyleSheet(
       "background-color: ...")` をウィジェット単位で当てる箇所なので、汎用規則
       の border が将来変わってもスウォッチの見た目がここ 1 か所の変更で
       自己完結するようにする。レビュー所見: 白/淡色スウォッチと空の入力欄
       （どちらも背景 {t.s2}）は境界線の色が同じだと見分けづらく、hex は
       tooltip 頼みになる — 完全解消ではないが自己完結にはしておく）。 */
    border: 1px solid {t.border};
    padding: 3px 8px;
    min-height: {t.control_h - 8}px;
    /* color_opt の null_label（"なし"/"透明（切り取り）"）と色ありの空文字とで
       テキスト長が変わっても sizeHint が揺れないよう、幅も明示的に固定する
       （QSS で width を明示すると、その種別の sizeHint はテキスト内容に関わらず
       常にこの値になる。テキストが収まる余裕を持たせた値。実測は
       tests/test_panel_row_metrics.py::test_color_swatch_width_is_independent_of_color_value）。 */
    min-width: 108px;
    max-width: 108px;
}}
#propertyPanelForm QPushButton::menu-indicator {{
    /* スピンの▲▼を width:0 で隠しているのと同じ方針。メニュー矢印が色面の
       右端に食い込んで色が見えづらくなるのを防ぐ（メニューはクリックで開ける）。 */
    width: 0px;
    image: none;
}}
#propertyPanelForm QCheckBox {{
    /* 実際の外形高さは `_pin_control_height`（`setFixedHeight`）が決める
       （上の QSpinBox 注記と同じ理由）。ここは保険の下限。border/padding を
       持たないため min-height をそのまま渡す。 */
    min-height: {t.control_h}px;
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
