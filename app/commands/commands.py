"""Undo/Redo コマンド群（§5）。モデル変更の唯一の入口。

各コマンドは `Document` を変更したのち、`CanvasScene` の
`add_item_for`/`remove_item_for`/`sync_item` でビューを同期する。
"""

from __future__ import annotations

import copy
from typing import TYPE_CHECKING, Any

from PySide6.QtGui import QUndoCommand

if TYPE_CHECKING:
    from app.model.document import Artboard
    from app.model.objects import BaseObject
    from app.scene.canvas_scene import CanvasScene


class AddObjectCommand(QUndoCommand):
    """オブジェクトを Document に追加し、対応する item を生成する。"""

    def __init__(self, scene: CanvasScene, obj: BaseObject, text: str = "add object") -> None:
        super().__init__(text)
        self._scene = scene
        self._obj = obj

    def redo(self) -> None:
        self._scene.document.add_object(self._obj)
        self._scene.add_item_for(self._obj)

    def undo(self) -> None:
        self._scene.remove_item_for(self._obj)
        self._scene.document.remove_object(self._obj)


class RemoveObjectCommand(QUndoCommand):
    """オブジェクトを Document から削除する。undo で元の z 位置に復元する。"""

    def __init__(self, scene: CanvasScene, obj: BaseObject, text: str = "remove object") -> None:
        super().__init__(text)
        self._scene = scene
        self._obj = obj
        self._index = scene.document.index_of(obj)

    def redo(self) -> None:
        self._scene.remove_item_for(self._obj)
        self._scene.document.remove_object(self._obj)

    def undo(self) -> None:
        self._scene.document.add_object(self._obj, index=self._index)
        self._scene.add_item_for(self._obj)


class SetGeometryCommand(QUndoCommand):
    """幾何プロパティ（x/y/width/height/rotation または p1/p2）を差分適用する。

    `mergeable=True` の場合のみ同一 obj への連続編集が `mergeWith` で統合される
    （プロパティパネルの連続スピナー編集向け）。既定 `mergeable=False` では
    select ツール/ハンドルの各ドラッグ移動が独立した undo エントリとして残る。
    """

    def __init__(
        self,
        scene: CanvasScene,
        obj: BaseObject,
        new_geom: dict[str, Any],
        old_geom: dict[str, Any],
        text: str = "move/resize",
        mergeable: bool = False,
    ) -> None:
        super().__init__(text)
        self._scene = scene
        self._obj = obj
        self._new_geom = dict(new_geom)
        self._old_geom = dict(old_geom)
        self._mergeable = mergeable

    def id(self) -> int:
        if not self._mergeable:
            return -1
        return hash(("setgeom", self._obj.id)) & 0x7FFFFFFF

    def mergeWith(self, other: QUndoCommand) -> bool:  # noqa: N802 (Qt override)
        if not isinstance(other, SetGeometryCommand):
            return False
        if not self._mergeable or not other._mergeable:
            return False
        if other._obj.id != self._obj.id:
            return False
        self._new_geom.update(other._new_geom)
        for key, value in other._old_geom.items():
            self._old_geom.setdefault(key, value)
        return True

    def redo(self) -> None:
        for key, value in self._new_geom.items():
            setattr(self._obj, key, value)
        self._scene.sync_item(self._obj)

    def undo(self) -> None:
        for key, value in self._old_geom.items():
            setattr(self._obj, key, value)
        self._scene.sync_item(self._obj)


class SetPropertyCommand(QUndoCommand):
    """単一プロパティの set。同一 (obj.id, key) の連続編集は mergeWith で統合する。"""

    def __init__(
        self,
        scene: CanvasScene,
        obj: BaseObject,
        key: str,
        new_value: Any,
        old_value: Any,
        text: str | None = None,
    ) -> None:
        super().__init__(text if text is not None else f"set {key}")
        self._scene = scene
        self._obj = obj
        self._key = key
        self._new_value = new_value
        self._old_value = old_value

    def id(self) -> int:
        # mergeWith 対象を安定的に識別するキー。-1 は Qt の「マージ無効」の
        # 特別値なので、31bit にマスクして必ず正の値にする。
        return hash(("setprop", self._obj.id, self._key)) & 0x7FFFFFFF

    def mergeWith(self, other: QUndoCommand) -> bool:  # noqa: N802 (Qt override)
        if not isinstance(other, SetPropertyCommand):
            return False
        if other._obj.id != self._obj.id or other._key != self._key:
            return False
        self._new_value = other._new_value
        return True

    def redo(self) -> None:
        setattr(self._obj, self._key, self._new_value)
        self._scene.sync_item(self._obj)

    def undo(self) -> None:
        setattr(self._obj, self._key, self._old_value)
        self._scene.sync_item(self._obj)


class ReorderCommand(QUndoCommand):
    """z順（`Document.objects` の配列位置）を変更する。undo で元の位置に戻す。"""

    def __init__(
        self,
        scene: CanvasScene,
        obj: BaseObject,
        new_index: int,
        old_index: int,
        text: str = "reorder",
    ) -> None:
        super().__init__(text)
        self._scene = scene
        self._obj = obj
        self._new_index = new_index
        self._old_index = old_index

    def redo(self) -> None:
        self._scene.document.move_to_index(self._obj, self._new_index)
        self._scene.resync_z()

    def undo(self) -> None:
        self._scene.document.move_to_index(self._obj, self._old_index)
        self._scene.resync_z()


class GroupCommand(QUndoCommand):
    """選択オブジェクト群に共通の `group_id` を設定する（グループ化）。"""

    def __init__(
        self,
        scene: CanvasScene,
        objs: list[BaseObject],
        group_id: int,
        text: str = "group",
    ) -> None:
        super().__init__(text)
        self._scene = scene
        self._objs = list(objs)
        self._group_id = group_id
        self._old_group_ids: list[int | None] = [obj.group_id for obj in self._objs]

    def redo(self) -> None:
        for obj in self._objs:
            obj.group_id = self._group_id
            self._scene.sync_item(obj)

    def undo(self) -> None:
        for obj, old_group_id in zip(self._objs, self._old_group_ids, strict=True):
            obj.group_id = old_group_id
            self._scene.sync_item(obj)


class UngroupCommand(QUndoCommand):
    """選択オブジェクト群の `group_id` を解除する（`None` に戻す）。"""

    def __init__(
        self,
        scene: CanvasScene,
        objs: list[BaseObject],
        text: str = "ungroup",
    ) -> None:
        super().__init__(text)
        self._scene = scene
        self._objs = list(objs)
        self._old_group_ids: list[int | None] = [obj.group_id for obj in self._objs]

    def redo(self) -> None:
        for obj in self._objs:
            obj.group_id = None
            self._scene.sync_item(obj)

    def undo(self) -> None:
        for obj, old_group_id in zip(self._objs, self._old_group_ids, strict=True):
            obj.group_id = old_group_id
            self._scene.sync_item(obj)


class SetArtboardCommand(QUndoCommand):
    """`Document.artboard` を差し替える（物理サイズプリセット適用等）。

    渡された `Artboard` はいずれも `deepcopy` して保持する（呼び出し側の
    ミュータブルな参照を後から変更されても影響を受けないようにするため）。
    """

    def __init__(
        self,
        scene: CanvasScene,
        new_artboard: Artboard,
        old_artboard: Artboard,
        text: str = "artboard",
    ) -> None:
        super().__init__(text)
        self._scene = scene
        self._new_artboard = copy.deepcopy(new_artboard)
        self._old_artboard = copy.deepcopy(old_artboard)

    def redo(self) -> None:
        self._scene.document.artboard = copy.deepcopy(self._new_artboard)
        self._scene.apply_artboard_change()

    def undo(self) -> None:
        self._scene.document.artboard = copy.deepcopy(self._old_artboard)
        self._scene.apply_artboard_change()
