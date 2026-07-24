"""Undo/Redo コマンド群（§5）。モデル変更の唯一の入口。

コマンドは `Document` のみに依存し、ビュー同期は Document の変更通知
（`DocumentListener`、契約 Stage A）が担う。コマンドは `Document` の
通知内蔵メソッド（`add_object`/`remove_object`/`move_to_index`/
`set_values`/`set_artboard`）を呼ぶのみで、`CanvasScene` を直接操作しない。
"""

from __future__ import annotations

import copy
from typing import TYPE_CHECKING, Any

from PySide6.QtGui import QUndoCommand

if TYPE_CHECKING:
    from app.model.document import Artboard, Document
    from app.model.objects import BaseObject


class AddObjectCommand(QUndoCommand):
    """オブジェクトを Document に追加する（ビュー同期は Document 通知経由）。"""

    def __init__(self, document: Document, obj: BaseObject, text: str = "add object") -> None:
        super().__init__(text)
        self._document = document
        self._obj = obj

    def redo(self) -> None:
        self._document.add_object(self._obj)

    def undo(self) -> None:
        self._document.remove_object(self._obj)


class RemoveObjectCommand(QUndoCommand):
    """オブジェクトを Document から削除する。undo で元の z 位置に復元する。"""

    def __init__(self, document: Document, obj: BaseObject, text: str = "remove object") -> None:
        super().__init__(text)
        self._document = document
        self._obj = obj
        self._index = document.index_of(obj)

    def redo(self) -> None:
        self._document.remove_object(self._obj)

    def undo(self) -> None:
        self._document.add_object(self._obj, index=self._index)


class SetGeometryCommand(QUndoCommand):
    """幾何プロパティ（x/y/width/height/rotation または p1/p2）を差分適用する。

    `mergeable=True` の場合のみ同一 obj への連続編集が `mergeWith` で統合される
    （プロパティパネルの連続スピナー編集向け）。既定 `mergeable=False` では
    select ツール/ハンドルの各ドラッグ移動が独立した undo エントリとして残る。
    """

    def __init__(
        self,
        document: Document,
        obj: BaseObject,
        new_geom: dict[str, Any],
        old_geom: dict[str, Any],
        text: str = "move/resize",
        mergeable: bool = False,
    ) -> None:
        super().__init__(text)
        self._document = document
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
        self._document.set_values(self._obj, self._new_geom)

    def undo(self) -> None:
        self._document.set_values(self._obj, self._old_geom)


class SetPropertyCommand(QUndoCommand):
    """単一プロパティの set。同一 (obj.id, key) の連続編集は mergeWith で統合する。"""

    def __init__(
        self,
        document: Document,
        obj: BaseObject,
        key: str,
        new_value: Any,
        old_value: Any,
        text: str | None = None,
    ) -> None:
        super().__init__(text if text is not None else f"set {key}")
        self._document = document
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
        self._document.set_values(self._obj, {self._key: self._new_value})

    def undo(self) -> None:
        self._document.set_values(self._obj, {self._key: self._old_value})


class ReorderCommand(QUndoCommand):
    """z順（`Document.objects` の配列位置）を変更する。undo で元の位置に戻す。"""

    def __init__(
        self,
        document: Document,
        obj: BaseObject,
        new_index: int,
        old_index: int,
        text: str = "reorder",
    ) -> None:
        super().__init__(text)
        self._document = document
        self._obj = obj
        self._new_index = new_index
        self._old_index = old_index

    def redo(self) -> None:
        self._document.move_to_index(self._obj, self._new_index)

    def undo(self) -> None:
        self._document.move_to_index(self._obj, self._old_index)


class GroupCommand(QUndoCommand):
    """選択オブジェクト群に共通の `group_id` を設定する（グループ化）。"""

    def __init__(
        self,
        document: Document,
        objs: list[BaseObject],
        group_id: int,
        text: str = "group",
    ) -> None:
        super().__init__(text)
        self._document = document
        self._objs = list(objs)
        self._group_id = group_id
        self._old_group_ids: list[int | None] = [obj.group_id for obj in self._objs]

    def redo(self) -> None:
        for obj in self._objs:
            self._document.set_values(obj, {"group_id": self._group_id})

    def undo(self) -> None:
        for obj, old_group_id in zip(self._objs, self._old_group_ids, strict=True):
            self._document.set_values(obj, {"group_id": old_group_id})


class UngroupCommand(QUndoCommand):
    """選択オブジェクト群の `group_id` を解除する（`None` に戻す）。"""

    def __init__(
        self,
        document: Document,
        objs: list[BaseObject],
        text: str = "ungroup",
    ) -> None:
        super().__init__(text)
        self._document = document
        self._objs = list(objs)
        self._old_group_ids: list[int | None] = [obj.group_id for obj in self._objs]

    def redo(self) -> None:
        for obj in self._objs:
            self._document.set_values(obj, {"group_id": None})

    def undo(self) -> None:
        for obj, old_group_id in zip(self._objs, self._old_group_ids, strict=True):
            self._document.set_values(obj, {"group_id": old_group_id})


class SetArtboardCommand(QUndoCommand):
    """`Document.artboard` を差し替える（物理サイズプリセット適用等）。

    渡された `Artboard` はいずれも `deepcopy` して保持する（呼び出し側の
    ミュータブルな参照を後から変更されても影響を受けないようにするため）。
    """

    def __init__(
        self,
        document: Document,
        new_artboard: Artboard,
        old_artboard: Artboard,
        text: str = "artboard",
    ) -> None:
        super().__init__(text)
        self._document = document
        self._new_artboard = copy.deepcopy(new_artboard)
        self._old_artboard = copy.deepcopy(old_artboard)

    def redo(self) -> None:
        self._document.set_artboard(copy.deepcopy(self._new_artboard))

    def undo(self) -> None:
        self._document.set_artboard(copy.deepcopy(self._old_artboard))
