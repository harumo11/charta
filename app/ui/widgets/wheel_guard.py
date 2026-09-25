"""ホイールイベントのガード（P2契約 D1-6）。

プロパティパネルはフォームがドックの高さを超えると縦スクロールに逃がす
（`property_panel.py` の `QScrollArea`）。ところがマウスホイールでパネルを
スクロールしようとすると、カーソルの真下にたまたまある未フォーカスの
`QComboBox`/`QSpinBox`/`QDoubleSpinBox` がホイールを奪って値を変えてしまい、
スクロールもできないうえに undo が積まれる（既存バグ）。

`install_wheel_guard` は、そのウィジェットが**フォーカスを持っていない間だけ**
ホイールイベントを無視し、親（スクロール領域）へ委ねる。フォーカスを与えて
（クリックまたは Tab）から回す分にはこれまでどおり値を変えられる。
"""

from __future__ import annotations

from PySide6.QtCore import QEvent, QObject, Qt
from PySide6.QtWidgets import QApplication, QWidget


class _WheelGuardFilter(QObject):
    """フォーカスの無いウィジェットへの Wheel イベントを親へ受け流すフィルタ。

    `forward_when_focused=True` のときは、フォーカスの有無に関わらず常に
    ホイールを親へ受け流す（finding #4）。約560件のフォントを持つ
    `FontFamilyCombo` はクリックで選んだ後もフォーカスが残るため、既定の
    「フォーカスがあれば通常どおり」だと、その後パネルをホイールでスクロール
    しただけで一覧を1段ずつ回してフォントが変わってしまう（各段が独立した
    undo エントリにもなる）。旧実装（`QLineEdit`）はホイールに反応しなかった。
    """

    def __init__(self, parent: QWidget, *, forward_when_focused: bool = False) -> None:
        super().__init__(parent)
        self._forward_when_focused = forward_when_focused

    def eventFilter(self, watched: QObject, event: QEvent) -> bool:  # noqa: N802 (Qt override)
        if event.type() != QEvent.Type.Wheel or not isinstance(watched, QWidget):
            return False
        if watched.hasFocus() and not self._forward_when_focused:
            # フォーカスがあるときは通常どおりウィジェット自身に値を変えさせる。
            return False
        # 1段だけ親へ送っても、その親が素の QWidget（レイアウト用のコンテナ）
        # なら既定の wheelEvent が ignore() するだけで止まってしまい、実際に
        # スクロールする祖先（QScrollArea の viewport）まで届かない。
        # 誰かが受理する（event.isAccepted() が True になる）か祖先が尽きるまで
        # 1段ずつ送り直す（`QApplication.sendEvent` は通常の配送と同じく
        # ウィジェット自身の `wheelEvent` を経由するので、受理判定もそちらに従う）。
        event.ignore()
        target = watched.parentWidget()
        while target is not None and not event.isAccepted():
            QApplication.sendEvent(target, event)
            target = target.parentWidget()
        # ここで True を返し、フォーカスの無いウィジェット自身には処理させない
        # （素通りさせると Fusion スタイルの QComboBox/QSpinBox は ignore() を
        # 無視して値を変えてしまう）。
        return True


def install_wheel_guard(widget: QWidget, *, forward_when_focused: bool = False) -> None:
    """`widget` にホイールガードを付け、`StrongFocus` にする。

    パネル内の全 `QComboBox`/`QSpinBox`/`QDoubleSpinBox` に掛ける想定
    （`FontFamilyCombo` は自前で呼ぶ。他のコントロールへの配線はプロパティ
    パネル側の担当）。フィルタは `widget` を親として生成するので、Qt の
    親子所有権により `widget` が生きている間は GC されない
    （念のため属性としても保持し、二重解放を避ける）。

    `forward_when_focused=True` は、項目数の多い一覧（`FontFamilyCombo` の
    フォント約560件）向けの特別扱い。短い enum コンボやスピンボックスでは
    ホイールで値を1段ずつ調整するのは妥当な操作なので既定 False のまま使う
    （`test_focused_wheel_still_changes_combo_value` が既定の挙動を固定する）。
    """
    widget.setFocusPolicy(Qt.FocusPolicy.StrongFocus)
    guard = _WheelGuardFilter(widget, forward_when_focused=forward_when_focused)
    widget.installEventFilter(guard)
    widget._charta_wheel_guard = guard  # type: ignore[attr-defined]
