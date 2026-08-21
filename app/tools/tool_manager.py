"""ツール状態機械（select/rect/ellipse/line）: シーンへのマウス入力を解釈する(§8)。

モデルは直接変更せず、`app.commands.commands` のコマンドを `scene.undo_stack` に push する。
"""

from __future__ import annotations

import math
from collections.abc import Callable
from dataclasses import MISSING
from dataclasses import fields as dataclass_fields
from typing import TYPE_CHECKING, Any, NamedTuple

from PySide6.QtCore import QObject, QPointF, QRectF, Qt, Signal
from PySide6.QtGui import QBrush, QColor, QFont, QPainterPath, QPen, QTransform, QUndoCommand
from PySide6.QtWidgets import (
    QGraphicsItem,
    QGraphicsLineItem,
    QGraphicsPathItem,
    QGraphicsRectItem,
)

from app.commands.commands import AddObjectCommand, SetGeometryCommand, SetPropertyCommand
from app.graphics import curves
from app.model.objects import (
    BaseObject,
    ConnectorObject,
    CurveObject,
    EllipseObject,
    FreehandObject,
    LineObject,
    MathObject,
    RectObject,
    TextObject,
    geometry_kind,
)
from app.prefs import Preferences

if TYPE_CHECKING:
    from app.scene.canvas_scene import CanvasScene

_TOOLS = (
    "select",
    "rect",
    "ellipse",
    "line",
    "arrow",
    "freehand",
    "text",
    "math",
    "connector",
    "curve",
)
_DRAW_TOOLS = ("rect", "ellipse", "line", "arrow")

# 移動判定/最小生成サイズのしきい値(px)。浮動小数の厳密等値比較を避けるための距離マージン。
_MOVE_EPS = 1.0
# freehand: 前点からこの距離(px)以上離れたら新しい点として採用する。
_FREEHAND_MIN_DIST = 2.0
# math: 新規生成時の既定 latex とレンダリング失敗時のフォールバック最小サイズ(px)。
_MATH_DEFAULT_LATEX = "E = mc^2"
_MATH_MIN_SIZE = 20.0
# curve: 下書き点を追加する最小間隔(px)/始点近傍とみなし閉じる判定に使う画面px半径。
_CURVE_MIN_DIST = 3.0
_CURVE_CLOSE_SCREEN_PX = 10.0

_MouseHandler = Callable[[Any, QPointF], bool]

#: sticky defaults(P3契約 §4.2)で記憶する「スタイル」フィールド名。ジオメトリ・
#: レイヤー状態(x/y/rotation/locked/visible 等)は対象外。
#: `fill`/`stroke` が `None`（線・塗りなし、P2契約 項目12）の場合もそのまま記憶
#: 対象になる——「線なし矩形を1つ作ると次も線なしになる」のは、`fill=None` の
#: 既存の粘り方（塗りなしを作ると次も塗りなし）と同じ意図的な挙動であり、
#: バグではない。
_STYLE_KEYS: frozenset[str] = frozenset(
    {
        "fill",
        "stroke",
        "stroke_width",
        "dash",
        "corner_radius",
        "arrow_start",
        "arrow_end",
        "arrow_size",
        "smoothing",
        "font_family",
        "font_size",
        "bold",
        "italic",
        "underline",
        "color",
        "align",
        "valign",
    }
)


def _dataclass_field_default(f: Any) -> Any:
    """`dataclasses.Field` の既定値を返す（`default`/`default_factory` のどちらでも）。"""
    if f.default is not MISSING:
        return f.default
    if f.default_factory is not MISSING:  # type: ignore[misc]
        return f.default_factory()
    return None


class _ToolHandlers(NamedTuple):
    """1 ツール分の press/move/release/double ハンドラ束。`ToolManager._handlers` の値型。

    `double` はダブルクリックで確定するツール（curve）だけが持つため既定値 None を
    持たせてある。既存の 3 引数での生成はそのまま通る。
    """

    press: _MouseHandler
    move: _MouseHandler
    release: _MouseHandler
    double: _MouseHandler | None = None


class ToolManager(QObject):
    """現在のツール名を保持し、マウスイベントを各ツールの振る舞いに振り分ける。"""

    tool_changed = Signal(str)

    def __init__(self, scene: CanvasScene, *, prefs: Preferences | None = None) -> None:
        super().__init__()
        self.scene = scene
        # 環境設定(C契約 §C-1)。None なら新規オブジェクトの既定は全て従来どおり
        # (dataclass 既定値のまま)で、既存の呼び出し元・テストへの回帰は無い。
        self.prefs = prefs
        self._tool: str = "select"
        # select ツール: press 時に記録する選択群の旧幾何 {obj.id: geom_dict}
        self._select_start: dict[int, dict[str, Any]] = {}
        self._select_press_pos: QPointF | None = None
        # rect/ellipse/line/arrow ツール: 描画中のプレビュー
        self._draw_start: QPointF | None = None
        self._preview_item: QGraphicsItem | None = None
        # freehand ツール: 蓄積中の生 scene 点列とプレビューパス
        self._freehand_points: list[QPointF] | None = None
        self._freehand_path: QPainterPath | None = None
        self._freehand_preview: QGraphicsPathItem | None = None
        # text ツール: press 時のクリック位置
        self._text_start: QPointF | None = None
        # math ツール: press 時のクリック位置
        self._math_start: QPointF | None = None
        # connector ツール: press 時に掴んだ source 候補オブジェクト（無ければ固定点）
        self._connector_source_obj: BaseObject | None = None
        # curve ツール: 下書き中の scene 点列(2点未満は生成しない)。hover はプレビュー用の
        # 最新マウス位置、_curve_preview/_curve_nodes_preview はライブ曲線・点マーカーの
        # プレビュー item。
        self._curve_points: list[QPointF] | None = None
        self._curve_hover: QPointF | None = None
        self._curve_preview: QGraphicsPathItem | None = None
        self._curve_nodes_preview: QGraphicsPathItem | None = None
        # 右クリック確定直後に Qt が合成する QContextMenuEvent を 1 回だけ抑止する
        # ワンショットフラグ(`consume_context_menu_suppression` 参照)。
        self._suppress_context_menu_once: bool = False
        # 左ボタンの press〜release の間 True（`is_interacting`）。ツール別の状態だけでは
        # 「押したが何も掴めなかった」ラバーバンド選択等を取りこぼすため別に持つ。
        # 右/中ボタンでは立てない: Linux の Qt は右ボタンの press と release の**間**に
        # QContextMenuEvent を合成配送する(実測順: press → CTX → release)。ここをボタン
        # 種別に関係なく立てていると、`CanvasView.contextMenuEvent` の `is_interacting()`
        # ガードが右クリック1回目の合成イベントを常に捨ててしまう（項目5契約）。
        self._left_press_active: bool = False
        # ツール名 → press/move/release ハンドラのディスパッチテーブル。
        # rect/ellipse/line/arrow(_DRAW_TOOLS)は同一の _draw_* 三つ組を共有する。
        self._handlers: dict[str, _ToolHandlers] = {
            "select": _ToolHandlers(self._select_press, self._select_move, self._select_release),
            "freehand": _ToolHandlers(
                self._freehand_press, self._freehand_move, self._freehand_release
            ),
            "text": _ToolHandlers(self._text_press, self._text_move, self._text_release),
            "math": _ToolHandlers(self._math_press, self._math_move, self._math_release),
            "connector": _ToolHandlers(
                self._connector_press, self._connector_move, self._connector_release
            ),
            "curve": _ToolHandlers(
                self._curve_press,
                self._curve_move,
                self._curve_release,
                self._curve_double_click,
            ),
        }
        for _draw_tool in _DRAW_TOOLS:
            self._handlers[_draw_tool] = _ToolHandlers(
                self._draw_press, self._draw_move, self._draw_release
            )

        # sticky defaults(P3契約 §4.2): type → {style_key: value}。「1個作って色を変えたら
        # 次に作る同種オブジェクトはその色」(Figma/Excalidraw流)。Document のリスナーとして
        # 自身を登録し、document 差し替え時(`scene.document_replaced`)にも再登録する。
        self._style_memory: dict[str, dict[str, Any]] = {}
        scene.document.add_listener(self)
        scene.document_replaced.connect(self._on_document_replaced)

    def _on_document_replaced(self) -> None:
        """`scene.document_replaced`(P3契約 §4.1): 新 document へリスナー登録し直す。

        curve の下書きは旧 document 前提の状態（生成予定の id 等）のため、
        document 差し替え時は確定させずに破棄する（crop/mask 同様、宙に浮いた
        参照を残さない方針）。
        """
        self.scene.document.add_listener(self)
        self._cancel_curve_draft()

    # ------------------------------------------------------------------
    # DocumentListener 実装（sticky defaults 専用。他のコールバックは no-op）
    # ------------------------------------------------------------------
    def on_object_added(self, obj: BaseObject, index: int) -> None:  # noqa: ARG002
        self._remember_style(obj)

    def on_object_removed(self, obj: BaseObject) -> None:  # noqa: ARG002
        pass

    def on_object_changed(self, obj: BaseObject, keys: tuple[str, ...]) -> None:
        if _STYLE_KEYS.intersection(keys):
            self._remember_style(obj)

    def on_order_changed(self) -> None:
        pass

    def on_artboard_changed(self) -> None:
        pass

    def _remember_style(self, obj: BaseObject) -> None:
        """obj の dataclass fields ∩ `_STYLE_KEYS` の現在値を type 別に記憶する。"""
        style: dict[str, Any] = {
            f.name: getattr(obj, f.name) for f in dataclass_fields(obj) if f.name in _STYLE_KEYS
        }
        if style:
            self._style_memory[obj.type] = style

    def _apply_style_memory(self, obj: Any) -> None:
        """直近に記憶した同種オブジェクトのスタイルを新規オブジェクトへ適用する。"""
        for key, value in self._style_memory.get(obj.type, {}).items():
            if hasattr(obj, key):
                setattr(obj, key, value)

    def clear_style_memory(self) -> None:
        """sticky defaults(style memory)を全消去する。

        `_style_memory` は「同種を一度でも作っていれば」永久に環境設定の既定値を
        上書きし続けてしまう（所見: rect を 1 個描いた直後に環境設定で色/線幅を
        変えても、次に描く rect は変更前の記憶された値のまま）。環境設定ダイアログ
        で作成既定を変更した確定時に `MainWindow.open_preferences` から呼ぶことで、
        「設定を変えた直後の新規作成は変更後の値になる」という自然な期待に合わせる。
        """
        self._style_memory.clear()

    def _apply_pref_defaults(self, obj: Any) -> None:
        """環境設定(`self.prefs`)の既定値を新規オブジェクトへ適用する(C契約 §C-1)。

        `_push_creation` 内で `_apply_style_memory` の**直前**に呼ぶ（同種を
        一度でも作っていれば、そのあと `_apply_style_memory` が上書きしスタイル
        記憶が勝つ）。`self.prefs` が `None`（未配線・多くの既存テスト）なら
        何もせず、全フィールドが dataclass 既定値のまま生成される（回帰なし）。

        適用規則（フィールドを持つ型にのみ・現在値が **dataclass 既定値**の
        ときのみ上書き。ユーザーがダイアログ等で明示的に値を変えていた場合は
        触らない）:
        - font_family / font_size: `prefs.default_font_family` / `default_font_size`
        - stroke_width: `prefs.default_stroke_width`
        - routing: `prefs.default_connector_routing`
        - `prefs.initial_color` が `None` でなければ、stroke（図形系）と
          color（text/math）を `initial_color` に（fill は触らない。塗りなしの
          既定を維持する）。

        「dataclass 既定値のときのみ」の判定は `dataclasses.fields(obj)` の
        `default`/`default_factory` と比較する（text/math ダイアログ側の
        既定値には介入しない、という `_finish_creation` の既存方針を守る）。
        """
        prefs = self.prefs
        if prefs is None:
            return
        overrides: dict[str, Any] = {
            "font_family": prefs.default_font_family,
            "font_size": prefs.default_font_size,
            "stroke_width": prefs.default_stroke_width,
            "routing": prefs.default_connector_routing,
        }
        if prefs.initial_color is not None:
            overrides["stroke"] = prefs.initial_color
            overrides["color"] = prefs.initial_color
        fields_by_name = {f.name: f for f in dataclass_fields(obj)}
        for name, value in overrides.items():
            field = fields_by_name.get(name)
            if field is None:
                continue
            if getattr(obj, name) == _dataclass_field_default(field):
                setattr(obj, name, value)

    def set_tool(self, name: str) -> None:
        if name not in _TOOLS:
            raise ValueError(f"unknown tool: {name}")
        self._cancel_preview()
        self._cancel_freehand()
        self._select_start = {}
        self._select_press_pos = None
        self._text_start = None
        self._math_start = None
        self._connector_source_obj = None
        self._left_press_active = False
        self._clear_snap_guides()
        if self._tool == name:
            return
        # crop の確定は「実際にツールが替わる」ときのみ行う（早期 return より
        # 後に置く）。同一ツールボタンの再クリックで crop 中の編集が不意に
        # 確定されないようにするため（レビュー所見 nit）。
        self._commit_active_crop()
        self._commit_active_mask()
        self._commit_active_node_edit()
        self._commit_active_text_edit()
        self._finish_curve_draft_on_tool_change()
        self._tool = name
        self.tool_changed.emit(name)

    def _commit_active_crop(self) -> None:
        """crop モード中ならツール切替前に確定する（宙ぶらりんのモードを残さない）。

        `active_crop_item` は `CanvasScene` 側の追加 API のため、未実装の
        scene でも壊れないようダックタイピングで呼ぶ。
        """
        getter = getattr(self.scene, "active_crop_item", None)
        crop_item = getter() if callable(getter) else None
        if crop_item is not None:
            crop_item.commit_crop()

    def _commit_active_mask(self) -> None:
        """SAM3 マスク編集モード中ならツール切替前に確定する（ダックタイピング、crop と同方針）。"""
        getter = getattr(self.scene, "active_mask_session", None)
        session = getter() if callable(getter) else None
        if session is not None:
            session.commit()

    def _commit_active_node_edit(self) -> None:
        """曲線ノード編集モード中ならツール切替前に確定する（ダックタイピング、crop と同方針）。

        `active_node_edit_item` は `CanvasScene` 側の追加 API のため、未実装の
        scene でも壊れないようダックタイピングで呼ぶ。`CurveItem`（担当外）を
        直接 import せず `commit_node_edit()` の呼び出しだけで連携する。
        """
        getter = getattr(self.scene, "active_node_edit_item", None)
        item = getter() if callable(getter) else None
        if item is not None:
            item.commit_node_edit()

    def _commit_active_text_edit(self) -> None:
        """テキストのインプレース編集中ならツール切替前に確定する。

        （ダックタイピング、crop と同方針）`active_text_edit_item` は
        `CanvasScene` 側の追加 API のため、未実装の scene でも壊れないよう
        ダックタイピングで呼ぶ。`TextItem`（担当外）を直接 import せず
        `commit_text_edit()` の呼び出しだけで連携する。
        """
        getter = getattr(self.scene, "active_text_edit_item", None)
        item = getter() if callable(getter) else None
        if item is not None:
            item.commit_text_edit()

    def _clear_snap_guides(self) -> None:
        """スナップガイドを消す(M7契約 §7)。

        `set_snap_guides` は `CanvasScene`(M7) 側の追加 API のため、未実装の
        scene でも壊れないようダックタイピングで呼ぶ。
        """
        set_guides = getattr(self.scene, "set_snap_guides", None)
        if callable(set_guides):
            set_guides([])

    def current_tool(self) -> str:
        return self._tool

    def is_interacting(self) -> bool:
        """人間がキャンバス上で操作の途中か（press 済み・release 前）。

        外部（エージェント制御サーバ）がモデルを書き換えてよいかの判定に使う。
        ドラッグ移動は press 時に旧幾何を撮って release 時に 1 コマンドとして
        push するため、その間にモデルを書き換えると `old_geom` が陳腐化し、
        undo でオブジェクトがワープする。

        右/中ボタンの押下はここでは追わない（`_left_press_active` は左ボタンの
        press/release だけで立てる/降ろす。項目5契約 — Linux の Qt が右ボタンの
        press と release の間に合成配送する `QContextMenuEvent` を、右クリック
        1 回目から確実に通すため）。`app/agent/host.py` の busy 判定は
        `QApplication.mouseButtons() != NoButton` をここより**前**に見ているので、
        右/中ボタンが押されている間にモデルを書き換えられてしまう不変条件5（§15）
        の懸念は生じない。

        ハンドルのドラッグは `ToolManager` を経由せず item のイベントで進むので、
        ここでは追えない。呼び出し側は `QApplication.mouseButtons()` も併せて見ること。
        """
        return (
            self._left_press_active
            or self._select_press_pos is not None
            or self._draw_start is not None
            or self._freehand_points is not None
            or self._text_start is not None
            or self._math_start is not None
            or self._curve_points is not None
        )

    def handle_mouse_press(self, event: Any, scene_pos: QPointF) -> bool:
        # 右クリック確定の抑止フラグ（`_suppress_context_menu_once`）は合成
        # QContextMenuEvent が届かなかった場合に立ちっぱなしになり、次の正当な
        # 右クリックメニューを 1 回黙って食う恐れがある（レビュー所見）。
        # `_curve_press` がフラグを立ててから `_finish_curve_draft` →
        # `_finish_creation` → `set_tool` までを合成イベント到着より前に済ませる
        # ため、以後のプレスは必ずその合成イベントより後に来る。よってここで
        # 無条件にクリアしても正当な抑止を消さない。
        self._suppress_context_menu_once = False
        handlers = self._handlers.get(self._tool)
        if handlers is None:
            return False
        if self._is_left_press(event):
            self._left_press_active = True
        return handlers.press(event, scene_pos)

    def handle_mouse_move(self, event: Any, scene_pos: QPointF) -> bool:
        handlers = self._handlers.get(self._tool)
        if handlers is None:
            return False
        return handlers.move(event, scene_pos)

    def handle_mouse_release(self, event: Any, scene_pos: QPointF) -> bool:
        handlers = self._handlers.get(self._tool)
        if self._is_left_press(event):
            # 左 press 中に右 release が来ても状態を消さない（項目5契約）。
            # 右クリック確定の合成イベント配送順(press → CTX → release)により
            # release だけが来るケースがあり、そのボタンで無条件に降ろすと
            # 「左ドラッグ中に右クリック」で busy ゲートが開いてしまう。
            self._left_press_active = False
        if handlers is None:
            return False
        return handlers.release(event, scene_pos)

    def handle_mouse_double_click(self, event: Any, scene_pos: QPointF) -> bool:
        """ダブルクリックを持つツール（curve）に振り分ける。`double` が無いツールは非消費。

        `tool == "curve"` の間は下書きが無くても常に True（消費）を返す。素通しすると
        `QGraphicsView` の既定処理まで落ち、`ImageItem.begin_crop` 等がダブルクリックに
        誤反応するため（curve ツール選択中は編集対象が無くてもダブルクリックは
        curve の確定操作として扱いたい）。
        """
        tool = self._tool
        handlers = self._handlers.get(tool)
        if handlers is None or handlers.double is None:
            return False
        consumed = handlers.double(event, scene_pos)
        # ★ ディスパッチ前の tool を見る。左ダブルクリック確定は
        # `_curve_double_click` → `_finish_creation` → `set_tool("select")` を
        # 呼ぶため、ハンドラ実行「後」の self._tool は既に "select" に変わって
        # おり、判定が死んでいた（レビュー所見）。
        if tool == "curve":
            return True
        return consumed

    # ------------------------------------------------------------------
    # select: 非消費。press で「選択済み・movable な BaseItem 本体」を実際に
    # 掴んだ場合のみ移動をアームし、release で移動分を SetGeometryCommand として
    # push する（複数選択は beginMacro/endMacro）。ハンドルや空白部（ラバーバンド
    # 選択）を掴んだ場合はアームしない。
    # ------------------------------------------------------------------
    def _topmost_item_at(self, scene_pos: QPointF) -> QGraphicsItem | None:
        views = self.scene.views()
        transform = views[0].transform() if views else QTransform()
        items = self.scene.items(
            scene_pos,
            Qt.ItemSelectionMode.IntersectsItemShape,
            Qt.SortOrder.DescendingOrder,
            transform,
        )
        return items[0] if items else None

    def _select_press(self, event: Any, scene_pos: QPointF) -> bool:
        self._select_press_pos = None
        self._select_start = {}
        if (
            getattr(event, "button", None) is not None
            and event.button() != Qt.MouseButton.LeftButton
        ):
            return False
        hit = self._topmost_item_at(scene_pos)
        obj = getattr(hit, "obj", None)
        if obj is None or getattr(obj, "locked", False):
            # 空白部（ラバーバンド選択の開始）またはハンドル操作: 移動をアームしない。
            return False

        currently_selected = self.scene.selected_objects()
        if obj in currently_selected and len(currently_selected) > 1:
            targets = currently_selected
        else:
            # M7契約 §7: 掴んだ obj がグループに属するなら、グループ全体を
            # 移動アーム対象にする(グループ選択自体の拡張は scene 側の責務。
            # ここでは undo 用の旧幾何記録の対象を広げるだけ)。
            group_id = getattr(obj, "group_id", None)
            if group_id is not None:
                targets = [
                    o
                    for o in self.scene.document.objects
                    if getattr(o, "group_id", None) == group_id and not getattr(o, "locked", False)
                ]
                if not targets:
                    targets = [obj]
            else:
                targets = [obj]

        self._select_press_pos = scene_pos
        for target in targets:
            if getattr(target, "locked", False):
                continue
            if target.type == "connector":
                # 本体ドラッグでは固定端点を平行移動する(コネクタ編集UX契約 §3)。
                self._select_start[target.id] = {
                    "_connector": True,
                    "source_point": list(target.source_point),
                    "target_point": list(target.target_point),
                }
            elif geometry_kind(target.type) == "endpoints":
                self._select_start[target.id] = {"p1": list(target.p1), "p2": list(target.p2)}
            else:
                self._select_start[target.id] = {"x": target.x, "y": target.y}
        return False

    def _select_move(self, event: Any, scene_pos: QPointF) -> bool:
        """line/arrow/connector をドラッグ中にライブ追従させる(§6.2、コネクタUX契約 §3)。

        box系(x/y geom)は ItemIsMovable=True で Qt 自身がライブ移動するため
        何もしない。line/arrow(p1/p2 geom)は ItemIsMovable=False のため、
        ここで press からのマウス移動量を live 表示にだけ反映する（モデルは
        書かない。確定は `_select_release` が行う）。connector 本体ドラッグも
        同様に、固定端点のみを平行移動する live オフセットを `set_live_body_offset`
        で反映する（接続端は図形追従を維持したまま）。box を Qt がドラッグでき
        るよう、また単独 line/connector press 時に Qt がラバーバンド選択を開始
        しないよう、常に `False`（非消費）を返す。
        """
        if self._select_press_pos is None or not self._select_start:
            return False
        dx = scene_pos.x() - self._select_press_pos.x()
        dy = scene_pos.y() - self._select_press_pos.y()
        document = self.scene.document
        for oid, geom in self._select_start.items():
            obj = document.object_by_id(oid)
            if obj is None:
                continue
            item = self.scene.item_for(obj)
            if geom.get("_connector"):
                set_live_body_offset = getattr(item, "set_live_body_offset", None)
                if callable(set_live_body_offset):
                    set_live_body_offset(dx, dy)
                continue
            if "p1" not in geom:
                continue
            set_live_points = getattr(item, "set_live_points", None)
            if not callable(set_live_points):
                continue
            p1 = geom["p1"]
            p2 = geom["p2"]
            set_live_points(p1=[p1[0] + dx, p1[1] + dy], p2=[p2[0] + dx, p2[1] + dy])
        return False

    def _select_release(self, event: Any, scene_pos: QPointF) -> bool:
        start_pos = self._select_press_pos
        old_geoms = self._select_start
        self._select_press_pos = None
        self._select_start = {}
        self._clear_snap_guides()
        if not old_geoms:
            return False
        dx: float | None = None
        dy: float | None = None
        if start_pos is not None:
            dx = scene_pos.x() - start_pos.x()
            dy = scene_pos.y() - start_pos.y()
        document = self.scene.document
        undo_stack = self.scene.undo_stack
        if undo_stack is None:
            return False
        commands: list[QUndoCommand] = []
        for oid, old_geom in old_geoms.items():
            obj = document.object_by_id(oid)
            if obj is None:
                continue
            if old_geom.get("_connector"):
                # 本体ドラッグ: 固定端点(source_id/target_id が None の側)のみを
                # 平行移動する。両端接続なら変化なし(コネクタUX契約 §3)。
                item = self.scene.item_for(obj)
                if dx is not None and math.hypot(dx, dy) >= _MOVE_EPS:
                    if obj.source_id is None:
                        old_sp = old_geom["source_point"]
                        new_sp = [old_sp[0] + dx, old_sp[1] + dy]
                        commands.append(
                            SetPropertyCommand(document, obj, "source_point", new_sp, list(old_sp))
                        )
                    if obj.target_id is None:
                        old_tp = old_geom["target_point"]
                        new_tp = [old_tp[0] + dx, old_tp[1] + dy]
                        commands.append(
                            SetPropertyCommand(document, obj, "target_point", new_tp, list(old_tp))
                        )
                if item is not None:
                    clear_live = getattr(item, "clear_live", None)
                    if callable(clear_live):
                        clear_live()
                continue
            if "p1" in old_geom:
                # LineItem は ItemIsMovable=False で pos を持たないため、
                # 実マウス移動量から delta を導出する他ない（§6.2）。
                if dx is None or math.hypot(dx, dy) < _MOVE_EPS:
                    continue
                p1 = old_geom["p1"]
                p2 = old_geom["p2"]
                new_geom = {"p1": [p1[0] + dx, p1[1] + dy], "p2": [p2[0] + dx, p2[1] + dy]}
            else:
                # rect/ellipse は ItemIsMovable=True で Qt がドラッグ中に item.pos()
                # を実際に動かすため、生マウス delta ではなく実移動量を使う。
                item = self.scene.item_for(obj)
                if item is not None:
                    new_x, new_y = item.pos().x(), item.pos().y()
                elif dx is not None:
                    new_x, new_y = old_geom["x"] + dx, old_geom["y"] + dy
                else:
                    continue
                if math.hypot(new_x - old_geom["x"], new_y - old_geom["y"]) < _MOVE_EPS:
                    continue
                new_geom = {"x": new_x, "y": new_y}
            commands.append(SetGeometryCommand(document, obj, new_geom, old_geom))
        if not commands:
            return False
        if len(commands) == 1:
            undo_stack.push(commands[0])
        else:
            undo_stack.beginMacro("move")
            for command in commands:
                undo_stack.push(command)
            undo_stack.endMacro()
        return False

    # ------------------------------------------------------------------
    # rect/ellipse/line: press で開始点記録、move でプレビュー、release で
    # オブジェクト生成 + AddObjectCommand、select ツールに戻し新規選択。
    # ------------------------------------------------------------------
    def _draw_press(self, event: Any, scene_pos: QPointF) -> bool:
        if (
            getattr(event, "button", None) is not None
            and event.button() != Qt.MouseButton.LeftButton
        ):
            # 右クリック等では何もしない(仮プレビュー未生成のまま消費しない)。
            return False
        self._cancel_preview()
        self._draw_start = scene_pos
        pen = QPen(QColor("#3399ff"))
        pen.setStyle(Qt.PenStyle.DashLine)
        pen.setCosmetic(True)
        if self._tool in ("line", "arrow"):
            item: QGraphicsItem = QGraphicsLineItem(
                scene_pos.x(), scene_pos.y(), scene_pos.x(), scene_pos.y()
            )
        else:
            item = QGraphicsRectItem(QRectF(scene_pos, scene_pos))
        item.setPen(pen)
        item.setZValue(1.0e6)
        self.scene.addItem(item)
        self._preview_item = item
        return True

    def _draw_move(self, event: Any, scene_pos: QPointF) -> bool:
        if self._draw_start is None or self._preview_item is None:
            return True
        if self._tool in ("line", "arrow"):
            line_item: QGraphicsLineItem = self._preview_item  # type: ignore[assignment]
            line_item.setLine(
                self._draw_start.x(), self._draw_start.y(), scene_pos.x(), scene_pos.y()
            )
        else:
            rect_item: QGraphicsRectItem = self._preview_item  # type: ignore[assignment]
            rect_item.setRect(QRectF(self._draw_start, scene_pos).normalized())
        return True

    def _draw_release(self, event: Any, scene_pos: QPointF) -> bool:
        start = self._draw_start
        self._cancel_preview()
        if start is None:
            return True
        dx = scene_pos.x() - start.x()
        dy = scene_pos.y() - start.y()
        if math.hypot(dx, dy) < _MOVE_EPS:
            # ドラッグなしのクリック(微小移動含む)は無視し、同じツールで再入力を待つ。
            return True

        document = self.scene.document
        obj: RectObject | EllipseObject | LineObject
        if self._tool in ("line", "arrow"):
            # 上の hypot(dx, dy) 判定により線長は既に _MOVE_EPS 以上。
            obj = LineObject(
                id=document.new_id(),
                type=self._tool,
                p1=[start.x(), start.y()],
                p2=[scene_pos.x(), scene_pos.y()],
                arrow_start="none",
                arrow_end="triangle" if self._tool == "arrow" else "none",
            )
        else:
            rect = QRectF(start, scene_pos).normalized()
            if rect.width() < _MOVE_EPS and rect.height() < _MOVE_EPS:
                # 幅・高さのいずれも最小サイズに満たない退化図形は生成しない。
                return True
            cls = RectObject if self._tool == "rect" else EllipseObject
            obj = cls(
                id=document.new_id(),
                x=rect.x(),
                y=rect.y(),
                width=rect.width(),
                height=rect.height(),
            )

        return self._finish_creation(obj)

    def _cancel_preview(self) -> None:
        if self._preview_item is not None:
            item_scene = self._preview_item.scene()
            if item_scene is not None:
                item_scene.removeItem(self._preview_item)
            self._preview_item = None
        self._draw_start = None

    def _push_creation(self, obj: Any) -> bool:
        """AddObjectCommand を push し新規オブジェクトを選択する（ツール切替はしない）。

        undo_stack 未設定時はモデルを変更しない(§13 と同じ方針)。push 前に環境設定の既定
        (`_apply_pref_defaults`、C契約 §C-1)、続けて sticky defaults(P3契約 §4.2)を
        この順で適用する（同種を一度でも作っていればスタイル記憶が勝つ）。curve の
        「ツール切替時に下書きを確定する」経路
        （`_finish_curve_draft_on_tool_change`）は `set_tool` の途中から呼ばれるため、
        ここで `set_tool` を呼ぶと再入することになる。そのため選択までを行う
        この段と、続けて select ツールへ戻す `_finish_creation` とに分けてある。
        """
        undo_stack = self.scene.undo_stack
        if undo_stack is None:
            return True
        self._apply_pref_defaults(obj)
        self._apply_style_memory(obj)
        self._remeasure_new_text_or_math(obj)
        undo_stack.push(AddObjectCommand(self.scene.document, obj))
        new_item = self.scene.item_for(obj)
        if new_item is not None:
            self.scene.clearSelection()
            new_item.setSelected(True)
        return True

    def _remeasure_new_text_or_math(self, obj: Any) -> None:
        """text/math の新規作成時、環境設定/style memory 適用後の実フォントで箱を再採寸する。

        所見: `_text_release`/`_math_release` は生成直後の dataclass 既定フォント
        （18pt 等）で width/height を採寸するが、`font_family`/`font_size` は
        直前の `_apply_pref_defaults`/`_apply_style_memory` が書き換えるため、箱と
        実際に使われるフォントが食い違ったまま push されていた（`default_font_size`
        を大きくすると生成直後から `text_overflow` が発生、math は箱にフィット
        描画するため設定が見た目に反映されない、の二重の実害）。`obj.type` を
        text/math に限定して再採寸することで、幾何由来の寸法を持つ他の型
        （freehand/curve 等）の挙動には触れない。prefs が None かつ style memory も
        空の回帰ケースでは、生成時に使ったのと同じ既定フォントで再計算するだけ
        なので結果は変わらない。
        """
        if obj.type == "text":
            from app.scene.items.text_item import default_text_size

            font = QFont(obj.font_family)
            font.setPointSizeF(obj.font_size)
            font.setBold(obj.bold)
            font.setItalic(obj.italic)
            obj.width, obj.height = default_text_size(obj.text, font)
        elif obj.type == "math":
            from app.scene.items.math_item import natural_math_size

            obj.width, obj.height = natural_math_size(
                obj.latex, obj.font_size, obj.color, minimum=_MATH_MIN_SIZE
            )

    def _finish_creation(self, obj: Any) -> bool:
        """`_push_creation` に続けて select ツールへ戻す。

        text/math のダイアログ既定値には介入しない（このメソッドを通る、
        生成された obj への適用のみ）。
        """
        result = self._push_creation(obj)
        self.set_tool("select")
        return result

    # ------------------------------------------------------------------
    # freehand: press で点列開始+プレビュー、move で一定距離以上離れたら点追加、
    # release で正規化して FreehandObject を生成する。
    # ------------------------------------------------------------------
    def _freehand_press(self, event: Any, scene_pos: QPointF) -> bool:
        if (
            getattr(event, "button", None) is not None
            and event.button() != Qt.MouseButton.LeftButton
        ):
            return False
        self._cancel_freehand()
        self._freehand_points = [scene_pos]
        path = QPainterPath(scene_pos)
        pen = QPen(QColor("#3399ff"))
        pen.setWidthF(2.0)
        pen.setCosmetic(True)
        pen.setCapStyle(Qt.PenCapStyle.RoundCap)
        pen.setJoinStyle(Qt.PenJoinStyle.RoundJoin)
        preview = QGraphicsPathItem(path)
        preview.setPen(pen)
        preview.setZValue(1.0e6)
        self.scene.addItem(preview)
        self._freehand_path = path
        self._freehand_preview = preview
        return True

    def _freehand_move(self, event: Any, scene_pos: QPointF) -> bool:
        points = self._freehand_points
        if points is None or self._freehand_path is None or self._freehand_preview is None:
            return True
        last = points[-1]
        if math.hypot(scene_pos.x() - last.x(), scene_pos.y() - last.y()) >= _FREEHAND_MIN_DIST:
            points.append(scene_pos)
            self._freehand_path.lineTo(scene_pos)
            self._freehand_preview.setPath(self._freehand_path)
        return True

    def _freehand_release(self, event: Any, scene_pos: QPointF) -> bool:
        points = self._freehand_points
        path = self._freehand_path
        self._cancel_freehand()
        if points is None or path is None:
            return True
        last = points[-1]
        if math.hypot(scene_pos.x() - last.x(), scene_pos.y() - last.y()) >= _FREEHAND_MIN_DIST:
            points.append(scene_pos)
            path.lineTo(scene_pos)
        if len(points) < 2 or path.length() <= _MOVE_EPS:
            # 点数不足または経路長が極小のフリーハンドは生成しない。
            return True

        from app.scene.items.freehand_item import normalize_freehand_points

        raw = [[p.x(), p.y()] for p in points]
        x, y, width, height, normalized = normalize_freehand_points(raw)
        obj = FreehandObject(
            id=self.scene.document.new_id(),
            x=x,
            y=y,
            width=width,
            height=height,
            points=normalized,
        )
        return self._finish_creation(obj)

    def _cancel_freehand(self) -> None:
        if self._freehand_preview is not None:
            item_scene = self._freehand_preview.scene()
            if item_scene is not None:
                item_scene.removeItem(self._freehand_preview)
        self._freehand_preview = None
        self._freehand_points = None
        self._freehand_path = None

    # ------------------------------------------------------------------
    # text: クリック(press→ほぼ同座標 release)で TextObject を生成する。
    # ヘッドレス安全のためダイアログは自動起動しない(ユーザーはダブルクリックで編集)。
    # ------------------------------------------------------------------
    def _text_press(self, event: Any, scene_pos: QPointF) -> bool:
        if (
            getattr(event, "button", None) is not None
            and event.button() != Qt.MouseButton.LeftButton
        ):
            return False
        self._text_start = scene_pos
        return True

    def _text_move(self, event: Any, scene_pos: QPointF) -> bool:
        return True

    def _text_release(self, event: Any, scene_pos: QPointF) -> bool:
        start = self._text_start
        self._text_start = None
        if start is None:
            return True

        from app.scene.items.text_item import default_text_size

        defaults = TextObject(id=0)
        font = QFont(defaults.font_family)
        font.setPointSizeF(defaults.font_size)
        text = "テキスト"
        width, height = default_text_size(text, font)
        obj = TextObject(
            id=self.scene.document.new_id(),
            text=text,
            x=start.x(),
            y=start.y(),
            width=width,
            height=height,
        )
        return self._finish_creation(obj)

    # ------------------------------------------------------------------
    # math: クリック(press→ほぼ同座標 release)で MathObject を生成する。
    # ヘッドレス安全のため edit_latex ダイアログは自動起動しない(ユーザーは
    # ダブルクリックで再編集する)。既定サイズは mathtext を実際にレンダリングし
    # QSvgRenderer.defaultSize() から px 値を得る(§9.4/M5契約 §3)。
    # ------------------------------------------------------------------
    def _math_press(self, event: Any, scene_pos: QPointF) -> bool:
        if (
            getattr(event, "button", None) is not None
            and event.button() != Qt.MouseButton.LeftButton
        ):
            return False
        self._math_start = scene_pos
        return True

    def _math_move(self, event: Any, scene_pos: QPointF) -> bool:
        return True

    def _math_release(self, event: Any, scene_pos: QPointF) -> bool:
        start = self._math_start
        self._math_start = None
        if start is None:
            return True

        width, height = self._math_default_size()
        obj = MathObject(
            id=self.scene.document.new_id(),
            latex=_MATH_DEFAULT_LATEX,
            x=start.x(),
            y=start.y(),
            width=width,
            height=height,
        )
        return self._finish_creation(obj)

    def _math_default_size(self) -> tuple[float, float]:
        """既定 latex の自然寸法（採寸ロジックは `math_item.natural_math_size` に一本化）。"""
        from app.scene.items.math_item import natural_math_size

        defaults = MathObject(id=0)
        return natural_math_size(
            _MATH_DEFAULT_LATEX, defaults.font_size, defaults.color, minimum=_MATH_MIN_SIZE
        )

    # ------------------------------------------------------------------
    # connector: press で直下の BaseItem(.obj 持ち, connector 以外)を source
    # 候補として記録（無ければ固定点=press 座標）、move でプレビュー線を更新、
    # release で target 候補を解決し ConnectorObject を生成する(§5/§9.3)。
    # ------------------------------------------------------------------
    def _pick_connectable(self, scene_pos: QPointF) -> BaseObject | None:
        """scene_pos 直下の「接続可能」なオブジェクトを返す(.obj 持ち, type!='connector')。"""
        hit = self._topmost_item_at(scene_pos)
        obj = getattr(hit, "obj", None)
        if obj is None or obj.type == "connector":
            return None
        return obj

    def _connector_press(self, event: Any, scene_pos: QPointF) -> bool:
        if (
            getattr(event, "button", None) is not None
            and event.button() != Qt.MouseButton.LeftButton
        ):
            return False
        self._cancel_preview()
        self._draw_start = scene_pos
        self._connector_source_obj = self._pick_connectable(scene_pos)
        pen = QPen(QColor("#3399ff"))
        pen.setStyle(Qt.PenStyle.DashLine)
        pen.setCosmetic(True)
        item = QGraphicsLineItem(scene_pos.x(), scene_pos.y(), scene_pos.x(), scene_pos.y())
        item.setPen(pen)
        item.setZValue(1.0e6)
        self.scene.addItem(item)
        self._preview_item = item
        return True

    def _connector_move(self, event: Any, scene_pos: QPointF) -> bool:
        if self._draw_start is None or self._preview_item is None:
            return True
        line_item: QGraphicsLineItem = self._preview_item  # type: ignore[assignment]
        line_item.setLine(self._draw_start.x(), self._draw_start.y(), scene_pos.x(), scene_pos.y())
        return True

    def _connector_release(self, event: Any, scene_pos: QPointF) -> bool:
        start = self._draw_start
        src_obj = self._connector_source_obj
        self._cancel_preview()
        self._connector_source_obj = None
        if start is None:
            return True

        tgt_obj = self._pick_connectable(scene_pos)
        if src_obj is not None and tgt_obj is not None and src_obj.id == tgt_obj.id:
            # source と target が同一オブジェクトなら生成しない。
            return True
        if src_obj is None and tgt_obj is None:
            dx = scene_pos.x() - start.x()
            dy = scene_pos.y() - start.y()
            if math.hypot(dx, dy) < _MOVE_EPS:
                # 固定点同士でドラッグなし(退化コネクタ)は生成しない。
                return True

        document = self.scene.document
        obj = ConnectorObject(
            id=document.new_id(),
            source_id=src_obj.id if src_obj is not None else None,
            target_id=tgt_obj.id if tgt_obj is not None else None,
            source_point=[start.x(), start.y()],
            target_point=[scene_pos.x(), scene_pos.y()],
            source_anchor="nearest",
            target_anchor="nearest",
            # routing は指定しない = モデルの既定（"orthogonal"）を使う。
            # ここで固定すると「エージェントが作った線は避けるが人間が引いた線は
            # 避けない」という分裂が生まれる。
            arrow_end="triangle",
        )
        return self._finish_creation(obj)

    # ------------------------------------------------------------------
    # curve: 左クリックで点を追加し、下書きプレビュー(ライブ曲線+点マーカー)を
    # 更新する。確定は Enter/ダブルクリック/始点近傍クリック(3点以上、closed)/
    # 右クリック(開いたまま)のいずれか、Esc またはツール切替で破棄する(§9)。
    # ------------------------------------------------------------------
    def _curve_button(self, event: Any) -> Qt.MouseButton | None:
        """`event.button()` があれば呼んで返す（無ければ None。move イベント等）。"""
        button_getter = getattr(event, "button", None)
        return button_getter() if callable(button_getter) else None

    @staticmethod
    def _is_left_press(event: Any) -> bool:
        """`event.button()` が左（または `button` 属性を持たない偽イベント）なら True。

        `button` を持たないダックタイプを左扱いにするのは、テストの偽イベントや
        move イベントを従来どおり受け付ける互換のため（`_select_press` の左ボタン
        判定と同じ流儀）。
        """
        button_getter = getattr(event, "button", None)
        if not callable(button_getter):
            return True
        return button_getter() == Qt.MouseButton.LeftButton

    def _curve_press(self, event: Any, scene_pos: QPointF) -> bool:
        button = self._curve_button(event)
        if button == Qt.MouseButton.RightButton:
            if self._curve_points is None:
                # 下書きなしの右クリックは消費しない(既定のコンテキストメニューに委ねる)。
                return False
            # Qt は press の直後に QContextMenuEvent を合成する。確定処理で
            # is_interacting() が False に戻るため、抑止フラグを立てておかないと
            # 確定直後にメニューが出てしまう(`consume_context_menu_suppression` 参照)。
            self._suppress_context_menu_once = True
            return self._finish_curve_draft(closed=False)
        if button is not None and button != Qt.MouseButton.LeftButton:
            # 中ボタン等はパンを妨げないよう非消費のまま無視する。
            return False
        if self._curve_points is None:
            self._curve_points = [scene_pos]
            self._curve_hover = scene_pos
            self._refresh_curve_preview()
            return True
        if len(self._curve_points) >= 3:
            start = self._curve_points[0]
            if (
                math.hypot(scene_pos.x() - start.x(), scene_pos.y() - start.y())
                <= self._curve_close_radius()
            ):
                # 始点近傍クリック(3点以上)は閉じて確定する。
                return self._finish_curve_draft(closed=True)
        last = self._curve_points[-1]
        if math.hypot(scene_pos.x() - last.x(), scene_pos.y() - last.y()) < _CURVE_MIN_DIST:
            # 直前点に近すぎる連打は無視する(消費はする)。
            return True
        self._curve_points.append(scene_pos)
        self._curve_hover = scene_pos
        self._refresh_curve_preview()
        return True

    def _curve_move(self, event: Any, scene_pos: QPointF) -> bool:
        if self._curve_points is None:
            return False
        self._curve_hover = scene_pos
        self._refresh_curve_preview()
        return True

    def _curve_release(self, event: Any, scene_pos: QPointF) -> bool:
        return self._curve_points is not None

    def _curve_double_click(self, event: Any, scene_pos: QPointF) -> bool:
        button = self._curve_button(event)
        if button is not None and button != Qt.MouseButton.LeftButton:
            return False
        self._dedupe_curve_tail()
        return self._finish_curve_draft(closed=False)

    def _build_curve_object(self, points: list[QPointF], *, closed: bool) -> CurveObject | None:
        """下書き点列(2点以上)から CurveObject を組み立てる。2点未満は None。"""
        if len(points) < 2:
            return None
        raw = [[p.x(), p.y()] for p in points]
        x, y, width, height, normalized = curves.normalize_points(raw)
        return CurveObject(
            id=self.scene.document.new_id(),
            x=x,
            y=y,
            width=width,
            height=height,
            points=normalized,
            closed=closed and len(points) >= 3,
        )

    def _finish_curve_draft(self, *, closed: bool) -> bool:
        """下書きを確定する。`closed` は 3 点以上のときのみ有効(2 点なら開いたまま)。"""
        points = self._curve_points
        self._cancel_curve_draft()
        if points is None or len(points) < 2:
            return True
        obj = self._build_curve_object(points, closed=closed)
        if obj is None:
            return True
        return self._finish_creation(obj)

    def _cancel_curve_draft(self) -> None:
        """下書きを破棄する(冪等)。モデルには一切触れない。"""
        if self._curve_preview is not None:
            item_scene = self._curve_preview.scene()
            if item_scene is not None:
                item_scene.removeItem(self._curve_preview)
            self._curve_preview = None
        if self._curve_nodes_preview is not None:
            item_scene = self._curve_nodes_preview.scene()
            if item_scene is not None:
                item_scene.removeItem(self._curve_nodes_preview)
            self._curve_nodes_preview = None
        self._curve_points = None
        self._curve_hover = None

    def _finish_curve_draft_on_tool_change(self) -> None:
        """`set_tool` からのみ呼ぶ: 下書き(2点以上)を確定するが select ツールへは戻さない。

        `set_tool` の実行途中から呼ばれるため、ここで `set_tool("select")` を
        呼ぶ `_finish_creation` を使うと再入する。`_push_creation` だけを使う。
        """
        points = self._curve_points
        self._cancel_curve_draft()
        if points is None or len(points) < 2:
            return
        obj = self._build_curve_object(points, closed=False)
        if obj is not None:
            self._push_creation(obj)

    def _refresh_curve_preview(self) -> None:
        """置いた点＋hover からライブ曲線と点マーカーのプレビューを更新する。

        始点の閉じ判定半径内に hover がある間は、hover を含めず既存点だけで
        閉じたプレビューを描き、始点を大きめの点でハイライトする。
        """
        points = self._curve_points
        if points is None:
            return
        hover = self._curve_hover
        closing = (
            hover is not None
            and len(points) >= 3
            and math.hypot(hover.x() - points[0].x(), hover.y() - points[0].y())
            <= self._curve_close_radius()
        )
        if closing:
            anchor_points = list(points)
        elif hover is not None:
            anchor_points = points + [hover]
        else:
            anchor_points = points

        anchors = [(p.x(), p.y()) for p in anchor_points]
        segments = curves.catmull_rom_segments(
            anchors, closed=closing, tension=curves.DEFAULT_TENSION
        )

        path = QPainterPath()
        if anchors:
            path.moveTo(anchors[0][0], anchors[0][1])
            for c1, c2, end in segments:
                path.cubicTo(c1[0], c1[1], c2[0], c2[1], end[0], end[1])
            if closing:
                path.closeSubpath()

        nodes_path = QPainterPath()
        marker_r = 3.0
        for p in points:
            nodes_path.addEllipse(p, marker_r, marker_r)
        if closing:
            nodes_path.addEllipse(points[0], marker_r * 1.8, marker_r * 1.8)

        if self._curve_preview is None:
            pen = QPen(QColor("#3399ff"))
            pen.setCosmetic(True)
            pen.setWidthF(2.0)
            preview_item = QGraphicsPathItem()
            preview_item.setPen(pen)
            preview_item.setZValue(1000000.0)
            self.scene.addItem(preview_item)
            self._curve_preview = preview_item
        self._curve_preview.setPath(path)

        if self._curve_nodes_preview is None:
            node_pen = QPen(QColor("#3399ff"))
            node_pen.setCosmetic(True)
            nodes_item = QGraphicsPathItem()
            nodes_item.setPen(node_pen)
            nodes_item.setBrush(QBrush(QColor("#ffffff")))
            nodes_item.setZValue(1000001.0)
            self.scene.addItem(nodes_item)
            self._curve_nodes_preview = nodes_item
        self._curve_nodes_preview.setPath(nodes_path)

    def _curve_close_radius(self) -> float:
        """`_CURVE_CLOSE_SCREEN_PX`(画面px) を現在のズーム倍率で scene 単位に換算する。

        `views()` が空(ヘッドレス・テスト)の場合は 1.0 で割る(IndexError 防止。
        つまり画面 px = scene 単位とみなす)。
        """
        views = self.scene.views()
        scale = views[0].transform().m11() if views else 1.0
        if not scale:
            scale = 1.0
        return _CURVE_CLOSE_SCREEN_PX / scale

    def _dedupe_curve_tail(self) -> None:
        """末尾 2 点が `_CURVE_MIN_DIST` 未満なら末尾の点を落とす(ダブルクリックの重複対策)。"""
        if self._curve_points is None or len(self._curve_points) < 2:
            return
        a = self._curve_points[-2]
        b = self._curve_points[-1]
        if math.hypot(b.x() - a.x(), b.y() - a.y()) < _CURVE_MIN_DIST:
            self._curve_points.pop()

    # -- curve 公開 API(CanvasView・エージェント/テストから使う) ----------------

    def has_curve_draft(self) -> bool:
        """curve の下書き中かどうか。"""
        return self._curve_points is not None

    def commit_curve_draft(self, *, closed: bool = False) -> bool:
        """curve の下書きを外部(Enter キー等)から確定する。"""
        return self._finish_curve_draft(closed=closed)

    def cancel_curve_draft(self) -> None:
        """curve の下書きを外部(Esc キー等)から破棄する。"""
        self._cancel_curve_draft()

    def consume_context_menu_suppression(self) -> bool:
        """右クリック確定直後の合成 QContextMenuEvent 抑止フラグを 1 回だけ消費する。

        ワンショット: 読み取ると同時に False に戻す。`CanvasView.contextMenuEvent`
        から呼ぶ想定。
        """
        flag = self._suppress_context_menu_once
        self._suppress_context_menu_once = False
        return flag
