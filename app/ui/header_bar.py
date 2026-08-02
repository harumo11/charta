"""統合ヘッダーバー（P1契約・UIモダン化 方式A）。

タイトルバー直下に「メニュー + ツール + 主要アクション」を1本にまとめて配置する。
`MainWindow._build_header_bar()` が構築した `QMenuBar`/`QAction` 群を受け取って並べる
だけの薄いレイアウト用ウィジェットで、アクションの生成・配線（ツール切替・undo/redo・
エクスポート）はすべて呼び出し側（MainWindow）の責務とする。
"""

from __future__ import annotations

from PySide6.QtCore import QSize, Qt
from PySide6.QtGui import QAction
from PySide6.QtWidgets import QFrame, QHBoxLayout, QMenuBar, QSizePolicy, QToolButton, QWidget

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


def _tool_button(action: QAction, *, fixed_size: bool = True) -> QToolButton:
    """`action` を紐付けた `QToolButton` を作る（アイコンのみ表示、ラベルはツールチップ）。"""
    button = QToolButton()
    button.setDefaultAction(action)
    button.setIconSize(_ICON_SIZE)
    if fixed_size:
        button.setFixedSize(_BUTTON_SIZE)
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
        layout.addWidget(_separator())
        layout.addWidget(_tool_button(copy_image_action))

        # 書き出しボタンはドロップダウン矢印を持つため固定 32×32 にはしない
        # （矢印込みで自然な幅に広がるままにする）。高さだけは他の 32×32 ボタンと
        # 縦位置を揃えるため固定する（幅を固定しないため fixed_size=False のまま）。
        # メニューは `export_action.menu()` を `MenuButtonPopup` が自動的に使う
        # （呼び出し側で `setMenu()` 済み）。
        export_button = _tool_button(export_action, fixed_size=False)
        export_button.setPopupMode(QToolButton.ToolButtonPopupMode.MenuButtonPopup)
        export_button.setFixedHeight(_BUTTON_SIZE.height())
        layout.addWidget(export_button)
