"""選択中オブジェクトのプロパティを編集するパネル（§9.1）。

`scene.selectionChanged` を購読し、単一選択時に `PROPERTIES[obj.type]` から
`QFormLayout` を動的生成する。値変更はすべて `QUndoCommand`（`SetGeometryCommand`/
`SetPropertyCommand`）経由でモデルに反映する。
"""

from __future__ import annotations

from collections.abc import Callable
from typing import TYPE_CHECKING, Any

import shiboken6
from PySide6.QtCore import QTimer
from PySide6.QtGui import QColor
from PySide6.QtWidgets import (
    QCheckBox,
    QColorDialog,
    QComboBox,
    QDoubleSpinBox,
    QFormLayout,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QPushButton,
    QSpinBox,
    QVBoxLayout,
    QWidget,
)

from app.commands.commands import SetGeometryCommand, SetPropertyCommand
from app.model.objects import BaseObject
from app.model.properties import PROPERTIES, PropSpec

if TYPE_CHECKING:
    from app.scene.canvas_scene import CanvasScene

# x/y/width/height/rotation は SetGeometryCommand、それ以外は SetPropertyCommand（§9.1）。
_GEOMETRY_KEYS = {"x", "y", "width", "height", "rotation"}

_SPIN_RANGE = 1_000_000.0
_DEFAULT_COLOR = "#000000"
_COLOR_PROP = "charta_color"
# 要件: 数値スピナーの表示・入力は小数第1位まで（int の QSpinBox は対象外）。
# ただし step が 0.1 未満の項目（opacity/brightness/contrast/smoothing、
# properties.py の step=0.05 指定）はこの丸めだと刻み幅が実質失われる
# （例: setValue(0.85)->0.8, stepBy(-1) が 1.0->0.9 になり singleStep と
# 表示が食い違う）ため、PropSpec.decimals で個別に上書きできるようにする。
_NUMBER_DECIMALS = 1

# パネル固定幅（選択対象に依らず一定にする要件への対応）。
# 実測（QT_QPA_PLATFORM=offscreen, decimals=1 適用後）: 9 種別すべての
# PropertyPanel.sizeHint().width() のうち line/arrow（point ウィジェット
# ×2 行を含む）が最大で 332px。他の全種別・未選択・複数選択はこれより
# 小さい。この最大値にフォント差など環境差のマージンを載せて 340px とする
# （setFixedWidth は最小=最大幅を固定するため、これより狭いとラベル/
# フィールドが切り詰められる — 実測値を下回らないことが必須）。
_PANEL_FIXED_WIDTH = 340


class PropertyPanel(QWidget):
    """選択オブジェクトのプロパティ編集フォーム。"""

    def __init__(self, scene: CanvasScene, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.scene = scene
        self._rebuilding = False

        # 現在フォーム表示中の対象の識別子（in-place 更新の可否判定用）。
        # None は「フォーム未表示」（未選択/複数選択）を意味する。
        self._current_obj_id: int | None = None
        self._current_type: str | None = None
        # 各ウィジェット生成時に登録する「モデル値→ウィジェットへ反映する」updater。
        # undo/redo 等で対象が変わらないまま値だけ変わった場合、フォームを
        # 破棄せずこれらを呼ぶだけで再同期する（§M8クラッシュ修正、下記参照）。
        self._updaters: list[Callable[[], None]] = []
        # このパネル自身が push() 中かどうかの深さ（ネスト対応の安全のため int）。
        # locked/visible の編集は sync_item() 内で item の
        # ItemIsSelectable/visible フラグを変えるため、Qt が当該 item を
        # 自動的に選択解除し、redo() の途中で scene.selectionChanged が
        # 同期的に（この push() 呼び出しの中で）発火し得る。そのタイミングで
        # 破壊的な _clear_form/_build_form を行うと「いま toggled を発火中の
        # チェックボックス自身」を破棄してしまう（実測でクラッシュ確認済み）。
        # push_depth > 0 の間は破壊的パスを _schedule_deferred_resync() に
        # 委譲し、push() が完全に戻ってから安全に再評価する。
        self._push_depth = 0
        self._resync_pending = False

        self._info_label = QLabel("未選択")
        self._form = QFormLayout()
        self._form_widget = QWidget()
        self._form_widget.setLayout(self._form)

        layout = QVBoxLayout(self)
        layout.addWidget(self._info_label)
        layout.addWidget(self._form_widget)
        layout.addStretch(1)

        # 選択オブジェクト種別ごとにフォーム内容の幅が変わっても QDockWidget が
        # 追従リサイズしないよう、パネル自体を固定幅にする（要件1）。
        self.setFixedWidth(_PANEL_FIXED_WIDTH)

        self.scene.selectionChanged.connect(self.on_selection_changed)
        self.on_selection_changed()

    # ------------------------------------------------------------------
    # 選択変更・フォーム構築
    # ------------------------------------------------------------------

    def on_selection_changed(self) -> None:
        """選択に応じてフォームを更新する。

        `undo_stack.indexChanged` はパネル自身が push した編集の redo でも
        発火するため、この slot は「フォーム編集の真っ最中に自分自身を
        呼び出す」再入を起こし得る。対象（id/type）が変わっていなければ
        `_refresh_values()` で値だけを signals ブロックしつつ再設定し、
        フォームは破棄しない（`_clear_form`/`_build_form` はウィジェットを
        removeRow で破棄するため、いま valueChanged 等を発火中のウィジェット
        自身を破棄すると use-after-free でクラッシュする — 実際に発生していた
        バグ）。

        対象が変わった場合（未選択/複数選択への変化・別オブジェクト選択）は
        フォーム再構築が必須だが、`self._push_depth > 0`（このパネル自身の
        push() 実行中の再入）であれば `_schedule_deferred_resync()` に委譲し、
        push() が完全に戻ってから安全に再構築する。
        """
        if not shiboken6.isValid(self) or not shiboken6.isValid(self.scene):
            return
        selected = self.scene.selected_objects()

        if len(selected) == 1:
            obj = selected[0]
            if (
                self._current_obj_id is not None
                and obj.id == self._current_obj_id
                and obj.type == self._current_type
            ):
                self._refresh_values()
                return

        if self._push_depth > 0:
            self._schedule_deferred_resync()
            return

        self._apply_selection_state(selected)

    def _apply_selection_state(self, selected: list[BaseObject]) -> None:
        """フォームを（必要なら）破棄して選択状態どおりに再構築する。"""
        if len(selected) == 1:
            obj = selected[0]
            self._current_obj_id = obj.id
            self._current_type = obj.type
            self._info_label.hide()
            self._form_widget.show()
            self._clear_form()
            self._build_form(obj)
            return

        self._current_obj_id = None
        self._current_type = None
        self._clear_form()
        if len(selected) == 0:
            self._info_label.setText("未選択")
        else:
            self._info_label.setText(f"{len(selected)} 個選択中")
        self._info_label.show()
        self._form_widget.hide()

    def _schedule_deferred_resync(self) -> None:
        """push() 完了後（次のイベントループ）に一度だけ再評価を予約する。"""
        if self._resync_pending:
            return
        self._resync_pending = True
        QTimer.singleShot(0, self._run_deferred_resync)

    def _run_deferred_resync(self) -> None:
        self._resync_pending = False
        if not shiboken6.isValid(self) or not shiboken6.isValid(self.scene):
            return
        self.on_selection_changed()

    def _refresh_values(self) -> None:
        """フォームを再構築せず、登録済み updater で現在値を再設定する。"""
        for updater in list(self._updaters):
            updater()

    def _clear_form(self) -> None:
        while self._form.rowCount() > 0:
            self._form.removeRow(0)

    def _build_form(self, obj: BaseObject) -> None:
        self._rebuilding = True
        self._updaters = []
        try:
            specs = PROPERTIES.get(obj.type, [])
            for spec in specs:
                widget = self._make_widget(obj, spec)
                self._form.addRow(spec.label, widget)
        finally:
            self._rebuilding = False

    # ------------------------------------------------------------------
    # ウィジェット生成（kind ごと）
    # ------------------------------------------------------------------

    def _make_widget(self, obj: BaseObject, spec: PropSpec) -> QWidget:
        if spec.kind == "number":
            return self._make_number_widget(obj, spec)
        if spec.kind == "int":
            return self._make_int_widget(obj, spec)
        if spec.kind == "point":
            return self._make_point_widget(obj, spec)
        if spec.kind == "color":
            return self._make_color_widget(obj, spec)
        if spec.kind == "color_opt":
            return self._make_color_opt_widget(obj, spec)
        if spec.kind == "enum":
            return self._make_enum_widget(obj, spec)
        if spec.kind == "bool":
            return self._make_bool_widget(obj, spec)
        if spec.kind == "text":
            return self._make_text_widget(obj, spec)
        raise NotImplementedError(f"unknown PropSpec.kind: {spec.kind!r}")

    def _make_number_widget(self, obj: BaseObject, spec: PropSpec) -> QDoubleSpinBox:
        spin = QDoubleSpinBox()
        spin.setKeyboardTracking(False)
        spin.setDecimals(spec.decimals if spec.decimals is not None else _NUMBER_DECIMALS)
        spin.setMinimum(spec.minimum if spec.minimum is not None else -_SPIN_RANGE)
        spin.setMaximum(spec.maximum if spec.maximum is not None else _SPIN_RANGE)
        spin.setSingleStep(spec.step if spec.step is not None else 1.0)
        spin.blockSignals(True)
        spin.setValue(float(getattr(obj, spec.key)))
        spin.blockSignals(False)

        def on_changed(value: float, obj: BaseObject = obj, spec: PropSpec = spec) -> None:
            if self._rebuilding:
                return
            old_value = getattr(obj, spec.key)
            if value == old_value:
                return
            self._commit_scalar(obj, spec.key, value, old_value)

        spin.valueChanged.connect(on_changed)

        def update_widget(
            spin: QDoubleSpinBox = spin, obj: BaseObject = obj, spec: PropSpec = spec
        ) -> None:
            if not shiboken6.isValid(spin):
                return
            spin.blockSignals(True)
            try:
                spin.setValue(float(getattr(obj, spec.key)))
            finally:
                spin.blockSignals(False)

        self._updaters.append(update_widget)
        return spin

    def _make_int_widget(self, obj: BaseObject, spec: PropSpec) -> QSpinBox:
        spin = QSpinBox()
        spin.setKeyboardTracking(False)
        spin.setMinimum(int(spec.minimum) if spec.minimum is not None else int(-_SPIN_RANGE))
        spin.setMaximum(int(spec.maximum) if spec.maximum is not None else int(_SPIN_RANGE))
        spin.setSingleStep(int(spec.step) if spec.step is not None else 1)
        spin.blockSignals(True)
        spin.setValue(int(getattr(obj, spec.key)))
        spin.blockSignals(False)

        def on_changed(value: int, obj: BaseObject = obj, spec: PropSpec = spec) -> None:
            if self._rebuilding:
                return
            old_value = getattr(obj, spec.key)
            if value == old_value:
                return
            self._commit_scalar(obj, spec.key, value, old_value)

        spin.valueChanged.connect(on_changed)

        def update_widget(
            spin: QSpinBox = spin, obj: BaseObject = obj, spec: PropSpec = spec
        ) -> None:
            if not shiboken6.isValid(spin):
                return
            spin.blockSignals(True)
            try:
                spin.setValue(int(getattr(obj, spec.key)))
            finally:
                spin.blockSignals(False)

        self._updaters.append(update_widget)
        return spin

    def _make_point_widget(self, obj: BaseObject, spec: PropSpec) -> QWidget:
        container = QWidget()
        hlayout = QHBoxLayout(container)
        hlayout.setContentsMargins(0, 0, 0, 0)

        point: list[float] = list(getattr(obj, spec.key))
        spin_x = QDoubleSpinBox()
        spin_y = QDoubleSpinBox()
        for spin in (spin_x, spin_y):
            spin.setKeyboardTracking(False)
            spin.setDecimals(_NUMBER_DECIMALS)
            spin.setMinimum(-_SPIN_RANGE)
            spin.setMaximum(_SPIN_RANGE)
        spin_x.blockSignals(True)
        spin_x.setValue(float(point[0]))
        spin_x.blockSignals(False)
        spin_y.blockSignals(True)
        spin_y.setValue(float(point[1]))
        spin_y.blockSignals(False)

        hlayout.addWidget(QLabel("x"))
        hlayout.addWidget(spin_x)
        hlayout.addWidget(QLabel("y"))
        hlayout.addWidget(spin_y)

        def on_changed(
            _value: float = 0.0,
            obj: BaseObject = obj,
            spec: PropSpec = spec,
            spin_x: QDoubleSpinBox = spin_x,
            spin_y: QDoubleSpinBox = spin_y,
        ) -> None:
            if self._rebuilding:
                return
            old_point = list(getattr(obj, spec.key))
            new_point = [spin_x.value(), spin_y.value()]
            if new_point == old_point:
                return
            cmd = SetGeometryCommand(
                self.scene, obj, {spec.key: new_point}, {spec.key: old_point}, mergeable=True
            )
            self._push(cmd)

        spin_x.valueChanged.connect(on_changed)
        spin_y.valueChanged.connect(on_changed)

        def update_widget(
            obj: BaseObject = obj,
            spec: PropSpec = spec,
            spin_x: QDoubleSpinBox = spin_x,
            spin_y: QDoubleSpinBox = spin_y,
        ) -> None:
            if not shiboken6.isValid(spin_x) or not shiboken6.isValid(spin_y):
                return
            point = list(getattr(obj, spec.key))
            spin_x.blockSignals(True)
            try:
                spin_x.setValue(float(point[0]))
            finally:
                spin_x.blockSignals(False)
            spin_y.blockSignals(True)
            try:
                spin_y.setValue(float(point[1]))
            finally:
                spin_y.blockSignals(False)

        self._updaters.append(update_widget)
        return container

    def _make_color_widget(self, obj: BaseObject, spec: PropSpec) -> QPushButton:
        button = QPushButton()
        self._apply_button_color(button, getattr(obj, spec.key) or _DEFAULT_COLOR)

        def on_click(
            _checked: bool = False,
            obj: BaseObject = obj,
            spec: PropSpec = spec,
            button: QPushButton = button,
        ) -> None:
            old_value = getattr(obj, spec.key)
            initial = QColor(old_value) if old_value else QColor(_DEFAULT_COLOR)
            color = QColorDialog.getColor(initial, self, "色を選択")
            if not color.isValid():
                return
            new_value = color.name()
            if new_value == old_value:
                return
            self._apply_button_color(button, new_value)
            self._push(SetPropertyCommand(self.scene, obj, spec.key, new_value, old_value))

        button.clicked.connect(on_click)

        def update_widget(
            button: QPushButton = button, obj: BaseObject = obj, spec: PropSpec = spec
        ) -> None:
            if not shiboken6.isValid(button):
                return
            self._apply_button_color(button, getattr(obj, spec.key) or _DEFAULT_COLOR)

        self._updaters.append(update_widget)
        return button

    def _make_color_opt_widget(self, obj: BaseObject, spec: PropSpec) -> QWidget:
        container = QWidget()
        hlayout = QHBoxLayout(container)
        hlayout.setContentsMargins(0, 0, 0, 0)

        current: str | None = getattr(obj, spec.key)
        checkbox = QCheckBox("透明")
        button = QPushButton()
        checkbox.blockSignals(True)
        checkbox.setChecked(current is None)
        checkbox.blockSignals(False)
        button.setEnabled(current is not None)
        self._apply_button_color(button, current or _DEFAULT_COLOR)

        hlayout.addWidget(checkbox)
        hlayout.addWidget(button)

        def on_toggle(
            checked: bool,
            obj: BaseObject = obj,
            spec: PropSpec = spec,
            button: QPushButton = button,
        ) -> None:
            button.setEnabled(not checked)
            if self._rebuilding:
                return
            old_value = getattr(obj, spec.key)
            stored_color = button.property(_COLOR_PROP) or _DEFAULT_COLOR
            new_value: str | None = None if checked else stored_color
            if new_value == old_value:
                return
            self._push(SetPropertyCommand(self.scene, obj, spec.key, new_value, old_value))

        def on_click(
            _checked: bool = False,
            obj: BaseObject = obj,
            spec: PropSpec = spec,
            button: QPushButton = button,
        ) -> None:
            old_value = getattr(obj, spec.key)
            initial = QColor(old_value) if old_value else QColor(_DEFAULT_COLOR)
            color = QColorDialog.getColor(initial, self, "色を選択")
            if not color.isValid():
                return
            new_value = color.name()
            if new_value == old_value:
                return
            self._apply_button_color(button, new_value)
            self._push(SetPropertyCommand(self.scene, obj, spec.key, new_value, old_value))

        checkbox.toggled.connect(on_toggle)
        button.clicked.connect(on_click)

        def update_widget(
            checkbox: QCheckBox = checkbox,
            button: QPushButton = button,
            obj: BaseObject = obj,
            spec: PropSpec = spec,
        ) -> None:
            if not shiboken6.isValid(checkbox) or not shiboken6.isValid(button):
                return
            current: str | None = getattr(obj, spec.key)
            checkbox.blockSignals(True)
            try:
                checkbox.setChecked(current is None)
            finally:
                checkbox.blockSignals(False)
            button.setEnabled(current is not None)
            # current が None（透明）の間はボタンの色/_COLOR_PROP に触れない。
            # ここで _DEFAULT_COLOR を書き込んでしまうと、on_toggle が
            # 「直前の色を覚えておいて透明解除時に復元する」ために読む
            # button.property(_COLOR_PROP) を上書きしてしまい、透明トグルの
            # on/off を往復するだけで元の色を失う（実際に発生した回帰）。
            if current is not None:
                self._apply_button_color(button, current)

        self._updaters.append(update_widget)
        return container

    def _make_enum_widget(self, obj: BaseObject, spec: PropSpec) -> QComboBox:
        combo = QComboBox()
        combo.addItems(list(spec.options))
        combo.blockSignals(True)
        idx = combo.findText(str(getattr(obj, spec.key)))
        combo.setCurrentIndex(idx if idx >= 0 else 0)
        combo.blockSignals(False)

        def on_changed(
            _index: int, obj: BaseObject = obj, spec: PropSpec = spec, combo: QComboBox = combo
        ) -> None:
            if self._rebuilding:
                return
            old_value = getattr(obj, spec.key)
            new_value = combo.currentText()
            if new_value == old_value:
                return
            self._push(SetPropertyCommand(self.scene, obj, spec.key, new_value, old_value))

        combo.currentIndexChanged.connect(on_changed)

        def update_widget(
            combo: QComboBox = combo, obj: BaseObject = obj, spec: PropSpec = spec
        ) -> None:
            if not shiboken6.isValid(combo):
                return
            combo.blockSignals(True)
            try:
                idx = combo.findText(str(getattr(obj, spec.key)))
                combo.setCurrentIndex(idx if idx >= 0 else 0)
            finally:
                combo.blockSignals(False)

        self._updaters.append(update_widget)
        return combo

    def _make_bool_widget(self, obj: BaseObject, spec: PropSpec) -> QCheckBox:
        checkbox = QCheckBox()
        checkbox.blockSignals(True)
        checkbox.setChecked(bool(getattr(obj, spec.key)))
        checkbox.blockSignals(False)

        def on_toggled(checked: bool, obj: BaseObject = obj, spec: PropSpec = spec) -> None:
            if self._rebuilding:
                return
            old_value = getattr(obj, spec.key)
            if checked == old_value:
                return
            self._push(SetPropertyCommand(self.scene, obj, spec.key, checked, old_value))

        checkbox.toggled.connect(on_toggled)

        def update_widget(
            checkbox: QCheckBox = checkbox, obj: BaseObject = obj, spec: PropSpec = spec
        ) -> None:
            if not shiboken6.isValid(checkbox):
                return
            checkbox.blockSignals(True)
            try:
                checkbox.setChecked(bool(getattr(obj, spec.key)))
            finally:
                checkbox.blockSignals(False)

        self._updaters.append(update_widget)
        return checkbox

    def _make_text_widget(self, obj: BaseObject, spec: PropSpec) -> QLineEdit:
        line_edit = QLineEdit()
        line_edit.blockSignals(True)
        line_edit.setText(str(getattr(obj, spec.key)))
        line_edit.blockSignals(False)

        def on_finished(
            obj: BaseObject = obj, spec: PropSpec = spec, le: QLineEdit = line_edit
        ) -> None:
            if self._rebuilding:
                return
            old_value = getattr(obj, spec.key)
            new_value = le.text()
            if new_value == old_value:
                return
            self._push(SetPropertyCommand(self.scene, obj, spec.key, new_value, old_value))

        line_edit.editingFinished.connect(on_finished)

        def update_widget(
            le: QLineEdit = line_edit, obj: BaseObject = obj, spec: PropSpec = spec
        ) -> None:
            if not shiboken6.isValid(le):
                return
            le.blockSignals(True)
            try:
                le.setText(str(getattr(obj, spec.key)))
            finally:
                le.blockSignals(False)

        self._updaters.append(update_widget)
        return line_edit

    # ------------------------------------------------------------------
    # コマンド発行ヘルパ
    # ------------------------------------------------------------------

    def _commit_scalar(self, obj: BaseObject, key: str, new_value: Any, old_value: Any) -> None:
        if key in _GEOMETRY_KEYS:
            cmd = SetGeometryCommand(
                self.scene, obj, {key: new_value}, {key: old_value}, mergeable=True
            )
        else:
            cmd = SetPropertyCommand(self.scene, obj, key, new_value, old_value)
        self._push(cmd)

    def _push(self, cmd: Any) -> None:
        if self.scene.undo_stack is None:
            return
        # push_depth の増減で囲むことで、この push() 中に再入した
        # on_selection_changed() が「破壊的パスは危険」と判定できるようにする
        # （上記 on_selection_changed のドキュメント参照）。
        self._push_depth += 1
        try:
            self.scene.undo_stack.push(cmd)
        finally:
            self._push_depth -= 1

    @staticmethod
    def _apply_button_color(button: QPushButton, color_str: str) -> None:
        button.setText(color_str)
        button.setStyleSheet(f"background-color: {color_str};")
        button.setProperty(_COLOR_PROP, color_str)
