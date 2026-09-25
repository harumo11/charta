"""Document モデル: アートボードとオブジェクトリストの単一の真実源（Qt 非依存）。"""

from __future__ import annotations

import uuid
from dataclasses import dataclass, field, replace
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
        # 名前付きスタイル（見た目キーの束）。project.json に保存する。
        # 描画には一切影響しないので、`DocumentListener` への通知は行わない
        # （リスナーに新しいコールバックを足すと既存の実装が壊れる）。
        self.styles: dict[str, dict[str, Any]] = {}
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

    def selectable_group_members(self, group_id: int) -> list[BaseObject]:
        """`group_id` のメンバーのうち、キャンバス上で実際に選択され得るものを返す。

        「ロックされていない」だけでなく `visible` も要求する（レビュー finding #7）。
        Qt の `QGraphicsItem.setSelected` は非表示アイテムに対しては何もしないため、
        「グループ全体が選択されている」を「不可視でない全メンバーが選択に含まれる」で
        判定する側（`ToolManager._select_press` のグループ内個別編集クリック候補判定・
        `PropertyPanel._whole_group_selection`・`CanvasScene.select_exactly` の部分集合
        判定・`CanvasView._handle_group_entry_key` の Esc 復帰）は、判定対象の集合を
        ここに揃える必要がある。ここを「ロックのみ」のままにすると、非表示メンバーを
        含むグループは「全メンバー選択」に決して到達できず、以後グループへ入れない／
        複数選択の X/Y フォームへ落ちて平行移動できない（書くと崩壊する）という壊れ方
        をする。
        """
        return [
            obj
            for obj in self.objects
            if getattr(obj, "group_id", None) == group_id and not obj.locked and obj.visible
        ]

    def movable_group_members(self, group_id: int) -> list[BaseObject]:
        """`group_id` のメンバーのうち、グループとして剛体移動する対象を返す。

        「非表示メンバーの扱い」の主セッション決定（要望10 追加決定、2026-09-25、
        Option A: PowerPoint 式）: ロックされていない非表示メンバーは、選択も
        当たり判定もできない（`selectable_group_members` が対象外にする）が、
        グループの構成要素であることに変わりはなく、移動・複製・貼付では可視
        メンバーと剛体で一緒に動く。ロックされたメンバーは今までどおり動かない
        （§9.1 の「ロックされたメンバーは今までどおりの意味を保つ」の対象）。

        `selectable_group_members` が「選べる／入れる」の判定に使う集合、こちらが
        「動く」の判定に使う集合——同じ `not obj.locked` を共有しつつ `visible` の
        有無だけが違う。移動・スナップ・複製・貼付・削除・グループ化・z順操作
        （前面化/背面化/一つ前/一つ後ろ）・プロパティパネルのグループ X/Y 平行移動は
        すべてこちらを使う（人間の操作経路は `CanvasScene.rigid_group_targets`
        経由。レビュー3巡目 finding #5/#6/#10/#14 で削除・グループ化・z順にも
        拡張——以前はこれらだけ `scene.selected_objects()` を生のまま使っていたため、
        非表示メンバーを削除で無言孤立させる／Ctrl+G で置き去りにする／前面化で
        古い z のまま取り残す、という Option A の抜け穴になっていた）。
        """
        return [
            obj
            for obj in self.objects
            if getattr(obj, "group_id", None) == group_id and not obj.locked
        ]

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

    def set_styles(self, styles: dict[str, dict[str, Any]]) -> dict[str, dict[str, Any]]:
        """名前付きスタイルの登録簿を丸ごと差し替え、旧値を返す。

        `revision` を進めるので `expect_revision` の楽観ロックが正しく効く。
        描画に影響しないためリスナーへの通知はしない（`DocumentListener` の
        プロトコルを増やすと `CanvasScene` 等の既存実装が全部壊れる）。
        """
        old = {name: dict(values) for name, values in self.styles.items()}
        self._begin_change()
        self.styles = {name: dict(values) for name, values in styles.items()}
        return old

    def to_dict(self) -> dict[str, Any]:
        """§6 の project.json スキーマに従って辞書化する。"""
        payload: dict[str, Any] = {
            "version": self.version,
            "artboard": self.artboard.to_dict(),
            "objects": [obj.to_dict() for obj in self.objects],
            "next_id": self.next_id,
        }
        if self.styles:
            # 空なら書かない（既存プロジェクトの diff を汚さない）。
            payload["styles"] = {name: dict(values) for name, values in self.styles.items()}
        return payload

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> Document:
        """辞書から Document を復元する。"""
        artboard = Artboard.from_dict(d.get("artboard", {}))
        doc = cls(artboard=artboard)
        doc.version = d.get("version", 1)
        doc.objects = [BaseObject.from_dict(od) for od in d.get("objects", [])]
        doc.next_id = d.get("next_id", 1)
        # 壊れた値には寛容にする（styles を知らない版が書いた project.json も読める）。
        raw_styles = d.get("styles")
        if isinstance(raw_styles, dict):
            doc.styles = {
                str(name): dict(values)
                for name, values in raw_styles.items()
                if isinstance(values, dict)
            }
        doc.normalize_z()
        return doc


#: 1 インチあたりのミリメートル数（px↔mm 変換の唯一の真実源）。
MM_PER_INCH = 25.4

#: アートボード px の値域。`app/prefs.py` の ARTBOARD_PX_MIN/MAX と同じ値
#: （`app/prefs.py` は app 配下を一切 import しない独立モジュールなので定数は共有しない。
#: 値を変えるときは両方直すこと）。
ARTBOARD_PX_MIN = 1
ARTBOARD_PX_MAX = 20000


def px_from_mm(width_mm: float, target_dpi: int) -> int:
    """物理サイズ(mm)と DPI から px を算出する（`mm/25.4*dpi` の唯一の真実源）。

    既存の 3 箇所（`artboard_presets.preset_px_size` / `png_exporter.artboard_pixel_size` /
    `schema.artboard_info`）が個別に書いていた同一の式を一本化する。丸めは
    従来どおり四則演算の直後の `round` のみで、ここで値域クランプはしない
    （呼び出し側の既存の挙動を 1px も変えないため）。
    """
    return round(width_mm / MM_PER_INCH * target_dpi)


def mm_from_px(width_px: float, target_dpi: int) -> float:
    """px と DPI から物理サイズ(mm)を算出する（`px_from_mm` の逆演算）。

    `target_dpi <= 0` は「DPI 未設定」を意味しうる壊れた入力であり、0 除算を
    避けるため 0.0 を返す（例外を投げて呼び出し側を落とさない）。
    """
    if target_dpi <= 0:
        return 0.0
    return width_px / target_dpi * MM_PER_INCH


def clamp_artboard_px(value: float) -> int:
    """アートボード px の値を `[ARTBOARD_PX_MIN, ARTBOARD_PX_MAX]` に丸めてクランプする。"""
    clamped = max(float(ARTBOARD_PX_MIN), min(float(ARTBOARD_PX_MAX), value))
    return int(round(clamped))


def artboard_with_pixel_size(artboard: Artboard, width_px: float, height_px: float) -> Artboard:
    """px 指定でアートボードを差し替えた新しい `Artboard` を返す（元は破壊しない）。

    `physical.target_dpi` は維持し、`physical.width_mm` を
    `mm_from_px(new_width_px, dpi)` で再計算する。`background` は引き継ぐ
    （`dataclasses.replace` が明示しないフィールドをそのまま保持するため自動的に成立する）。

    mm を「px からの逆算値」に置き換えるのは、`artboard_pixel_size`（png_exporter）が
    書き出し px を `mm/25.4*dpi` で算出するため。mm をこの逆算で置いたときだけ
    「アートボード px = 書き出し px」が成立する（2026-08-21 ユーザー決定）。
    """
    new_width_px = clamp_artboard_px(width_px)
    new_height_px = clamp_artboard_px(height_px)
    new_physical = replace(
        artboard.physical,
        width_mm=mm_from_px(new_width_px, artboard.physical.target_dpi),
    )
    return replace(
        artboard,
        width_px=new_width_px,
        height_px=new_height_px,
        physical=new_physical,
    )
