"""view 層が duck-typing で依存している item 側の暗黙契約を文書化する（Phase 0 サブタスク C）。

このモジュールは**型ドキュメントのみ**であり、実行時の挙動には一切関与しない。
`app/scene/handles.py`・`app/tools/tool_manager.py`・`app/scene/canvas_scene.py`・
`app/scene/canvas_view.py`・`app/scene/items/connector_item.py` は、具象クラス
（`RectEllipseItem`/`LineItem`/`TextItem`/`MathItem`/`ImageItem`/`FreehandItem`/
`ConnectorItem`/`CropOverlay` 等）を import せず、`getattr(item, "...", None)` +
`callable(...)` チェックで「その属性があれば呼ぶ」形の緩い多態性に頼っている
箇所がある（型を絞らずに複数の item 種別を横断して扱うため）。ここではその
暗黙の形状を `typing.Protocol` として書き出す。

**注意（重要）**:
- どの実装クラスも、ここに定義する Protocol を **継承しない**（構造的部分型と
  して満たしているだけ）。実装ファイル（shape_item.py 等）は本サブタスクで
  一切変更していない。
- `runtime_checkable` は付けない。`isinstance(x, SomeProtocol)` によるランタイム
  判定へ書き換える意図はなく、既存の `getattr` + `callable` によるダック
  タイピングの**挙動を変えないまま**型だけを記述するための文書である。
- スコープは「item オブジェクトが持つ属性」の契約に限る。`scene.undo_stack` /
  `scene.item_for` / `scene.snap_enabled` / `scene.grid_size_or_none` /
  `scene.set_snap_guides` など **CanvasScene 側**がダックタイピングされている
  箇所（例: base_item.py:151,180 / handles.py:38,45,47,406 /
  connector_item.py:172,419,502 / tool_manager.py:95）は本サブタスクの対象外
  （item Protocol ではなく scene Protocol の話であり、担当ファイルはこの
  1ファイルのみのため範囲外とする）。
- `refresh_handles()`（`base_item.py:105` で `BaseItem` が定義し、全 item 種別が
  一様に継承する）も `canvas_view.py:106` で duck-typing 呼び出しされているが、
  これは「item 種別によって形が変わる」契約ではなく `BaseItem` が常に提供する
  共通メソッドの防御的呼び出し（isinstance 回避のスタイル）なので、ここでは
  Protocol 化しない。
"""

from __future__ import annotations

from typing import Protocol

from app.graphics.routing import Point


class BoxGeometryItem(Protocol):
    """矩形バウンディングボックス（x, y, width, height, rotation）で幾何を持つ item。

    実装例: `RectEllipseItem`（shape_item.py）, `TextItem`（text_item.py）,
    `MathItem`（math_item.py）, `ImageItem` / `CropOverlay`（image_item.py）,
    `FreehandItem`（freehand_item.py）。全実装でシグネチャは一字一句同一。

    duck-typing での呼び出し箇所:
    - `handles.py:261-262,289,312,326,374`（`BoxHandleSet`。ただしここは
      `parent_item: RectEllipseItem` 等の静的型注釈があるため厳密には
      duck-typing ではないが、`BoxHandleSet` は他の box 系 item からも
      同一シグネチャで再利用される想定の構造的contract）。
    - `connector_item.py:60-70`（`logical_box_for_item`）/ `:85-96`
      （`_anchor_set_for_item`）: `getattr(item, "live_geometry", None)` +
      `callable` で box 系（"width"/"height" キー）か endpoint 系
      （"p1"/"p2" キー、`EndpointGeometryItem` 参照）かを戻り値の
      キー構成で判別している。
    """

    def set_live_rect(self, x: float, y: float, w: float, h: float) -> None:
        """ドラッグ中のライブ矩形更新（モデルは書かない）。ハンドル/ツールから呼ばれる。"""
        ...

    def set_live_rotation(self, rotation: float) -> None:
        """ドラッグ中のライブ回転更新（モデルは書かない）。回転ハンドルから呼ばれる。"""
        ...

    def live_geometry(self) -> dict[str, float]:
        """現在の表示幾何（ライブドラッグ中はライブ値、確定後はモデル値と一致）。

        キーは `x`/`y`/`width`/`height`/`rotation`。
        """
        ...

    def model_geometry(self) -> dict[str, float]:
        """`obj` に保存されているモデル幾何（ドラッグ開始時の undo 用スナップショット）。"""
        ...


class EndpointGeometryItem(Protocol):
    """始点・終点（p1, p2）で幾何を持つ item。実装例: `LineItem`（shape_item.py、line/arrow）。

    duck-typing での呼び出し箇所:
    - `tool_manager.py:243-248`（`_select_move`）: `getattr(item, "set_live_points", None)`
      + `callable` チェックの上でライブ平行移動を反映する。
    - `handles.py:441,446,459,461,466`（`EndpointHandleSet`。`parent_item: LineItem`
      の静的型注釈があるが、box 系と対になる構造的契約として記載）。
    - `connector_item.py:60-70,85-96`: `live_geometry()` の戻り値に
      `"p1"`/`"p2"` キーがあれば endpoint 系として扱う（`BoxGeometryItem` 参照）。
    """

    def set_live_points(self, p1: list[float] | None = None, p2: list[float] | None = None) -> None:
        """ドラッグ中のライブ端点更新（モデルは書かない）。`None` の側は変更しない。"""
        ...

    def live_geometry(self) -> dict[str, list[float]]:
        """現在の表示端点。キーは `p1`/`p2`（各 `[x, y]`）。"""
        ...

    def model_geometry(self) -> dict[str, list[float]]:
        """`obj` に保存されているモデル端点（`p1`/`p2`。undo 用スナップショット）。"""
        ...


class ConnectorLiveItem(Protocol):
    """コネクタ編集 UX が要求する `ConnectorItem` 固有のライブ更新・確定 API。

    実装: `ConnectorItem`（connector_item.py）のみ。呼び出し側:
    - `tool_manager.py:237-239`（`_select_move`）: 本体ドラッグ中、
      `getattr(item, "set_live_body_offset", None)` + `callable` で
      固定端点側のみを平行移動する。
    - `tool_manager.py:295-297`（`_select_release`）: ドラッグ確定後、
      `getattr(item, "clear_live", None)` + `callable` でライブ
      オーバーライドを解除する。
    - `set_live_endpoint` / `commit_endpoint_drop` / `set_anchor` は
      `ConnectorHandleSet`（app/scene/handles.py。`parent_item` は
      `ConnectorItem` へ静的に型付けされている）から直接呼ばれ、かつ
      `commit_endpoint_drop` 内部（connector_item.py:396）でも
      `set_live_endpoint` を自己呼び出しする。tool_manager.py/canvas_scene.py
      からの `getattr` 越しの呼び出しは無い（純粋な duck-typing 経路は
      `set_live_body_offset`/`clear_live` のみ）が、`ConnectorItem` 固有の
      一枚岩の契約として同じ Protocol にまとめて記載する。
    """

    def set_live_endpoint(self, which: str, point: Point | None) -> None:
        """`which`（"source"/"target"）の端点ドラッグ中の視覚追従。`point=None` で解除。"""
        ...

    def set_live_body_offset(self, dx: float, dy: float = 0.0) -> None:
        """本体ドラッグ用: 固定端点側のみ `(dx, dy)` 平行移動したライブ点を設定する。"""
        ...

    def clear_live(self) -> None:
        """ライブ端点オーバーライドをすべて解除する（コミット後・確定後に呼ぶ）。"""
        ...

    def commit_endpoint_drop(self, which: str, drop: Point) -> None:
        """端点ドラッグの確定。スナップ/ヒットテスト結果を `SetPropertyCommand` で push する。"""
        ...

    def set_anchor(self, which: str, anchor: str) -> None:
        """`which` の `source_anchor`/`target_anchor` を `SetPropertyCommand` で設定する。"""
        ...


class ConnectorBindable(Protocol):
    """接続先 item の `geometryChanged` シグナルへの購読管理 API。

    実装: `ConnectorItem`（connector_item.py）のみ。`BaseItem`（他の全 item 種別）
    はこれらのメソッドを持たない。呼び出し側は `CanvasScene`:
    - `canvas_scene.py:222-224`（`remove_item_for`）: 除去対象 item 自身が
      `getattr(item, "destroy_bindings", None)` を持てば `removeItem()` の
      前に呼び、Qt 側の自動切断より先に購読解除する。
    - `canvas_scene.py:253-265`（`rebind_connectors`）: シーン内の全 item を
      走査し、`getattr(item, "bind_endpoints", None)` が呼び出し可能な
      item（＝ `ConnectorItem`）にのみ `bind_endpoints()` を呼んで接続先
      バインドを張り直す。`add_item_for`/`remove_item_for`/`load_document`
      （canvas_scene.py:61-62,202,226,250-251）がこの `rebind_connectors()`
      を呼ぶ駆動元。
    """

    def bind_endpoints(self) -> None:
        """接続先 item の `geometryChanged` を購読する（張り替え時は旧接続を解除）。"""
        ...

    def destroy_bindings(self) -> None:
        """item がシーンから除去される際に呼ぶ（購読解除のみ）。"""
        ...
