"""レイヤーパネル（P2契約 §4）。

`scene.document.objects` を z 降順（前面が上）で `QListWidget` に表示し、
型アイコン・名前・可視/ロックのトグルを提供する。リスト選択と scene 選択を
双方向に同期する。
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

import shiboken6
from PySide6.QtCore import QSize, Qt
from PySide6.QtWidgets import (
    QAbstractItemView,
    QHBoxLayout,
    QLabel,
    QListWidget,
    QListWidgetItem,
    QSizePolicy,
    QToolButton,
    QVBoxLayout,
    QWidget,
)

from app.commands.commands import SetPropertyCommand
from app.model.objects import BaseObject
from app.ui.theme import icons

if TYPE_CHECKING:
    from app.scene.canvas_scene import CanvasScene

_ID_ROLE = Qt.ItemDataRole.UserRole

# 型アイコン(QLabel pixmap)・表示/ロックボタンの寸法(P2契約 §4)。
_TYPE_ICON_SIZE = 16
_TOGGLE_BUTTON_SIZE = 22
_TOGGLE_ICON_SIZE = 14


class LayerPanel(QWidget):
    """オブジェクト一覧・可視/ロック切替・選択同期を行う最小レイヤーパネル。"""

    def __init__(self, scene: CanvasScene, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.scene = scene
        self._updating = False
        # 現在表示中の行構造の署名（z降順の obj.id タプル）。追加/削除/z順変更で
        # 構造が変わったときだけ検出して再構築するための in-place 更新ガード。
        self._current_signature: tuple[int, ...] = ()

        self._list = QListWidget()
        # scene 側の複数選択（multi モード）をリストにも複数行で映せるようにする。
        # 既定の SingleSelection のままだと _sync_selection_from_scene の
        # setSelected(True) が 1 行ごとに前の選択を打ち消し、最後の 1 行しか
        # ハイライトされない（P2 の複数選択編集導入で顕在化）。
        self._list.setSelectionMode(QAbstractItemView.SelectionMode.ExtendedSelection)

        layout = QVBoxLayout(self)
        layout.addWidget(self._list)

        self._list.itemSelectionChanged.connect(self._on_list_selection_changed)
        self.scene.selectionChanged.connect(self.on_selection_changed)

        self.refresh()

    # ------------------------------------------------------------------
    # 構築・更新
    # ------------------------------------------------------------------

    def refresh(self) -> None:
        """`scene.document.objects` からリストを更新する（z 降順）。

        `undo_stack.indexChanged` はレイヤーパネル自身が push した編集
        （表示/ロックのトグル）の redo でも発火するため、この slot は
        「トグルボタンの toggled シグナルを発火中に自分自身を呼び出す」
        再入を起こし得る。表示中の行構造（id の並び順）が前回と同じであれば
        `self._list.clear()` によるウィジェット破棄（トグル中のボタン自身を
        破棄し use-after-free でクラッシュする — 実際に発生していたバグ）を
        避け、各行を in-place 更新するだけにする。オブジェクトの
        追加/削除/z順変更で構造が変わったときだけ、従来どおり再構築する。
        """
        if not shiboken6.isValid(self) or not shiboken6.isValid(self.scene):
            return
        objects = list(reversed(self.scene.document.objects))
        signature = tuple(obj.id for obj in objects)
        if signature == self._current_signature:
            self._refresh_rows_in_place(objects)
        else:
            self._rebuild_rows(objects)
            self._current_signature = signature
        self._sync_selection_from_scene()

    def _rebuild_rows(self, objects: list[BaseObject]) -> None:
        """リストを clear して `objects`（z 降順・既に並べ替え済み）から再構築する。"""
        self._list.blockSignals(True)
        try:
            self._list.clear()
            for obj in objects:
                item = QListWidgetItem()
                item.setData(_ID_ROLE, obj.id)
                self._list.addItem(item)
                row_widget = self._make_row_widget(obj)
                self._list.setItemWidget(item, row_widget)
                item.setSizeHint(row_widget.sizeHint())
        finally:
            self._list.blockSignals(False)

    def _refresh_rows_in_place(self, objects: list[BaseObject]) -> None:
        """行の追加/削除/並べ替えをせず、各行ウィジェットの表示だけを更新する。"""
        by_id = {obj.id: obj for obj in objects}
        for i in range(self._list.count()):
            item = self._list.item(i)
            obj = by_id.get(item.data(_ID_ROLE))
            if obj is None:
                continue
            row_widget = self._list.itemWidget(item)
            if row_widget is not None:
                self._update_row_widget(row_widget, obj)

    @staticmethod
    def _update_row_widget(row_widget: QWidget, obj: BaseObject) -> None:
        """行ウィジェット内の name/表示/ロック を signals ブロックしつつ再設定する。

        型アイコンは同一 id の行では type が変わらない（型変更 UI が無い）ため
        ここでは再設定しない。
        """
        name_label = getattr(row_widget, "_name_label", None)
        visible_cb = getattr(row_widget, "_visible_cb", None)
        locked_cb = getattr(row_widget, "_locked_cb", None)

        if name_label is not None and shiboken6.isValid(name_label):
            name_label.setText(obj.name or f"{obj.type}#{obj.id}")

        if visible_cb is not None and shiboken6.isValid(visible_cb):
            visible_cb.blockSignals(True)
            try:
                visible_cb.setChecked(obj.visible)
            finally:
                visible_cb.blockSignals(False)

        if locked_cb is not None and shiboken6.isValid(locked_cb):
            locked_cb.blockSignals(True)
            try:
                locked_cb.setChecked(obj.locked)
            finally:
                locked_cb.blockSignals(False)

    def _make_row_widget(self, obj: BaseObject) -> QWidget:
        widget = QWidget()
        hlayout = QHBoxLayout(widget)
        hlayout.setContentsMargins(4, 2, 4, 2)

        type_icon_label = QLabel()
        type_icon_label.setPixmap(
            icons.icon(icons.OBJECT_ICONS.get(obj.type, obj.type)).pixmap(
                _TYPE_ICON_SIZE, _TYPE_ICON_SIZE
            )
        )

        name_label = QLabel(obj.name or f"{obj.type}#{obj.id}")
        name_label.setSizePolicy(QSizePolicy.Policy.Preferred, QSizePolicy.Policy.Preferred)

        # 表示/ロックともに checkable QToolButton。属性名 _visible_cb/_locked_cb は
        # チェックボックス時代からの互換名として維持する（テストが setChecked/
        # isChecked のみを触るため型を変えても影響しない）。
        visible_cb = QToolButton()
        visible_cb.setCheckable(True)
        visible_cb.setAutoRaise(True)
        visible_cb.setFixedSize(_TOGGLE_BUTTON_SIZE, _TOGGLE_BUTTON_SIZE)
        visible_cb.setIconSize(QSize(_TOGGLE_ICON_SIZE, _TOGGLE_ICON_SIZE))
        visible_cb.setIcon(icons.icon_pair("mdi6.eye-outline", "mdi6.eye-off-outline"))
        visible_cb.setToolTip("表示")
        visible_cb.blockSignals(True)
        visible_cb.setChecked(obj.visible)
        visible_cb.blockSignals(False)

        locked_cb = QToolButton()
        locked_cb.setCheckable(True)
        locked_cb.setAutoRaise(True)
        locked_cb.setFixedSize(_TOGGLE_BUTTON_SIZE, _TOGGLE_BUTTON_SIZE)
        locked_cb.setIconSize(QSize(_TOGGLE_ICON_SIZE, _TOGGLE_ICON_SIZE))
        locked_cb.setIcon(icons.icon_pair("mdi6.lock-outline", "mdi6.lock-open-variant-outline"))
        locked_cb.setToolTip("ロック")
        locked_cb.blockSignals(True)
        locked_cb.setChecked(obj.locked)
        locked_cb.blockSignals(False)

        def on_visible_toggled(checked: bool, obj: BaseObject = obj) -> None:
            old_value = obj.visible
            if checked == old_value:
                return
            self._push(SetPropertyCommand(self.scene.document, obj, "visible", checked, old_value))

        def on_locked_toggled(checked: bool, obj: BaseObject = obj) -> None:
            old_value = obj.locked
            if checked == old_value:
                return
            self._push(SetPropertyCommand(self.scene.document, obj, "locked", checked, old_value))

        visible_cb.toggled.connect(on_visible_toggled)
        locked_cb.toggled.connect(on_locked_toggled)

        hlayout.addWidget(type_icon_label)
        hlayout.addWidget(name_label)
        hlayout.addStretch(1)
        hlayout.addWidget(visible_cb)
        hlayout.addWidget(locked_cb)

        # in-place 更新（_update_row_widget）が signal 再接続なしに参照できるよう、
        # 行ウィジェット自身に子ウィジェットを属性として保持する。
        widget._name_label = name_label  # type: ignore[attr-defined]
        widget._visible_cb = visible_cb  # type: ignore[attr-defined]
        widget._locked_cb = locked_cb  # type: ignore[attr-defined]
        return widget

    def _push(self, cmd: Any) -> None:
        if self.scene.undo_stack is not None:
            self.scene.undo_stack.push(cmd)

    # ------------------------------------------------------------------
    # 選択同期
    # ------------------------------------------------------------------

    def on_selection_changed(self) -> None:
        """scene 側の選択変更をリスト選択に反映する。"""
        if not shiboken6.isValid(self) or not shiboken6.isValid(self.scene):
            return
        self._sync_selection_from_scene()

    def _sync_selection_from_scene(self) -> None:
        if not shiboken6.isValid(self) or not shiboken6.isValid(self.scene):
            return
        if self._updating:
            return
        self._updating = True
        try:
            selected_ids = {obj.id for obj in self.scene.selected_objects()}
            self._list.blockSignals(True)
            try:
                for i in range(self._list.count()):
                    item = self._list.item(i)
                    item.setSelected(item.data(_ID_ROLE) in selected_ids)
            finally:
                self._list.blockSignals(False)
        finally:
            self._updating = False

    def _on_list_selection_changed(self) -> None:
        if not shiboken6.isValid(self) or not shiboken6.isValid(self.scene):
            return
        if self._updating:
            return
        self._updating = True
        try:
            selected_ids = {item.data(_ID_ROLE) for item in self._list.selectedItems()}
            # scene.clearSelection()/setSelected() は組込 selectionChanged を自然に
            # 発火させる。_updating の再入ガードにより _sync_selection_from_scene 側の
            # 折り返しは無視されるので、他パネル（PropertyPanel 等）への伝播は
            # 手動 emit なしで機能する。
            self.scene.clearSelection()
            for obj in self.scene.document.objects:
                item = self.scene.item_for(obj)
                if item is not None:
                    item.setSelected(obj.id in selected_ids)
        finally:
            self._updating = False
