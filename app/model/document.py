"""Document モデル: アートボードとオブジェクトリストの単一の真実源（Qt 非依存）。"""

from __future__ import annotations

import uuid
from dataclasses import dataclass, field
from typing import Any, Protocol

from app.model.objects import BaseObject


class DocumentListener(Protocol):
    """Document の変更通知を受け取るリスナー（Qt 非依存）。

    通知はリスナー登録順に同期呼び出しされる。リスナーの実装はこのコール
    バックの中から Document を再変更しない前提とする（再入は想定外）。
    """

    def on_object_added(self, obj: BaseObject, index: int) -> None: ...

    def on_object_removed(self, obj: BaseObject) -> None: ...

    def on_object_changed(self, obj: BaseObject, keys: tuple[str, ...]) -> None: ...

    def on_order_changed(self) -> None: ...

    def on_artboard_changed(self) -> None: ...


@dataclass(kw_only=True)
class Physical:
    """アートボードの物理寸法設定（mm/DPI）。"""

    width_mm: float = 170.0
    target_dpi: int = 300

    def to_dict(self) -> dict[str, Any]:
        return {"width_mm": self.width_mm, "target_dpi": self.target_dpi}

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> Physical:
        return cls(
            width_mm=d.get("width_mm", 170.0),
            target_dpi=d.get("target_dpi", 300),
        )


@dataclass(kw_only=True)
class Artboard:
    """単ページのキャンバス設定。"""

    width_px: int = 1920
    height_px: int = 1080
    physical: Physical = field(default_factory=Physical)
    background: str = "#FFFFFF"

    def to_dict(self) -> dict[str, Any]:
        return {
            "width_px": self.width_px,
            "height_px": self.height_px,
            "physical": self.physical.to_dict(),
            "background": self.background,
        }

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> Artboard:
        physical_d = d.get("physical", {})
        return cls(
            width_px=d.get("width_px", 1920),
            height_px=d.get("height_px", 1080),
            physical=Physical.from_dict(physical_d),
            background=d.get("background", "#FFFFFF"),
        )


class Document:
    """シーングラフ本体（アートボード＋オブジェクトのリスト）。

    `objects` の配列順 = z順（後ろほど前面、§6）。
    """

    def __init__(self, artboard: Artboard | None = None) -> None:
        self.version: int = 1
        self.artboard: Artboard = artboard if artboard is not None else Artboard()
        self.objects: list[BaseObject] = []
        self.next_id: int = 1
        # このインスタンスの一意 ID。プロジェクトを開き直すと別値になる。
        # 外部クライアント（エージェント）が「別ドキュメントに差し替わった」を検知するために使う。
        # シリアライズしない。
        self.uid: str = uuid.uuid4().hex
        # 変更のたびに +1 する単調増加カウンタ。外部クライアントの陳腐化検知・
        # 楽観的同時実行制御（expect_revision）に使う。シリアライズしない。
        # ※ `version` は project.json のスキーマ版であり別物。
        self.revision: int = 0
        # プロジェクトディレクトリの絶対パス（画像 src の相対パス解決の基点）。
        # シリアライズしない（to_dict/from_dict に含めない）。
        self.base_dir: str | None = None
        # 変更通知リスナー（Qt 非依存）。シリアライズには一切含めない。
        self._listeners: list[DocumentListener] = []

    def add_listener(self, listener: DocumentListener) -> None:
        """変更通知リスナーを登録する。"""
        self._listeners.append(listener)

    def remove_listener(self, listener: DocumentListener) -> None:
        """変更通知リスナーの登録を解除する。未登録でもエラーにしない。"""
        if listener in self._listeners:
            self._listeners.remove(listener)

    def _begin_change(self) -> list[DocumentListener]:
        """変更を 1 件記録し、通知先リスナーのスナップショットを返す。

        `revision` をインクリメントし、`_listeners` のコピーを返す。コピーを返すのは
        通知中にリスナーが登録解除される（`CanvasScene.close` 等）ケースで
        走査が壊れないようにするため。
        """
        self.revision += 1
        return list(self._listeners)

    def new_id(self) -> int:
        """未使用の id を払い出す。"""
        oid = self.next_id
        self.next_id += 1
        return oid

    def add_object(self, obj: BaseObject, index: int | None = None) -> None:
        """オブジェクトを追加する。index 省略時は末尾（最前面）に追加。z を再正規化する。"""
        if index is None:
            self.objects.append(obj)
            inserted_index = len(self.objects) - 1
        else:
            self.objects.insert(index, obj)
            inserted_index = index
        self.normalize_z()
        for listener in self._begin_change():
            listener.on_object_added(obj, inserted_index)

    def remove_object(self, obj: BaseObject) -> None:
        """オブジェクトを削除する。"""
        self.objects.remove(obj)
        self.normalize_z()
        for listener in self._begin_change():
            listener.on_object_removed(obj)

    def object_by_id(self, oid: int) -> BaseObject | None:
        """id からオブジェクトを検索する。見つからなければ None。"""
        for obj in self.objects:
            if obj.id == oid:
                return obj
        return None

    def index_of(self, obj: BaseObject) -> int:
        """オブジェクトの現在のインデックス（z順位置）を返す。"""
        return self.objects.index(obj)

    def move_to_index(self, obj: BaseObject, index: int) -> None:
        """オブジェクトの z順（配列上の位置）を変更する。"""
        self.objects.remove(obj)
        self.objects.insert(index, obj)
        self.normalize_z()
        for listener in self._begin_change():
            listener.on_order_changed()

    def set_values(self, obj: BaseObject, values: dict[str, Any]) -> dict[str, Any]:
        """`obj` に `values` を setattr で一括適用し、適用前の旧値 dict を返す。

        末尾で `on_object_changed(obj, tuple(values.keys()))` を 1 回だけ通知する。
        """
        old_values: dict[str, Any] = {key: getattr(obj, key) for key in values}
        for key, value in values.items():
            setattr(obj, key, value)
        for listener in self._begin_change():
            listener.on_object_changed(obj, tuple(values.keys()))
        return old_values

    def set_artboard(self, artboard: Artboard) -> None:
        """アートボードを差し替える（deepcopy はしない。呼び出し側の責務）。"""
        self.artboard = artboard
        for listener in self._begin_change():
            listener.on_artboard_changed()

    def normalize_z(self) -> None:
        """各オブジェクトの `z` フィールドを配列インデックスに合わせて再設定する。

        `obj.z` は配列順から導出される派生値（シリアライズ用キャッシュ）であり、
        真実源は常に `objects` の配列順である。読み取りコードは `index_of` や
        `reversed(objects)` など配列順を直接使い、`obj.z` を読んではならない。
        """
        for i, obj in enumerate(self.objects):
            obj.z = i

    def to_dict(self) -> dict[str, Any]:
        """§6 の project.json スキーマに従って辞書化する。"""
        return {
            "version": self.version,
            "artboard": self.artboard.to_dict(),
            "objects": [obj.to_dict() for obj in self.objects],
            "next_id": self.next_id,
        }

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> Document:
        """辞書から Document を復元する。"""
        artboard = Artboard.from_dict(d.get("artboard", {}))
        doc = cls(artboard=artboard)
        doc.version = d.get("version", 1)
        doc.objects = [BaseObject.from_dict(od) for od in d.get("objects", [])]
        doc.next_id = d.get("next_id", 1)
        doc.normalize_z()
        return doc
