"""統合ヘッダーバー（P1契約・UIモダン化 方式A）。

タイトルバー直下に「メニュー + ツール + 主要アクション」を1本にまとめて配置する。
`MainWindow._build_header_bar()` が構築した `QMenuBar`/`QAction` 群を受け取って並べる
だけの薄いレイアウト用ウィジェットで、アクションの生成・配線（ツール切替・undo/redo・
エクスポート）はすべて呼び出し側（MainWindow）の責務とする。
"""

from __future__ import annotations

from PySide6.QtCore import QSize, Qt
from PySide6.QtGui import QAction
from PySide6.QtWidgets import (
    QFrame,
    QHBoxLayout,
    QMenu,
    QMenuBar,
    QSizePolicy,
    QToolButton,
    QWidget,
)

# ツールボタンの寸法（契約: 32×32、アイコン18×18・高密度UI）。
_BUTTON_SIZE = QSize(32, 32)
_ICON_SIZE = QSize(18, 18)


def _separator() -> QFrame:
    """縦区切り線（メニュー / ツール群 / 取り込みボタンの間を仕切る、1px border色）。"""
    line = QFrame()
    line.setFrameShape(QFrame.Shape.VLine)
    line.setFrameShadow(QFrame.Shadow.Plain)
    line.setLineWidth(1)
    line.setFixedWidth(1)
    # Fusion の既定 QFrame 描画（明暗2色の溝）だとトークンの border 色にならないため、
    # 色は QSS 側（`#headerSep`、qss.py）に一本化する（ウィジェット単位 setStyleSheet の
    # 焼き込みを避け、テーマ切替時に追従できるようにするため）。
    line.setObjectName("headerSep")
    return line


def _tool_button(
    action: QAction,
    *,
    fixed_size: bool = True,
    with_menu: bool = False,
    menu: QMenu | None = None,
    text: str | None = None,
) -> QToolButton:
    """`action` を紐付けた `QToolButton` を作る（アイコンのみ表示、ラベルはツールチップ）。

    メニュー付きボタンには 2 系統ある:
    - `with_menu=True`: `action.menu()`（呼び出し側で `setMenu()` 済み）を
      `MenuButtonPopup` として持たせる。`export_action` のように他所と共有
      していない専用 QAction にのみ使う。
    - `menu=...`: `QToolButton.setMenu()`（**QAction ではなくボタン自身**）に
      メニューを付ける。QAction が `menu()` を持つと Qt はその項目を常に
      サブメニュー扱いにしてクリックで `triggered` を出さなくなる（実測確認済み）
      ため、編集メニューの「画面を画像としてコピー」等と共有している QAction
      （`copy_image_action`）にはこちらを使う。`QToolButton.setDefaultAction()`
      と `setMenu()` は独立したプロパティなので、本体クリックは action の
      triggered・矢印クリックは `menu` の展開という分離が両立する。

    `text` を渡すとアイコン+テキスト表示にする（項目7契約）。**共有 QAction の
    `setText` は使わない**——ボタン側だけにテキストを付けることで、同じ QAction
    を使う編集メニューの項目文言（例:「画面を画像としてコピー」）を壊さない。

    注意（構造的な脆さ、レビュー所見）: `QToolButton` は `ActionChanged` を受けると
    `setText(action.iconText())` を再実行し、ここで設定した `text` を action の
    文言に戻してしまう。`setDefaultAction()` 呼び出し**後**に共有 QAction へ
    `setEnabled`/`setToolTip`/`setIcon` 等を行うと再現するため、この関数呼び出しの
    後で当該 QAction の見た目に触る変更を足さないこと（触る必要があるなら、
    ヘッダー専用の QAction を分けて共有をやめるのが本筋）。
    """
    button = QToolButton()
    button.setDefaultAction(action)
    button.setIconSize(_ICON_SIZE)
    if fixed_size:
        button.setFixedSize(_BUTTON_SIZE)
    if menu is not None:
        button.setMenu(menu)
        button.setPopupMode(QToolButton.ToolButtonPopupMode.MenuButtonPopup)
    elif with_menu:
        button.setPopupMode(QToolButton.ToolButtonPopupMode.MenuButtonPopup)
    if text is not None:
        button.setToolButtonStyle(Qt.ToolButtonStyle.ToolButtonTextBesideIcon)
        button.setText(text)
    return button


class HeaderBar(QWidget):
    """タイトルバー直下の統合バー（メニュー + ツール + 主要アクション）。方式A。"""

    def __init__(
        self,
        menu_bar: QMenuBar,
        tool_actions: list[QAction],
        import_action: QAction,
        undo_action: QAction,
        redo_action: QAction,
        copy_image_action: QAction,
        copy_menu: QMenu,
        export_action: QAction,
        parent: QWidget | None = None,
    ) -> None:
        super().__init__(parent)
        self.setObjectName("headerBar")
        # Python 側の QWidget サブクラスは既定で WA_StyledBackground を持たず、QSS の
        # background/border 系プロパティ（#headerBar の下線含む）が描画されない
        # （C++ 側で QSS 対応済みのウィジェット、例: QMenuBar/QToolButton とは異なる）。
        self.setAttribute(Qt.WidgetAttribute.WA_StyledBackground, True)

        # メニューは横に伸縮させない（伸びるとツール群を右へ押し出してしまうため）。
        menu_bar.setSizePolicy(QSizePolicy.Policy.Fixed, QSizePolicy.Policy.Preferred)

        layout = QHBoxLayout(self)
        layout.setContentsMargins(6, 4, 8, 4)
        layout.setSpacing(2)

        layout.addWidget(menu_bar)
        layout.addWidget(_separator())

        for action in tool_actions:
            layout.addWidget(_tool_button(action))

        layout.addWidget(_separator())
        layout.addWidget(_tool_button(import_action))

        layout.addStretch(1)

        layout.addWidget(_tool_button(undo_action))
        layout.addWidget(_tool_button(redo_action))

        # 「画面を画像としてコピー」は書き出しと同じ出力系アクションとして隣に置く。
        # ドロップダウンに「選択範囲をコピー」「透過背景でコピー」を持つため（項目7契約）、
        # 書き出しボタンと同様に固定高にする（幅は矢印込みで自然に広がるまま固定しない）。
        # コピーは動線が編集メニューと分かれるため、ボタン側にだけラベル「コピー」を付ける
        # （export ボタンはラベルなし＝アイコンのみ）。`menu=copy_menu` は共有 QAction
        # ではなくボタン自身にメニューを付ける経路（`_tool_button` docstring 参照）。
        layout.addWidget(_separator())
        copy_button = _tool_button(
            copy_image_action, fixed_size=False, menu=copy_menu, text="コピー"
        )
        copy_button.setFixedHeight(_BUTTON_SIZE.height())
        layout.addWidget(copy_button)

        # 書き出しボタンはドロップダウン矢印を持つため固定 32×32 にはしない
        # （矢印込みで自然な幅に広がるままにする）。高さだけは他の 32×32 ボタンと
        # 縦位置を揃えるため固定する（幅を固定しないため fixed_size=False のまま）。
        # メニューは `export_action.menu()` を `MenuButtonPopup`（`with_menu=True`）が
        # 自動的に使う（呼び出し側で `setMenu()` 済み）。
        export_button = _tool_button(export_action, fixed_size=False, with_menu=True)
        export_button.setFixedHeight(_BUTTON_SIZE.height())
        layout.addWidget(export_button)
