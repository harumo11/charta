"""Document モデル: アートボードとオブジェクトリストの単一の真実源（Qt 非依存）。"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from app.model.objects import BaseObject


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
        # プロジェクトディレクトリの絶対パス（画像 src の相対パス解決の基点）。
        # シリアライズしない（to_dict/from_dict に含めない）。
        self.base_dir: str | None = None

    def new_id(self) -> int:
        """未使用の id を払い出す。"""
        oid = self.next_id
        self.next_id += 1
        return oid

    def add_object(self, obj: BaseObject, index: int | None = None) -> None:
        """オブジェクトを追加する。index 省略時は末尾（最前面）に追加。z を再正規化する。"""
        if index is None:
            self.objects.append(obj)
        else:
            self.objects.insert(index, obj)
        self.normalize_z()

    def remove_object(self, obj: BaseObject) -> None:
        """オブジェクトを削除する。"""
        self.objects.remove(obj)
        self.normalize_z()

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

    def normalize_z(self) -> None:
        """各オブジェクトの `z` フィールドを配列インデックスに合わせて再設定する。"""
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
