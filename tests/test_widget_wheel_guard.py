"""`install_wheel_guard`（`app/ui/widgets/wheel_guard.py`）の単体テスト（P2契約 D1-6）。

パネルをホイールでスクロールしたとき、カーソル直下の未フォーカスなコンボ/スピンの
値が変わって undo が積まれてしまう既存バグを塞ぐことの回帰。
"""

from __future__ import annotations

from typing import Any

from PySide6.QtCore import QPoint, QPointF, Qt
from PySide6.QtGui import QWheelEvent
from PySide6.QtWidgets import QApplication, QComboBox, QLabel, QScrollArea, QVBoxLayout, QWidget

from app.ui.widgets.wheel_guard import install_wheel_guard


def _wheel_event(delta: int = -120) -> QWheelEvent:
    return QWheelEvent(
        QPointF(5, 5),
        QPointF(5, 5),
        QPoint(0, 0),
        QPoint(0, delta),
        Qt.MouseButton.NoButton,
        Qt.KeyboardModifier.NoModifier,
        Qt.ScrollPhase.NoScrollPhase,
        False,
    )


def test_install_wheel_guard_sets_strong_focus_policy(qapp: Any) -> None:
    combo = QComboBox()
    install_wheel_guard(combo)
    assert combo.focusPolicy() == Qt.FocusPolicy.StrongFocus


def test_unfocused_wheel_does_not_change_combo_value(qapp: Any) -> None:
    combo = QComboBox()
    combo.addItems(["a", "b", "c"])
    install_wheel_guard(combo)
    combo.setCurrentIndex(0)
    assert not combo.hasFocus()

    QApplication.sendEvent(combo, _wheel_event())
    assert combo.currentIndex() == 0, "フォーカスが無ければ値が変わってはいけない"


def test_focused_wheel_still_changes_combo_value(qapp: Any) -> None:
    window = QWidget()
    combo = QComboBox(window)
    combo.addItems(["a", "b", "c"])
    install_wheel_guard(combo)
    combo.setCurrentIndex(0)
    window.show()
    qapp.processEvents()
    combo.setFocus()
    qapp.processEvents()
    assert combo.hasFocus()

    QApplication.sendEvent(combo, _wheel_event())
    assert combo.currentIndex() == 1, "フォーカスがあれば従来どおり値を変えられる"
    window.close()


def test_unfocused_wheel_is_forwarded_up_to_the_scroll_area(qapp: Any) -> None:
    """カーソル下のコンボがホイールを奪わず、祖先の QScrollArea が代わりにスクロール
    する（`_WheelGuardFilter` がコンボの直接の親（レイアウト用コンテナ）だけでなく、
    実際にスクロールする viewport まで手動で送り届けることの固定）。"""
    scroll = QScrollArea()
    content = QWidget()
    layout = QVBoxLayout(content)
    combo = QComboBox()
    combo.addItems(["a", "b", "c"])
    install_wheel_guard(combo)
    layout.addWidget(combo)
    for i in range(60):
        layout.addWidget(QLabel(f"row {i}"))
    scroll.setWidget(content)
    scroll.setWidgetResizable(True)
    scroll.resize(200, 100)
    scroll.show()
    qapp.processEvents()
    combo.clearFocus()
    qapp.processEvents()
    assert not combo.hasFocus()

    scrollbar = scroll.verticalScrollBar()
    before = scrollbar.value()
    QApplication.sendEvent(combo, _wheel_event())
    qapp.processEvents()

    assert combo.currentIndex() == 0, "コンボの値は変わらない"
    assert scrollbar.value() != before, "ホイールはスクロール領域まで届く"
    scroll.close()


def test_forward_when_focused_never_changes_combo_and_scrolls_parent(qapp: Any) -> None:
    """finding #4: `forward_when_focused=True` はフォーカス中でも値を変えない。

    `FontFamilyCombo` のように、クリックで選んだ直後もフォーカスが残るコンボに
    使う想定（フォーカス直後にパネルをホイールでスクロールすると、カーソル下の
    フォント一覧（約560件）を1段ずつ回してしまう事故を防ぐ）。
    """
    scroll = QScrollArea()
    content = QWidget()
    layout = QVBoxLayout(content)
    combo = QComboBox()
    combo.addItems(["a", "b", "c"])
    install_wheel_guard(combo, forward_when_focused=True)
    layout.addWidget(combo)
    for i in range(60):
        layout.addWidget(QLabel(f"row {i}"))
    scroll.setWidget(content)
    scroll.setWidgetResizable(True)
    scroll.resize(200, 100)
    scroll.show()
    qapp.processEvents()
    combo.setFocus()
    qapp.processEvents()
    assert combo.hasFocus()

    scrollbar = scroll.verticalScrollBar()
    before_index = combo.currentIndex()
    before_scroll = scrollbar.value()
    QApplication.sendEvent(combo, _wheel_event())
    qapp.processEvents()

    assert combo.currentIndex() == before_index, "フォーカスがあっても値を変えない"
    assert scrollbar.value() != before_scroll, "ホイールはスクロール領域まで届く"
    scroll.close()


def test_default_forward_when_focused_is_false_and_unchanged_behavior(qapp: Any) -> None:
    """既定（省略時）は従来どおりフォーカス時に値を変える（enum コンボ/スピン用）。"""
    window = QWidget()
    combo = QComboBox(window)
    combo.addItems(["a", "b", "c"])
    install_wheel_guard(combo)
    combo.setCurrentIndex(0)
    window.show()
    qapp.processEvents()
    combo.setFocus()
    qapp.processEvents()
    assert combo.hasFocus()

    QApplication.sendEvent(combo, _wheel_event())
    assert combo.currentIndex() == 1
    window.close()


def test_wheel_guard_does_not_block_events_for_other_types(qapp: Any) -> None:
    """`event.type() != Wheel` の分岐（他のイベントには一切介入しない）。"""
    combo = QComboBox()
    install_wheel_guard(combo)
    # フォーカスイベント等、通常の操作が引き続き機能すること（クラッシュしない）。
    combo.show()
    qapp.processEvents()
    combo.close()
