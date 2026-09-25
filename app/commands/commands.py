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
    """単一プロパティの set。同一 (obj.id, key) の連続編集は mergeWith で統合する。

    `mergeable=True`（既定）はスピナー/スクラブの連打のようなドラッグ性の
    連続入力向け（既存呼び出しの大半——opacity/stroke_width/corner_radius・
    `_fix_bound_endpoints`・`commit_text` 等——はこの既定に依存している）。
    `mergeable=False` は色スウォッチのメニュー選択・enum コンボ・bool
    チェックボックスのような**離散的な1回きりの選択**専用
    （2026-09-25 レビュー3巡目 finding #9）: 既定のままだと「赤→青」の2回の
    選択が1エントリへ潰れ、Ctrl+Z が赤を飛ばして初期色へ戻ってしまう
    （`SetPropertyWithFollowCommand` の `mergeable=False` と同じ理由・同じ
    `id()==-1` 規約）。
    """

    def __init__(
        self,
        document: Document,
        obj: BaseObject,
        key: str,
        new_value: Any,
        old_value: Any,
        text: str | None = None,
        mergeable: bool = True,
    ) -> None:
        super().__init__(text if text is not None else f"set {key}")
        self._document = document
        self._obj = obj
        self._key = key
        self._new_value = new_value
        self._old_value = old_value
        self._mergeable = mergeable

    def id(self) -> int:
        # mergeWith 対象を安定的に識別するキー。-1 は Qt の「マージ無効」の
        # 特別値なので、31bit にマスクして必ず正の値にする。
        if not self._mergeable:
            return -1
        return hash(("setprop", self._obj.id, self._key)) & 0x7FFFFFFF

    def mergeWith(self, other: QUndoCommand) -> bool:  # noqa: N802 (Qt override)
        if not isinstance(other, SetPropertyCommand):
            return False
        if not self._mergeable or not other._mergeable:
            return False
        if other._obj.id != self._obj.id or other._key != self._key:
            return False
        self._new_value = other._new_value
        self.setObsolete(self._new_value == self._old_value)
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


class SetPropertyWithFollowCommand(QUndoCommand):
    """1プロパティの set と、それに伴う box 追従（math/text）を1つの undo エントリに
    まとめる。

    追従先（math の `follow_math_box`/text の `follow_text_box`）の box は
    「自然サイズ×表示倍率」や「内容に合わせた折返し高さ」の派生値なので、
    `latex`/`font_size`/`text`/`bold`/... の変更に伴う寸法変化を、プロパティ
    変更そのものと同じ undo エントリで添えないと「フォントを変えたのに undo
    したら寸法だけ古いまま」というちぐはぐな状態が起こる。

    `mergeable=True`（既定）は同一 `(obj.id, key)` への連続編集（プロパティ
    パネルのスピナー/スクラブの連打）を1エントリへ統合する（`SetPropertyCommand`
    と同じ `mergeWith` 規約を `follow` 側にも同時に適用する: 最新の新値・
    最初の旧値を保つ）。`mergeable=False` は B/I/U トグル・フォント選択・
    LaTeX/テキスト/名前の確定編集のような**離散的な1回きりの入力**専用
    （2026-09-25 レビュー2巡目 finding #1）: 同じ `_commit_scalar` を経由する
    ためスクラブ向けのマージに意図せず巻き込まれていた（例: B を2回クリック
    すると old=False/new=False の no-op エントリが残り Ctrl+Z が見た目
    何もしなくなる。LaTeX を2回書き換えると中間の式が Ctrl+Z で飛ばされる）。
    `mergeable=False` を渡すとこのコマンドは他のどのコマンドともマージしない
    （`id()` が -1 を返す。Qt の「-1 はマージ無効」規約）。追従が無いキー
    （underline・name 等）でも `follow_new`/`follow_old` が空 dict になる
    だけで、redo/undo は `SetPropertyCommand` と同じ結果になる——
    `SetPropertyCommand` 自体にフラグを足すのではなく、追従し得るキーか
    どうかに関わらずこの1クラスへ経路を寄せることで、ティックによって
    コマンド型が入れ替わりマージが途切れる事態を避けている
    （`app/panels/property_panel.py::PropertyPanel._commit_scalar` 参照）。
    """

    def __init__(
        self,
        document: Document,
        obj: BaseObject,
        key: str,
        new_value: Any,
        old_value: Any,
        follow_new: dict[str, Any],
        follow_old: dict[str, Any],
        text: str,
        mergeable: bool = True,
    ) -> None:
        super().__init__(text)
        self._document = document
        self._obj = obj
        self._key = key
        self._new_value = new_value
        self._old_value = old_value
        self._follow_new = dict(follow_new)
        self._follow_old = dict(follow_old)
        self._mergeable = mergeable

    def id(self) -> int:
        if not self._mergeable:
            return -1
        return hash(("setprop_follow", self._obj.id, self._key)) & 0x7FFFFFFF

    def mergeWith(self, other: QUndoCommand) -> bool:  # noqa: N802 (Qt override)
        if not isinstance(other, SetPropertyWithFollowCommand):
            return False
        if not self._mergeable or not other._mergeable:
            return False
        if other._obj.id != self._obj.id or other._key != self._key:
            return False
        self._new_value = other._new_value
        self._follow_new.update(other._follow_new)
        for k, v in other._follow_old.items():
            self._follow_old.setdefault(k, v)
        # マージの結果、正味の変化が無くなった（値もfollowも往復して元に戻った）
        # スクラブ操作は no-op エントリとして undo スタックに残さない。
        net_unchanged = self._new_value == self._old_value and self._follow_new == self._follow_old
        self.setObsolete(net_unchanged)
        return True

    def redo(self) -> None:
        self._document.set_values(self._obj, {self._key: self._new_value, **self._follow_new})

    def undo(self) -> None:
        self._document.set_values(self._obj, {self._key: self._old_value, **self._follow_old})


class TranslateGroupCommand(QUndoCommand):
    """グループ全体 X/Y の平行移動を1つの undo エントリにまとめる。

    キーはメンバー id の集合のみ（軸は問わない）。単一オブジェクトの
    `SetGeometryCommand` が x のティックと y のティックを同じ
    `("setgeom", obj.id)` キーで1エントリへ統合するのと同じ規則を、
    グループ全メンバーぶんまとめて行う（2026-09-25 レビュー2巡目 finding #3:
    以前はティックごとに `beginMacro`/`endMacro` していたため、`QUndoStack`
    がマクロ同士を絶対に `mergeWith` しない性質により、グループの X/Y スピンの
    矢印を5回押すと5個の独立した undo エントリになっていた）。他のコマンドが
    間に push されれば（Qt は隣接するトップとしかマージを試みない）マージは
    自動的に途切れる。
    """

    def __init__(
        self,
        document: Document,
        entries: list[tuple[BaseObject, dict[str, Any], dict[str, Any]]],
        text: str,
    ) -> None:
        super().__init__(text)
        self._document = document
        self._objs = [obj for obj, _new, _old in entries]
        self._new_geom: dict[int, dict[str, Any]] = {
            obj.id: dict(new_geom) for obj, new_geom, _old in entries
        }
        self._old_geom: dict[int, dict[str, Any]] = {
            obj.id: dict(old_geom) for obj, _new, old_geom in entries
        }
        self._member_ids: frozenset[int] = frozenset(self._new_geom)

    def id(self) -> int:
        return hash(("translate_group", self._member_ids)) & 0x7FFFFFFF

    def mergeWith(self, other: QUndoCommand) -> bool:  # noqa: N802 (Qt override)
        if not isinstance(other, TranslateGroupCommand):
            return False
        if other._member_ids != self._member_ids:
            return False
        for obj_id, new_geom in other._new_geom.items():
            self._new_geom[obj_id].update(new_geom)
        for obj_id, old_geom in other._old_geom.items():
            for k, v in old_geom.items():
                self._old_geom[obj_id].setdefault(k, v)
        all_same = all(
            self._new_geom[obj_id] == self._old_geom[obj_id] for obj_id in self._new_geom
        )
        self.setObsolete(all_same)
        return True

    def redo(self) -> None:
        for obj in self._objs:
            self._document.set_values(obj, self._new_geom[obj.id])

    def undo(self) -> None:
        for obj in self._objs:
            self._document.set_values(obj, self._old_geom[obj.id])


class SetMultiPropertyCommand(QUndoCommand):
    """複数オブジェクトへの同種プロパティ変更を1つの undo エントリにまとめる
    （プロパティパネルの複数選択スピナー用、2026-09-25 レビュー3巡目 finding #8）。

    `TranslateGroupCommand` と同じ発想: 以前はスピンのティックごとに
    `_push_macro`（`beginMacro`/`endMacro`）を使っていたが、`QUndoStack` は
    マクロ同士を絶対にマージしないため、矢印を5回押すと5個の独立した undo
    エントリになっていた（単一選択の `SetPropertyWithFollowCommand`/
    `SetGeometryCommand(mergeable=True)` と挙動が食い違う）。

    `entries` は `(obj, new_values, old_values)` のリスト（`new_values`/
    `old_values` は同じキー集合の dict。box 追従を伴うキーは追従先のキーも
    ここに同居させる——呼び出し側が `box_follow_geometry` の結果を
    `new_values`/`old_values` へ事前にマージしておくこと）。

    `merge_key` は「どのプロパティ（または幾何全体）の変更か」を区別する
    識別子。`entries` に現れるオブジェクト id 集合と合わせてマージ対象を
    決めるため、font_size のティックに続けて stroke_width をいじっても
    別エントリのままになる（メンバー集合は同じでも merge_key が違うため）。

    メンバー集合は呼び出し側が**毎ティック同じ**であることを前提にする
    （値が既に一致しているオブジェクトを除外せず、`new_values == old_values`
    のエントリとして含めること）。除外してしまうと選択が混在した状態から
    始まった最初のティックでメンバー集合が変わり、以後のティックと
    マージできなくなる。
    """

    def __init__(
        self,
        document: Document,
        merge_key: str,
        entries: list[tuple[BaseObject, dict[str, Any], dict[str, Any]]],
        text: str,
    ) -> None:
        super().__init__(text)
        self._document = document
        self._merge_key = merge_key
        self._objs = [obj for obj, _new, _old in entries]
        self._new_values: dict[int, dict[str, Any]] = {
            obj.id: dict(new_values) for obj, new_values, _old in entries
        }
        self._old_values: dict[int, dict[str, Any]] = {
            obj.id: dict(old_values) for obj, _new, old_values in entries
        }
        self._member_ids: frozenset[int] = frozenset(self._new_values)

    def id(self) -> int:
        return hash(("multiprop", self._merge_key, self._member_ids)) & 0x7FFFFFFF

    def mergeWith(self, other: QUndoCommand) -> bool:  # noqa: N802 (Qt override)
        if not isinstance(other, SetMultiPropertyCommand):
            return False
        if other._merge_key != self._merge_key or other._member_ids != self._member_ids:
            return False
        for obj_id, new_values in other._new_values.items():
            self._new_values[obj_id].update(new_values)
        for obj_id, old_values in other._old_values.items():
            for k, v in old_values.items():
                self._old_values[obj_id].setdefault(k, v)
        all_same = all(
            self._new_values[obj_id] == self._old_values[obj_id] for obj_id in self._new_values
        )
        self.setObsolete(all_same)
        return True

    def redo(self) -> None:
        for obj in self._objs:
            self._document.set_values(obj, self._new_values[obj.id])

    def undo(self) -> None:
        for obj in self._objs:
            self._document.set_values(obj, self._old_values[obj.id])


class SetStylesCommand(QUndoCommand):
    """`Document.styles`（名前付きスタイルの登録簿）を丸ごと差し替える。

    登録簿は小さい（名前 → 見た目キーの束）ので差分は取らず、全体を持つ。
    `deepcopy` して保持するのは `SetArtboardCommand` と同じ理由
    （呼び出し側の参照を後から変更されても影響を受けないため）。
    """

    def __init__(
        self,
        document: Document,
        new_styles: dict[str, dict[str, Any]],
        old_styles: dict[str, dict[str, Any]],
        text: str = "スタイル登録",
    ) -> None:
        super().__init__(text)
        self._document = document
        self._new_styles = copy.deepcopy(new_styles)
        self._old_styles = copy.deepcopy(old_styles)

    def redo(self) -> None:
        self._document.set_styles(copy.deepcopy(self._new_styles))

    def undo(self) -> None:
        self._document.set_styles(copy.deepcopy(self._old_styles))
