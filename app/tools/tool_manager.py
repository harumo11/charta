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
from app.graphics.constraints import constrain_to_axis_or_diagonal
from app.graphics.routing import Point
from app.graphics.strokes import FILL_TYPES, is_stroked
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
from app.scene.anchor_snap import (
    ANCHOR_REVEAL_SCREEN_PX,
    SNAP_SCREEN_PX,
    AnchorHintOverlay,
    anchor_sets_near,
    find_anchor_snap,
    logical_box_for_item,
    scene_threshold,
)
from app.scene.hit import topmost_item_at
from app.scene.snapping import ALIGN_SNAP_SCREEN_PX, Box, snap_move, union_box

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

# 最小生成サイズのしきい値(px)。浮動小数の厳密等値比較を避けるための距離マージン。
# 描画ツール（rect/ellipse の `_draw_release` クリック判定・line/arrow・freehand・
# connector の生成）の縮退判定**のみ**に使う「明確に動いた」しきい値である。
# select ツールのクリック/ドラッグ判定は画面px換算の `_DRAG_START_SCREEN_PX`
# （`scene_threshold` 経由）を使い、_select_release のコマンド確定ゲートには
# 使わない（下記 _GEOM_NOOP_EPS 参照。task3 2026-09-25: 以前は「移動判定」
# 「クリック/ドラッグの最終フォールバック判定」という select ツール寄りの
# 書き方をしていたが、select 側は現在この定数を一切読まない）。
_MOVE_EPS = 1.0
# _select_release: box/line/connector が「確定すべき実変化を持つか」を見る許容誤差
# （担当X: finding #5 残課題「b'」）。クリック/ドラッグの判定自体は上の
# `_DRAG_START_SCREEN_PX` 閾値が既に一括で行っている(is_drag)ため、ここでは
# 「浮動小数の丸め誤差だけの真の no-op」だけを弾けばよい。以前は `_MOVE_EPS`
# (1.0 scene px)をここでも使っていたため、実ドラッグ後に吸着で正味変位が
# 1px 未満に収まるケース(例: box が 100.4→100.0 に吸着)で、box だけコマンドが
# 落とされて item.pos()(=100.0)と model(=100.4)が食い違ったまま残った
# (line/connector には同じゲートがあるのに box とだけ独立に判定されるため、
# 剛体移動の一部だけ確定し一部だけ据え置かれる不整合も起きていた)。
_GEOM_NOOP_EPS = 1e-6
# select ツール: クリックとドラッグを区別する開始しきい値（画面px、finding #5）。
# `ALIGN_SNAP_SCREEN_PX` と同じ `scene_threshold()` 換算を共有する。これ未満の
# 移動はまだ「クリック」として扱い、吸着もモデルへのライブ反映も行わない――以前は
# 最初の move イベントから即座に union box で吸着していたため、ズーム倍率が高い
# 状況での 1px 程度の手ぶれで box系メンバーだけが吸着後の delta ぶん先に動き、
# line/connector の自由端は追従しない（複数選択/グループの相対配置が崩れる）
# 回帰があった。`QApplication.startDragDistance()`（既定 約10px）は図形の微調整
# には広すぎるため使わない。
_DRAG_START_SCREEN_PX = 3.0
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
#: `fill`/`stroke` のどちらか片方だけが `None`（線・塗りなし、P2契約 項目12）の場合は
#: そのまま記憶対象になる——「線なし矩形を1つ作ると次も線なしになる」のは、`fill=None`
#: の既存の粘り方（塗りなしを作ると次も塗りなし）と同じ意図的な挙動であり、バグではない。
#: ただし `fill` と線（`app.graphics.strokes.is_stroked`）の**両方**が無い、完全に
#: 不可視な組み合わせは `_remember_style` が記憶をスキップする（レビュー finding #3:
#: 矩形の既定が線なし塗りありになったため、パネルで塗りを「なし」にする 1 操作だけで
#: この組み合わせに到達しやすく、記憶されると以後ずっと同種オブジェクトが不可視のまま
#: 作られ続けてしまうため）。
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
        "background",
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


def _event_modifiers(event: Any) -> Qt.KeyboardModifier:
    """`event.modifiers()` を安全に読む。

    テストの疑似イベント（`_FakeEvent`）は `button()` しか持たないため、素で呼ぶと
    AttributeError で既存テストが落ちる（P4/P5契約 (A)）。
    """
    modifiers = getattr(event, "modifiers", None)
    if modifiers is None:
        return Qt.KeyboardModifier.NoModifier
    return modifiers()


def _shift_pressed(event: Any) -> bool:
    """Shift 押下中かどうか（`_event_modifiers` のガード込み）。"""
    return bool(_event_modifiers(event) & Qt.KeyboardModifier.ShiftModifier)


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
        # select ツール: 「もう一度クリックでグループに入る」候補（グループ内個別編集
        # 契約 §F-2）。press 時、掴んだ obj がまだ入っていないグループの一員で、
        # かつそのグループの全メンバーが既に選択済み（=グループ全体が選択されている
        # 状態）のときだけセットする。release で移動量が閾値未満（=ドラッグでなく
        # クリック）ならそのグループへ実際に入る。
        self._group_entry_candidate: BaseObject | None = None
        # select ツール: Ctrl+press で「まだ選択されていない item」を掴んだ場合の
        # 昇格待ち対象（finding #7）。press 時点では選択せず、最初の move で
        # `_promote_ctrl_add_pending` が実際に選択して移動対象を作り直す
        # （Qt 自身の release が「移動していない Ctrl+click」をトグルとして扱う
        # ため、press 時点で選択すると click-no-move で再び OFF に戻ってしまう）。
        self._ctrl_add_pending: BaseObject | None = None
        # select ツール: 移動セッション（吸着契約 §G-4）。press 時に 1 回だけ構築し
        # （対象 id・box系メンバーの開始 box・union box・吸着対象プール）、
        # `_select_move` が union box で 1 回だけ吸着判定して delta を確定する。
        # box系メンバーは `BaseItem._maybe_snap_position`（`scene.session_snapped_position`
        # 経由）が、line/arrow の自由端・connector の固定端点は `_select_move`/
        # `_select_release` 自身がこの delta を使う（finding #1/#6: 以前は「line
        # は両端自由なときだけ・connector は常に raw」だったため剛体性が崩れて
        # いた）。None は「セッション対象なし（吸着 OFF・対象なし等）」。
        self._move_session: dict[str, Any] | None = None
        # select ツール: press からの移動距離が `_DRAG_START_SCREEN_PX` を一度でも
        # 超えたら True に latch する（finding #5）。超えるまでは「クリック」として
        # 扱い、吸着・ライブ追従・確定のいずれも行わない。一度 True になったら
        # release まで戻さない（ドラッグ中にカーソルが press 位置付近へ戻っても
        # ちらつかせないため）。
        self._select_dragging: bool = False
        # select ツール: press で掴んだ item が `ItemIsMovable` を持つか
        # （round2 finding #1）。line/arrow/connector は `ItemIsMovable=False`
        # のため、これらを掴んで複数選択/グループをドラッグしても Qt の既定
        # `mouseMoveEvent` は「掴んだ item が movable」の場合しか選択中の item を
        # 一括で動かさない——掴んだ item 自身が非 movable だと、box系メンバーが
        # movable であっても Qt は何も動かさない。`_select_move` はこのフラグを
        # 見て、非 movable な item を掴んでいるときだけ box系メンバーへ
        # 明示的に `setPos` する（Qt が代わりに動かしてくれない分を埋める）。
        # 既定 True（「box を掴んだ」＝ Qt が普段どおり動かす）にしておき、
        # press で実際の掴み対象に応じて上書きする。
        self._grab_is_movable: bool = True
        # rect/ellipse ツール: 作成ドラッグ中の吸着対象プール（吸着契約 §G-6）。
        # `_draw_press` で 1 回だけ集め、`_draw_move`/`_draw_release` の毎イベントでは
        # 使い回す（押下時に索引を1回だけ作る性能規約、§G-8）。
        self._draw_snap_targets: list[Box] = []
        # rect/ellipse/line/arrow ツール: 描画中のプレビュー
        self._draw_start: QPointF | None = None
        self._preview_item: QGraphicsItem | None = None
        # line/arrow 作図中のアンカー磁石吸着(P4/P5契約 (B) B-4)。press で始点が
        # アンカーへ吸着したときの (obj_id, anchor_name)。p1_id/p1_anchor として
        # LineObject に渡す。p2 側は release 時に都度解決するため持ち越さない。
        self._draw_p1_bind: tuple[int, str] | None = None
        # 作図中のアンカー候補ヒント表示(緑ドット群+吸着ハイライト)。
        # `_cancel_preview` で必ず破棄する。
        self._draw_anchor_hint: AnchorHintOverlay | None = None
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
        """obj の dataclass fields ∩ `_STYLE_KEYS` の現在値を type 別に記憶する。

        塗りも線も無い完全に不可視な rect/ellipse/curve（`app.graphics.strokes.
        FILL_TYPES` と同じ集合。`app.graphics.diagnostics`/`app.agent.diagnose`
        もこれを唯一の真実源として import しており、二重管理を避けるためここも
        同じ定数をそのまま import している）は記憶しない（レビュー finding #3）。
        矩形の既定が「線なし＋薄いグレー塗り」になったことで、パネルの塗り
        スウォッチで「なし」を選ぶ 1 操作だけで fill=None/stroke=None の完全
        不可視スタイルが記憶され、以後ずっと同種オブジェクトが見えないまま
        作られ続ける（File>New でも消えない）回帰があったため。fill=None だけ、
        stroke=None だけを記憶すること自体は意図的な既存挙動（「塗りなしを
        作ると次も塗りなし」と同じ）なので、それは今までどおり残す。
        """
        if obj.type in FILL_TYPES and getattr(obj, "fill", None) is None and not is_stroked(obj):
            return
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
          color（text/math）を `initial_color` に（fill は触らない）。ただし
          **dataclass 既定が None のフィールドは色で上書きしない**（2026-09-25:
          rect/ellipse の stroke 既定が None＝線なしになったため。さもないと
          「現在値 None == 既定 None」の判定で線なしの既定が初期色の線に化ける）。

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
            default = _dataclass_field_default(field)
            if default is None:
                # 「なし」が既定のフィールド（rect/ellipse の stroke）は初期色で埋めない。
                continue
            if getattr(obj, name) == default:
                setattr(obj, name, value)

    def set_tool(self, name: str) -> None:
        if name not in _TOOLS:
            raise ValueError(f"unknown tool: {name}")
        self._cancel_preview()
        self._cancel_freehand()
        self._select_start = {}
        self._select_press_pos = None
        self._group_entry_candidate = None
        self._ctrl_add_pending = None
        self._move_session = None
        self._select_dragging = False
        self._grab_is_movable = True
        end_session = getattr(self.scene, "end_move_session", None)
        if callable(end_session):
            end_session()
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
    def _topmost_item_at(self, scene_pos: QPointF, event: Any = None) -> QGraphicsItem | None:
        """`scene_pos` にある最上位の item を Qt の press ピックと同じ規則で返す。

        実体は `app.scene.hit.topmost_item_at`（task2, 2026-09-25）: 右クリック
        メニュー（`MainWindow._topmost_object_at`）と 1 実装を共有する——以前は
        ここだけがデバイスpx矩形クエリ（round2 finding #3）へ移行しており、
        右クリックメニューは厳密な 1 点クエリのままだったため、図形の縁ぎりぎり
        では「左クリックは掴めるのに右クリックメニューは別のオブジェクトに効く」
        （またはその逆）という食い違いが起きていた。

        `button` は `event.button()` が使えるならそれを、無ければ
        `Qt.MouseButton.LeftButton` を渡す（レビュー3巡目 finding #1）:
        呼び出し元 `_select_press` は既にこの手前で右クリック等を弾いている
        （`event.button() != LeftButton` なら早期 return）ため、ここに来る
        時点の press は常に左クリックだとみなせる。テストの疑似イベントで
        `button()` を持たない呼び方も従来どおり動くよう、無ければ Left を
        既定にする。
        """
        button_getter = getattr(event, "button", None)
        button = button_getter() if callable(button_getter) else Qt.MouseButton.LeftButton
        return topmost_item_at(self.scene, scene_pos, event, button=button)

    def _select_press(self, event: Any, scene_pos: QPointF) -> bool:
        self._select_press_pos = None
        self._select_start = {}
        self._group_entry_candidate = None
        self._ctrl_add_pending = None
        self._select_dragging = False
        if (
            getattr(event, "button", None) is not None
            and event.button() != Qt.MouseButton.LeftButton
        ):
            return False
        hit = self._topmost_item_at(scene_pos, event)
        obj = getattr(hit, "obj", None)
        if obj is None or getattr(obj, "locked", False):
            # 空白部（ラバーバンド選択の開始）またはハンドル操作: 移動をアームしない。
            return False
        # round2 finding #1: 掴んだ item 自身が movable かどうかを記録する。
        # line/arrow/connector は `ItemIsMovable=False` のため、これを掴んで
        # 複数選択/グループをドラッグしても Qt は他の box系メンバーを動かして
        # くれない（`_select_move` がこのフラグを見て代わりに動かす）。
        self._grab_is_movable = bool(hit.flags() & QGraphicsItem.GraphicsItemFlag.ItemIsMovable)

        document = self.scene.document
        entered_group_id: int | None = None
        entered_getter = getattr(self.scene, "entered_group_id", None)
        if callable(entered_getter):
            entered_group_id = entered_getter()

        currently_selected = self.scene.selected_objects()
        group_id = getattr(obj, "group_id", None)

        if obj not in currently_selected and bool(
            _event_modifiers(event) & Qt.KeyboardModifier.ControlModifier
        ):
            # Ctrl+press で「まだ選択されていない item」を掴んだ（finding #7）。
            # ここでは選択しない: Qt 自身の release は「移動していない
            # Ctrl+click」を選択のトグルとして扱うため、ここで先に選択すると
            # release で再び OFF へトグルされてしまう。実際に選択して移動対象を
            # 作り直すのは最初の move イベント（`_select_move` →
            # `_promote_ctrl_add_pending`）まで遅らせる。
            self._ctrl_add_pending = obj

        # グループ内個別編集(グループ内個別編集契約 §F-2): 掴んだ obj がまだ
        # 「入っていない」グループの一員で、かつそのグループの全メンバー(ロック
        # 除く)が既に選択済み(=グループ全体が選択されている状態)なら、
        # 「もう一度クリック」候補として記録する。実際に入るかは release で
        # 移動量を見て判定する(ドラッグならグループとして動かす、クリックなら
        # そのメンバーだけを選ぶ)。修飾キー押下時(Ctrl等での選択の追加/除去)は
        # 対象外にする(§9.1 と同じ getattr ガード経由。Qt 標準の Ctrl+click
        # トグルと意味が衝突しないようにするため、group.md §5 案A3 不採用の理由)。
        if (
            obj in currently_selected
            and group_id is not None
            and group_id != entered_group_id
            and _event_modifiers(event) == Qt.KeyboardModifier.NoModifier
        ):
            # not locked だけでなく visible も要る（review finding #7）: 非表示メンバーを
            # 含むグループは、Qt が非表示アイテムを選択できない以上「ロックのみ」判定だと
            # 永久に全体選択へ到達できず、もう一度クリックしての個別編集に入れなかった。
            unlocked_members = document.selectable_group_members(group_id)
            if unlocked_members and all(m in currently_selected for m in unlocked_members):
                self._group_entry_candidate = obj

        if obj in currently_selected:
            # 既に選択済みの item を掴んだ場合、Qt はここでは選択を変えない
            # （press だけでは選択が変化しない）ため、移動対象は「現在選択されて
            # いるものそのもの」でなければならない（finding #3、パートナー機能の
            # 選択が壊れる回帰の修正）。以前は複数選択（len > 1）のときだけこの
            # 分岐に入っていたが、単一選択でも同じ理由が成り立つ——単一選択時に
            # else 分岐（グループ全体へ広げる）に落ちると、「一部だけ選択された
            # 非 entered グループ」で 1 個だけドラッグしたときに Qt が動かさない
            # 他メンバーまで移動対象に含めてしまい、モデルと画面がずれる。
            # `rigid_group_targets`（要望10 追加決定 Option A）: `currently_selected`
            # は Qt の選択なので非表示メンバーを絶対に含まない（`setSelected` が
            # 非表示アイテムに無言で no-op なため）。選択が「あるグループの選べる
            # 全メンバー」を指しているなら、その非表示メンバーも移動対象へ足す
            # ——さもないとグループを掴んでドラッグしても非表示メンバーだけ
            # その場に取り残される。
            targets = self.scene.rigid_group_targets(currently_selected)
        else:
            # M7契約 §7 / グループ内個別編集契約 §F-2: 掴んだ obj がグループに
            # 属するなら、グループ全体を移動アーム対象にする(グループ選択自体の
            # 拡張は scene 側の責務。ここでは undo 用の旧幾何記録の対象を広げる
            # だけ)。ただし「そのグループに既に入っている」間は広げない
            # ——入っている間はメンバー単体だけを動かす(§F-2 の中核)。
            # `Document.movable_group_members`（要望10 追加決定 Option A）:
            # ロックされていなければ非表示でも移動対象に含める（非表示メンバーは
            # 選択も当たり判定もできないが、グループとしては剛体で動く）。
            if group_id is not None and group_id != entered_group_id:
                targets = document.movable_group_members(group_id)
                if not targets:
                    targets = [obj]
            else:
                targets = [obj]

        self._select_press_pos = scene_pos
        self._select_start = self._record_move_starts(targets)
        self._move_session = self._build_move_session(targets)
        begin_session = getattr(self.scene, "begin_move_session", None)
        if callable(begin_session):
            box_starts = self._move_session["box_starts"] if self._move_session else {}
            begin_session(box_starts)
        return False

    def _record_move_starts(self, targets: list[BaseObject]) -> dict[int, dict[str, Any]]:
        """`targets` それぞれの移動開始時点の旧幾何を `_select_start` 形式で返す。

        `_select_press` と `_promote_ctrl_add_pending`（finding #7: Ctrl+drag で
        未選択の item を掴んだ場合の移動対象の作り直し）が同じロジックを共有する
        （二重実装によるドリフトを避けるため、ここへ抽出した）。
        """
        starts: dict[int, dict[str, Any]] = {}
        for target in targets:
            if getattr(target, "locked", False):
                continue
            if target.type == "connector":
                # 本体ドラッグでは固定端点を平行移動する(コネクタ編集UX契約 §3)。
                starts[target.id] = {
                    "_connector": True,
                    "source_point": list(target.source_point),
                    "target_point": list(target.target_point),
                }
            elif geometry_kind(target.type) == "endpoints":
                starts[target.id] = {"p1": list(target.p1), "p2": list(target.p2)}
            else:
                starts[target.id] = {"x": target.x, "y": target.y}
        return starts

    def _promote_ctrl_add_pending(self) -> None:
        """Ctrl+press で掴んだ未選択の item を、最初の move で実際に選択し直す（finding #7）。

        Qt の `QGraphicsItem::mouseMoveEvent` は最初の move イベントで
        `movingItemsInitialPositions` のスナップショットを「現在選択されている
        item 全部 + 押された item」から取り、その**後**に押された item を選択する。
        `CanvasView.mouseMoveEvent` は `tool_manager.handle_mouse_move`（＝この
        メソッドを含む select move 処理）を `super().mouseMoveEvent()`（Qt 自身の
        処理、上記スナップショット）より**前**に呼ぶ。したがって、ここで
        Qt より先に実際に選択してしまえば、Qt 自身がスナップショットを取る
        時点で既に「選択されている item」に含まれ、以後の Qt のドラッグと
        `ToolManager` の移動対象・移動セッションが同じ集合を動かすようになる。

        これをしないと 2 通りの desync が起きる:
        (1) 元から選択されていた他の item は画面上では Qt に動かされるが、
            `_select_start`/`_select_release` の対象に入っていないためモデルへ
            コミットされない。
        (2) 掴んだ item がグループの一員なら `_expand_group_selection` が
            兄弟も選択に加えるが、Qt のスナップショットには乗っていないため
            (0, 0) + delta へ瞬間移動し、それがそのままモデルへコミットされる。

        素の Ctrl+click（move が一度も来ない）ではこのメソッドは一度も呼ばれない
        ため、Qt 自身の release（移動していない Ctrl+click は選択をトグルする）が
        そのまま働く——ここで先回りして選択してしまうと release で再び OFF へ
        トグルされてしまうため、意図的に press ではなく最初の move まで遅らせて
        いる（`_select_press` 側のコメント参照）。
        """
        obj = self._ctrl_add_pending
        if obj is None:
            return
        self._ctrl_add_pending = None
        item = self.scene.item_for(obj)
        if item is not None:
            item.setSelected(True)
        # `rigid_group_targets`（要望10 追加決定 Option A）: Ctrl+drag で選択に
        # 加えた結果が「あるグループの選べる全メンバー」を指すなら、そのグループの
        # 非表示メンバーも移動対象へ足す（`_select_press` の同種コメント参照）。
        selected = [o for o in self.scene.selected_objects() if not getattr(o, "locked", False)]
        targets = self.scene.rigid_group_targets(selected)
        self._select_start = self._record_move_starts(targets)
        self._move_session = self._build_move_session(targets)
        end_session = getattr(self.scene, "end_move_session", None)
        if callable(end_session):
            end_session()
        begin_session = getattr(self.scene, "begin_move_session", None)
        if callable(begin_session):
            box_starts = self._move_session["box_starts"] if self._move_session else {}
            begin_session(box_starts)

    def _build_move_session(self, targets: list[BaseObject]) -> dict[str, Any] | None:
        """吸着契約 §G-4: 選択移動セッションを press 時に 1 回だけ構築する。

        union box（吸着判定の基準）に入れるのは box系メンバー全員と、line/arrow の
        うち**両端とも自由**（`pN_id` が両方 None）なもの――接着端のある line は
        動かした瞬間にその端も一緒に動く「その他の図形」の影響を受けるため、
        自分自身の位置で吸着基準を作ると意味を成さない。connector も同じ理由で
        union box には含めない（固定端点はあっても、本体そのものが接続先に
        追従して動くため吸着基準にはできない）。ただし、どちらも union box から
        除外されるだけで**移動対象（`_select_start`）ではあり続け**、他のメンバー
        と同じセッションの delta がそのまま適用される（finding #1/#6: 以前は
        「line は両端自由なときだけ・connector は常に raw delta」だったため、
        セッション内で box が吸着した分だけ自由端・固定端点が追従せず、剛体移動
        が崩れる回帰があった）。

        吸着 OFF、または union box を構成できるメンバーが1つも無ければ None
        （`_select_move`/`_select_release` は raw delta にフォールバックする）。

        非表示メンバー（要望10 追加決定 Option A）は union box（吸着の基準・
        吸着対象）には寄与しない——不可視なので、見えている図形どうしを揃える
        吸着の根拠にはならない。ただし `box_starts` には残す: `_select_move` の
        「掴んだ item が非movable」経路は非表示の box系メンバーへも明示 `setPos`
        するため（`_grab_is_movable` 分岐）、`box_starts` に無いと
        `session_snapped_position` が None を返し、その非表示メンバーだけ
        `BaseItem._maybe_snap_position` の単独判定に落ちて他メンバーと違う delta
        で独立にスナップしてしまう（剛体移動が崩れる）。
        """
        if not getattr(self.scene, "snap_enabled", False):
            return None
        ids = frozenset(t.id for t in targets)
        union: Box | None = None
        box_starts: dict[int, tuple[float, float]] = {}
        for obj in targets:
            if obj.type == "connector":
                continue
            if geometry_kind(obj.type) == "endpoints" and (
                obj.p1_id is not None or obj.p2_id is not None
            ):
                continue
            box = self.scene.snap_box_for_object(obj)
            if box is None:
                continue
            if geometry_kind(obj.type) != "endpoints":
                box_starts[obj.id] = (obj.x, obj.y)
            if not obj.visible:
                continue
            union = box if union is None else union_box(union, box)
        if union is None:
            return None
        target_boxes = self.scene.collect_snap_targets(exclude_ids=ids)
        return {
            "ids": ids,
            "union_box": union,
            "target_boxes": target_boxes,
            "box_starts": box_starts,
            "delta": None,
        }

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

        先頭で `_promote_ctrl_add_pending()`（finding #7）を呼ぶ: Ctrl+press で
        未選択の item を掴んでいた場合、Qt 自身がこの move イベントを処理する
        （`CanvasView.mouseMoveEvent` の `super().mouseMoveEvent()`）より前に
        実際へ選択し、移動対象・移動セッションを作り直す。

        press からの移動量が `_DRAG_START_SCREEN_PX` 未満の間は「まだクリック」
        として扱い、吸着判定・ライブ追従のいずれも行わない（finding #5）。
        セッションがあれば delta を `(0.0, 0.0)` に固定して公開する――box系
        メンバーは `BaseItem._maybe_snap_position` がこれを見て開始位置に留まる
        （Qt 自身の生ドラッグを追認しない）。line/connector は `set_live_points`/
        `set_live_body_offset` を一度も呼ばなければ press 時点のモデル座標の
        ままなので、ここでは何もしないだけでよい。閾値を一度でも超えたら
        `_select_dragging` を latch し、以後は release まで通常のドラッグとして
        扱う（閾値付近でのちらつき防止）。
        """
        self._promote_ctrl_add_pending()
        if self._select_press_pos is None or not self._select_start:
            return False
        dx = scene_pos.x() - self._select_press_pos.x()
        dy = scene_pos.y() - self._select_press_pos.y()
        if not self._select_dragging:
            threshold = scene_threshold(self.scene, _DRAG_START_SCREEN_PX)
            if math.hypot(dx, dy) < threshold:
                session = self._move_session
                if session is not None:
                    session["delta"] = (0.0, 0.0)
                    update_delta = getattr(self.scene, "update_move_session_delta", None)
                    if callable(update_delta):
                        update_delta((0.0, 0.0))
                self._clear_snap_guides()
                return False
            self._select_dragging = True
        # 吸着契約 §G-4: セッションがあれば union box で 1 回だけ吸着判定し、
        # box系メンバー全員・line/arrow(自由端)・connector(固定端点)へ同じ delta
        # を適用する（複数選択/グループで各メンバーが独立に吸着してバラバラに
        # なる問題の修正、finding #1/#6）。box系メンバー自身への適用は
        # `BaseItem._maybe_snap_position`（`scene.session_snapped_position` 経由）
        # が担当し、ここでは line/arrow/connector のライブ追従にだけ使う。
        snap_dx, snap_dy = dx, dy
        session = self._move_session
        if session is not None:
            if getattr(self.scene, "snap_enabled", False):
                union_start = session["union_box"]
                threshold = scene_threshold(self.scene, ALIGN_SNAP_SCREEN_PX)
                grid_size = self.scene.grid_size_or_none()
                proposed_xy = (union_start[0] + dx, union_start[1] + dy)
                (snapped_x, snapped_y), guides = snap_move(
                    union_start, proposed_xy, session["target_boxes"], grid_size, threshold
                )
                snap_dx = snapped_x - union_start[0]
                snap_dy = snapped_y - union_start[1]
                session["delta"] = (snap_dx, snap_dy)
                self.scene.set_snap_guides(guides)
                update_delta = getattr(self.scene, "update_move_session_delta", None)
                if callable(update_delta):
                    update_delta(session["delta"])
            else:
                # ドラッグ中に吸着が OFF に切り替わった場合のフォールバック
                # （通常は press 時点の状態で固定されるが、念のため raw に戻す）。
                session["delta"] = None
                self._clear_snap_guides()
        document = self.scene.document
        if not self._grab_is_movable:
            # round2 finding #1: 掴んだ item（line/arrow/connector）が
            # `ItemIsMovable=False` だと、Qt の既定 `mouseMoveEvent` は
            # box系メンバーを一切動かさない（掴んだ item 自身が movable の
            # ときだけ選択中の item をまとめて動かす実装のため）。ここで
            # box系メンバーへ明示的に `setPos` して埋める。`setPos` は
            # `itemChange(ItemPositionChange)` 経由で `BaseItem.
            # _maybe_snap_position` を通るため、セッションがあれば
            # `session_snapped_position` が union box 吸着後の同じ delta を
            # 返し（box を直接掴んだときと同一の経路・同一の結果）、セッションが
            # 無ければ（吸着 OFF）None が返って proposed（= raw delta）が
            # そのまま使われる。ライブプレビューになるので release まで
            # 待たずに他メンバーと一緒に動いて見える。
            for oid, geom in self._select_start.items():
                if "x" not in geom:
                    continue
                target_obj = document.object_by_id(oid)
                if target_obj is None:
                    continue
                target_item = self.scene.item_for(target_obj)
                if target_item is None:
                    continue
                # crop/テキスト編集中などで一時的に movable を外されたメンバー
                # は Qt 自身も動かさないので、ここでも動かさない（Qt の挙動に
                # 合わせる。round2 finding #1 FIX 手順 2）。
                if not (target_item.flags() & QGraphicsItem.GraphicsItemFlag.ItemIsMovable):
                    continue
                target_item.setPos(geom["x"] + snap_dx, geom["y"] + snap_dy)
        for oid, geom in self._select_start.items():
            obj = document.object_by_id(oid)
            if obj is None:
                continue
            item = self.scene.item_for(obj)
            if geom.get("_connector"):
                # セッションがあれば union box での吸着後 delta を使う（他の
                # box/line メンバーと同じ delta で剛体移動する、finding #1/#6）。
                # セッションが無い（union box を構成する box/free line が選択に
                # 無い、例えば connector 単体のドラッグ）なら snap_dx/dy は raw
                # dx/dy のまま――そのときは従来どおり吸着しない
                # （report snap.md §6: 「Connector body drag doesn't snap either,
                # which is fine」は単体ドラッグの話であり、セッション内では
                # 適用されない）。
                set_live_body_offset = getattr(item, "set_live_body_offset", None)
                if callable(set_live_body_offset):
                    set_live_body_offset(snap_dx, snap_dy)
                continue
            if "p1" not in geom:
                continue
            set_live_points = getattr(item, "set_live_points", None)
            if not callable(set_live_points):
                continue
            p1 = geom["p1"]
            p2 = geom["p2"]
            # 自由端は常にセッションの delta を使う（finding #1/#6: 以前は
            # 「両端とも自由なときだけ」吸着後の delta を使い、片端でも接着済みの
            # line の自由端は生の dx/dy のままだったため、box が吸着で数px
            # 動いたのに自由端だけ追従せず剛体性が崩れていた）。セッションが
            # 無ければ snap_dx/dy は raw dx/dy のままなので、単体ドラッグ
            # （接着済み line 1本だけを選択して動かす等）の挙動は変わらない。
            line_dx, line_dy = snap_dx, snap_dy
            # 接着端（p1_id/p2_id が非 None、項目8）は本体ドラッグで動かさない
            # （`ConnectorItem.set_live_body_offset` の固定端点のみ動かす規則と同じ、
            # §9.3）。両端とも接着済みなら動かせる自由端が無いため呼び出し自体を
            # 省略する（`set_live_points()` は引数無しでも無条件に geometryChanged を
            # emit するため、何も変わらないのに接続先へ空更新を撒くのを避ける）。
            live_kwargs: dict[str, list[float]] = {}
            if obj.p1_id is None:
                live_kwargs["p1"] = [p1[0] + line_dx, p1[1] + line_dy]
            if obj.p2_id is None:
                live_kwargs["p2"] = [p2[0] + line_dx, p2[1] + line_dy]
            if live_kwargs:
                set_live_points(**live_kwargs)
        return False

    def _select_release(self, event: Any, scene_pos: QPointF) -> bool:
        start_pos = self._select_press_pos
        old_geoms = self._select_start
        candidate = self._group_entry_candidate
        session = self._move_session
        self._select_press_pos = None
        self._select_start = {}
        self._group_entry_candidate = None
        # 移動が一度も無い（move イベント無し）Ctrl+click では
        # `_promote_ctrl_add_pending` が一度も呼ばれないため、ここで確実に
        # リセットする（finding #7。次の press まで残ると別の item への
        # press に誤って持ち越されかねない）。
        self._ctrl_add_pending = None
        self._move_session = None
        self._clear_snap_guides()
        end_session = getattr(self.scene, "end_move_session", None)
        if callable(end_session):
            end_session()
        dx: float | None = None
        dy: float | None = None
        if start_pos is not None:
            dx = scene_pos.x() - start_pos.x()
            dy = scene_pos.y() - start_pos.y()
        # finding #5: 「クリック」か「ドラッグ」かを 1 回だけ判定する。通常は
        # `_select_move` が閾値超過時に latch した `_select_dragging` をそのまま
        # 使う。move イベントが一度も来ていない（`item.setPos()` を直接呼ぶ
        # 既存テスト・プログラム的な移動など）場合のフォールバックとして、
        # 生の press→release 距離でも判定する。
        drag_threshold = scene_threshold(self.scene, _DRAG_START_SCREEN_PX)
        is_drag = self._select_dragging or (dx is not None and math.hypot(dx, dy) >= drag_threshold)
        # round2 finding #1 step3: `_select_dragging` を False に戻す前に捕まえて
        # おく。box系メンバーの確定に使う（`_select_dragging` が True＝実際に
        # `_select_move` を通る real drag が起きた）。
        moved_via_select_move = self._select_dragging
        self._select_dragging = False
        # 次の press まで持ち越さない（`_select_press` が改めて実際の掴み対象で
        # 上書きする。ここでリセットしないと、次に box を直接掴んで動かす前に
        # このメソッドを介さない経路（テストで直接呼ぶ等）が残っている場合に
        # 古い値が残る）。
        self._grab_is_movable = True
        # 吸着契約 §G-4: line/arrow(自由端)・connector(固定端点) の確定は、
        # プレビューと同じ `_select_move` が最後に計算した delta を再利用する
        # （生の dx/dy を使うとプレビューは吸着していたのに確定した線だけ
        # 吸着前へずれる、§9.1 の Shift 制約と同じ「プレビューと確定は同じ値を
        # 使う」規則。finding #1/#6）。セッションが無い/delta 未確定
        # （`_select_move` が一度も走っていない、例えば setPos を直接呼ぶ既存
        # テスト）なら生の dx/dy にフォールバックする。
        move_dx, move_dy = dx, dy
        if session is not None and session.get("delta") is not None and dx is not None:
            move_dx, move_dy = session["delta"]
        if candidate is not None and not is_drag:
            # グループ内個別編集契約 §F-2: ドラッグではなくクリックだったので、
            # 候補のグループへ実際に入り、その候補だけへ選択を絞り込む。
            # 絞り込みを Qt 自身の release 処理に委ねない（finding #6）:
            # Qt は release の scenePos が press の scenePos と厳密に一致した
            # ときだけ選択を絞り込むため、ズーム倍率が高い状況で 1〜2 device px
            # 程度の手ぶれ（`is_drag` が False＝`scene_threshold(_DRAG_START_
            # SCREEN_PX)` 未満でドラッグとは判定しない範囲。task3 2026-09-25:
            # ここは `_MOVE_EPS` ではなく画面px換算のこの閾値で判定している）が
            # あると、「入っている」状態にはなるのに選択はグループ全体のまま
            # ——という中途半端な状態が生じていた。`select_exactly` を直接
            # 呼んで確実に候補だけへ絞り込む。ここは Qt 既定の release 処理
            # (`super().mouseReleaseEvent`)より**前**に走る(`CanvasView.
            # mouseReleaseEvent` の呼び出し順、報告書 group.md §3)。先にここで
            # 選択と「入っている」フラグを確定させておくことで、直後に Qt が
            # scenePos 厳密一致で行う絞り込み（起きる場合）は同じ結果を
            # 再現するだけの idempotent な操作になる。
            select_exactly = getattr(self.scene, "select_exactly", None)
            if callable(select_exactly):
                select_exactly([candidate])
            else:
                set_entered_group = getattr(self.scene, "set_entered_group", None)
                if callable(set_entered_group):
                    set_entered_group(candidate.group_id)
        if not old_geoms:
            return False
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
                # finding #1/#6: セッションがあれば move_dx/move_dy（吸着後の
                # delta、無ければ raw dx/dy と同じ）を使う。以前は常に raw
                # dx/dy を使っていたため、同じセッション内の box/line が吸着
                # 分だけ動いたのに connector の固定端点だけ追従せず、剛体移動
                # が崩れていた。
                item = self.scene.item_for(obj)
                if is_drag and dx is not None and math.hypot(move_dx, move_dy) >= _GEOM_NOOP_EPS:
                    if obj.source_id is None:
                        old_sp = old_geom["source_point"]
                        new_sp = [old_sp[0] + move_dx, old_sp[1] + move_dy]
                        # mergeable=False（レビュー3巡目 finding #9）: この
                        # コマンドが単独（`len(commands) == 1`）で push される
                        # と `undo_stack.push` はマクロを介さないため、既定の
                        # mergeable=True のままだと半接着コネクタの本体ドラッグを
                        # 2回別々に行っても隣接する同一 (obj.id, key) の
                        # `SetPropertyCommand` 同士が1エントリへ吸収されてしまう
                        # （`tests/test_line_anchor_snap.py` が既に理由付きで
                        # 明記していた既知の落とし穴）。
                        commands.append(
                            SetPropertyCommand(
                                document,
                                obj,
                                "source_point",
                                new_sp,
                                list(old_sp),
                                mergeable=False,
                            )
                        )
                    if obj.target_id is None:
                        old_tp = old_geom["target_point"]
                        new_tp = [old_tp[0] + move_dx, old_tp[1] + move_dy]
                        commands.append(
                            SetPropertyCommand(
                                document,
                                obj,
                                "target_point",
                                new_tp,
                                list(old_tp),
                                mergeable=False,
                            )
                        )
                if item is not None:
                    clear_live = getattr(item, "clear_live", None)
                    if callable(clear_live):
                        clear_live()
                continue
            if "p1" in old_geom:
                # LineItem は ItemIsMovable=False で pos を持たないため、delta を
                # 生の pos() 差分から取れず別経路で求める他ない（§6.2）。ここで
                # 使う `move_dx`/`move_dy` は生マウス移動量そのものではなく、
                # 移動セッション（吸着契約 §G-4）があればその吸着後delta、
                # 無ければ raw dx/dy にフォールバックする値（このメソッド冒頭の
                # `move_dx, move_dy = dx, dy` / `session["delta"]` 参照。
                # task3 2026-09-25: 旧コメントは常に「実マウス移動量」と読めたが
                # セッションがある限りそうではない）。
                item = self.scene.item_for(obj)
                if is_drag and dx is not None and math.hypot(move_dx, move_dy) >= _GEOM_NOOP_EPS:
                    p1 = old_geom["p1"]
                    p2 = old_geom["p2"]
                    # finding #1/#6: 自由端は常にセッションの delta（move_dx/dy）
                    # を使う。以前は「両端とも自由なときだけ」吸着後の delta を
                    # 使い、片端でも接着済みの line の自由端は生の dx/dy の
                    # ままだったため、同じセッション内で box が吸着した分だけ
                    # 剛体移動が崩れていた。セッションが無ければ move_dx/dy は
                    # raw dx/dy と同じなので、単体ドラッグの挙動は変わらない。
                    line_dx, line_dy = move_dx, move_dy
                    # 接着端（p1_id/p2_id が非 None、項目8）は本体ドラッグで動かさない
                    # （コネクタの固定端点のみ書き戻す規則と同じ、§9.3）。redo/undo は
                    # 同じキー集合を触る必要があるため、new/old は接着していない側の
                    # キーだけを含む部分辞書にする。両端とも接着済みなら動かせる自由端が
                    # 無いため no-op（コマンドを積まない）。
                    new_partial: dict[str, list[float]] = {}
                    old_partial: dict[str, list[float]] = {}
                    if obj.p1_id is None:
                        new_partial["p1"] = [p1[0] + line_dx, p1[1] + line_dy]
                        old_partial["p1"] = p1
                    if obj.p2_id is None:
                        new_partial["p2"] = [p2[0] + line_dx, p2[1] + line_dy]
                        old_partial["p2"] = p2
                    if new_partial:
                        commands.append(SetGeometryCommand(document, obj, new_partial, old_partial))
                if item is not None:
                    # ドラッグが確定した(=コマンドを積む)場合は`SetGeometryCommand`の
                    # redo が `sync_from_model()` 経由で自然にクリアするが、閾値未満で
                    # 何も push しない場合はそれが起きない。`_select_move` が設定した
                    # `_live_p1`/`_live_p2` を明示的に解除しないと、次に接着先が
                    # `geometryChanged` を出すたびに自由端がこの古いライブ値へ固定
                    # されたままになる（`ConnectorItem` の `clear_live()` と同じ
                    # 位置づけ、レビュー major所見の追加対応）。
                    clear_live_points = getattr(item, "clear_live_points", None)
                    if callable(clear_live_points):
                        clear_live_points()
                continue
            else:
                # rect/ellipse は ItemIsMovable=True で Qt がドラッグ中に item.pos()
                # を実際に動かすため、生マウス delta ではなく実移動量を使う。
                item = self.scene.item_for(obj)
                if not is_drag:
                    # finding #5: 閾値未満の「クリック」では確定しない。吸着 ON
                    # ならセッションが delta=(0,0) を公開しているため item.pos()
                    # は既に old_geom と一致しているはずだが、吸着 OFF では
                    # セッションによる位置の巻き戻しが働かず Qt の生ドラッグが
                    # そのまま反映されている（既存の view/model 乖離バグ、
                    # finding #5 の「side issue」）。ここで明示的に元へ戻す。
                    if item is not None:
                        sync_from_model = getattr(item, "sync_from_model", None)
                        if callable(sync_from_model):
                            sync_from_model()
                    continue
                if moved_via_select_move and move_dx is not None and move_dy is not None:
                    # round2 finding #1 step3: 実ドラッグが `_select_move` を
                    # 通った（＝ `_select_dragging` が latch された）なら、
                    # プレビューと同じ確定 delta（move_dx/move_dy。セッション
                    # ありなら吸着後、無しなら raw）から求める。掴んだ item が
                    # 非 movable（line/arrow/connector）で box系メンバーを
                    # `_select_move` 側で `setPos` した場合も、直接 box を
                    # 掴んだ場合も同じ式になる（round1 #31 step3 の意図。
                    # `item.pos()` に頼ると、box を直接掴んでいない経路で
                    # Qt 自身が動かしていない可能性に依存してしまう）。
                    new_x, new_y = old_geom["x"] + move_dx, old_geom["y"] + move_dy
                elif item is not None:
                    new_x, new_y = item.pos().x(), item.pos().y()
                elif dx is not None:
                    new_x, new_y = old_geom["x"] + dx, old_geom["y"] + dy
                else:
                    continue
                if math.hypot(new_x - old_geom["x"], new_y - old_geom["y"]) < _GEOM_NOOP_EPS:
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
        if self._tool in ("line", "arrow") and not _shift_pressed(event):
            # 始点がアンカー候補の近くなら吸着する(P4/P5契約 (B) B-4)。Shift 押下中は
            # 吸着しない(B-3 と同じ規則: 45°制約と磁石を同時に効かせると
            # どちらの規則にも従わない点になる)。
            snap = self._find_draw_anchor_snap(scene_pos)
            if snap is not None:
                anchor_point, obj_id, anchor_name = snap
                scene_pos = QPointF(anchor_point[0], anchor_point[1])
                self._draw_p1_bind = (obj_id, anchor_name)
        if self._tool in ("rect", "ellipse") and getattr(self.scene, "snap_enabled", False):
            self._draw_snap_targets = self.scene.collect_snap_targets()
        else:
            self._draw_snap_targets = []
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

    def _draw_end_point(self, event: Any, scene_pos: QPointF) -> QPointF:
        """line/arrow の作図中の終点。Shift 押下中は45°刻みへ制約する（項目2）。

        `_draw_move`（プレビュー）と `_draw_release`（確定）の**両方**がこれを通す。
        片方だけだと「見た目は45°なのにできた線は斜め」になる。
        rect/ellipse は対象外（正方形/正円はユーザー要望に無い）。
        """
        if self._tool not in ("line", "arrow") or self._draw_start is None:
            return scene_pos
        if not _shift_pressed(event):
            return scene_pos
        anchor = (self._draw_start.x(), self._draw_start.y())
        point = (scene_pos.x(), scene_pos.y())
        constrained = constrain_to_axis_or_diagonal(anchor, point)
        return QPointF(constrained[0], constrained[1])

    def _snap_draw_corner(self, scene_pos: QPointF) -> QPointF:
        """rect/ellipse 作成中のドラッグ角を吸着する（吸着契約 §G-6）。

        `_draw_move`（プレビュー）と `_draw_release`（確定）の**両方**がこれを通す
        （`_draw_end_point` の Shift 制約と同じ「プレビューと確定は同じ関数を通す」
        規則、§9.1）。line/arrow はアンカー磁石が既に優先されるため対象外（契約の
        明示スコープ。`_find_draw_anchor_snap`/`_update_draw_anchor_hint` を使う）。

        角を「サイズ0の box」として扱うことで、既存の `snapping.snap_move` を
        そのまま再利用できる（左/中央/右が全て同じ値になり、最も近い整列線に
        素直に吸着する）。

        吸着対象プールは `_draw_press` が押下時に 1 回だけ集めたもの
        （`_draw_snap_targets`）を使い回す（§G-8: 索引は押下時に1回だけ作る）。
        """
        if self._tool not in ("rect", "ellipse"):
            return scene_pos
        if not getattr(self.scene, "snap_enabled", False):
            return scene_pos
        point_box: Box = (scene_pos.x(), scene_pos.y(), 0.0, 0.0)
        grid_size = self.scene.grid_size_or_none()
        threshold = scene_threshold(self.scene, ALIGN_SNAP_SCREEN_PX)
        (sx, sy), guides = snap_move(
            point_box, (scene_pos.x(), scene_pos.y()), self._draw_snap_targets, grid_size, threshold
        )
        self.scene.set_snap_guides(guides)
        return QPointF(sx, sy)

    def _draw_move(self, event: Any, scene_pos: QPointF) -> bool:
        if self._tool in ("line", "arrow") and self._draw_start is None:
            # press 前のホバー中でもヒントを更新する（レビュー major所見）。
            # 始点の接着可否は press の瞬間に判定される（`_draw_press`）ため、
            # ホバー中にヒントが一度も出ないまま接着が確定してしまうと、
            # 図形の角の近くから線を引き始めただけで無自覚に接着される事故に
            # なる。`_preview_item` はまだ無いので早期 return より前に処理する。
            self._update_draw_anchor_hint(event, scene_pos)
            return True
        if self._draw_start is None or self._preview_item is None:
            return True
        scene_pos = self._draw_end_point(event, scene_pos)
        if self._tool in ("line", "arrow"):
            line_item: QGraphicsLineItem = self._preview_item  # type: ignore[assignment]
            snap = self._update_draw_anchor_hint(event, scene_pos)
            # プレビューの終点も吸着先に合わせる（レビュー所見: ヒントは吸着先を
            # ハイライトするのにプレビュー線自体はマウス位置のままだと、確定
            # (`_draw_release`)の瞬間に線が跳んで見える）。`_update_draw_anchor_hint`
            # が既に同じ探索をしているので、その戻り値を使い回し走査を増やさない。
            end_point = QPointF(snap[0][0], snap[0][1]) if snap is not None else scene_pos
            line_item.setLine(
                self._draw_start.x(), self._draw_start.y(), end_point.x(), end_point.y()
            )
        else:
            rect_item: QGraphicsRectItem = self._preview_item  # type: ignore[assignment]
            scene_pos = self._snap_draw_corner(scene_pos)
            rect_item.setRect(QRectF(self._draw_start, scene_pos).normalized())
        return True

    def _find_draw_anchor_snap(self, point: QPointF) -> tuple[Point, int, str] | None:
        """作図中の終点(または始点)近くの磁石スナップ先を探す（B-4。`EndpointHandleSet`/
        `LineItem._find_snap` と同じ距離換算だが、まだ line 自体が存在しないため
        `exclude` は渡さない。
        """
        threshold = scene_threshold(self.scene, SNAP_SCREEN_PX)
        return find_anchor_snap(self.scene, (point.x(), point.y()), threshold=threshold)

    def _update_draw_anchor_hint(
        self, event: Any, scene_pos: QPointF
    ) -> tuple[Point, int, str] | None:
        """line/arrow 作図（またはその前のホバー）中、終点付近のアンカー候補ヒントを
        表示する（B-4）。**吸着先（`_find_draw_anchor_snap` の結果）を返す**——
        呼び出し側（`_draw_move`）がプレビュー線の終点をこれで合わせられるように
        するため（同じ探索を2回走らせない）。

        Shift 押下中は磁石吸着を行わない（B-3 と同じ規則）ため、ヒントも消す
        （吸着しないのに候補ドットだけ出るとユーザーを誤誘導する）。
        """
        if _shift_pressed(event):
            if self._draw_anchor_hint is not None:
                self._draw_anchor_hint.destroy()
                self._draw_anchor_hint = None
            return None
        point = (scene_pos.x(), scene_pos.y())
        reveal = scene_threshold(self.scene, ANCHOR_REVEAL_SCREEN_PX)
        candidates = anchor_sets_near(self.scene, point, reveal)
        snap = self._find_draw_anchor_snap(scene_pos)
        if not candidates and snap is None:
            if self._draw_anchor_hint is not None:
                self._draw_anchor_hint.destroy()
                self._draw_anchor_hint = None
            return None
        if self._draw_anchor_hint is None:
            self._draw_anchor_hint = AnchorHintOverlay(self.scene)
        self._draw_anchor_hint.update_hints(candidates, snap)
        return snap

    def _draw_release(self, event: Any, scene_pos: QPointF) -> bool:
        # `_cancel_preview()` は `_draw_start`/`_draw_p1_bind` を None にするため、
        # 制約・吸着の適用は必ずそれより前に行う（P4/P5契約 (A)。順序を誤ると
        # プレビューは45°/吸着済みなのに確定した線だけずれる）。
        scene_pos = self._draw_end_point(event, scene_pos)
        start = self._draw_start
        p1_bind = self._draw_p1_bind
        p2_bind: tuple[int, str] | None = None
        if self._tool in ("line", "arrow") and not _shift_pressed(event):
            p2_snap = self._find_draw_anchor_snap(scene_pos)
            if p2_snap is not None:
                anchor_point, obj_id, anchor_name = p2_snap
                scene_pos = QPointF(anchor_point[0], anchor_point[1])
                p2_bind = (obj_id, anchor_name)
        else:
            # round2 finding #2: クリックかドラッグかを、吸着**前**の生の移動量で
            # まず判定する。`_snap_draw_corner` は近傍の整列線/グリッドへ角を
            # 吸着するため、この判定より先に吸着してしまうと「整列線の数px
            # 手前でのただのクリック」が吸着後の座標との距離で「ドラッグした」
            # と誤認され、幅/高さがほぼ0の不可視オブジェクトが生成されてしまう
            # （ab473f9 では `_snap_draw_corner` 自体が無かったため起きなかった
            # 回帰）。ドラッグ作成のプレビュー（`_draw_move`）は元々この生距離を
            # 経ずに毎回吸着し続けるので、ここで早期 return しても「見た目は
            # 動いたのに確定しない」という不一致は起きない。
            if self._tool in ("rect", "ellipse") and start is not None:
                raw = math.hypot(scene_pos.x() - start.x(), scene_pos.y() - start.y())
                if raw < _MOVE_EPS:
                    self._cancel_preview()
                    return True
            scene_pos = self._snap_draw_corner(scene_pos)
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
                p1_id=p1_bind[0] if p1_bind is not None else None,
                p1_anchor=p1_bind[1] if p1_bind is not None else "center",
                p2_id=p2_bind[0] if p2_bind is not None else None,
                p2_anchor=p2_bind[1] if p2_bind is not None else "center",
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
        self._draw_p1_bind = None
        self._draw_snap_targets = []
        self._clear_snap_guides()
        if self._draw_anchor_hint is not None:
            self._draw_anchor_hint.destroy()
            self._draw_anchor_hint = None

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
        """scene_pos 直下の「接続可能」なオブジェクトを返す(.obj 持ち, type!='connector')。

        選択（`_topmost_item_at`）は `shape()`（辺の帯）でヒットさせるが、コネクタの
        始点/終点は「塗りなし矩形の内部でも掴める」ことが要件（項目11。
        `tests/test_routing_avoid.py` が実際にそれをやっている）。ここだけ役割を
        分け、箱型図形（`live_geometry()` が x/y/width/height を返す
        rect/ellipse/image/text/math/freehand/curve）はモデルの論理 box
        （`anchor_snap.logical_box_for_item`）への点包含判定
        （`app.graphics.boxes.point_in_obb`）を主とする。**選択は辺のみ・
        接続は箱全体**。line/arrow は対象外のまま従来どおり `shape()` を使う
        （p1/p2 の軸並行 bbox は斜め線で広大になり水平線で高さ0になるため、
        bbox 判定に切り替えると逆に取りこぼす／誤って広く拾いすぎる）。

        box 判定には `shape()`（辺の帯）を OR で合流させる（レビュー所見）:
        box はモデルのちょうどの x/y/width/height なので、太い線（stroke_width>0）
        の場合ストロークは境界を挟んで内外均等に描かれ、その外側半分は box の
        外に出る。box だけだと「選択はできる（shape() が拾う）のに接続はできない
        （box が拾わない）」という、見た目には区別のつかない当たり判定の穴が
        できる。OR にすることで接続可能領域が選択可能領域の**上位集合**になる
        （box ⊇ 旧仕様、shape() ⊆ 選択領域、なので接続 = box ∪ shape ⊇ 選択）。
        """
        from app.graphics.boxes import point_in_obb

        views = self.scene.views()
        transform = views[0].transform() if views else QTransform()
        # 広い一次候補は sceneBoundingRect（Qt の BSP を使った高速な絞り込み）で
        # 取り、実際の採否は箱型なら point_in_obb（+ shape() の帯）、それ以外は
        # shape() のみで決める。
        items = self.scene.items(
            scene_pos,
            Qt.ItemSelectionMode.IntersectsItemBoundingRect,
            Qt.SortOrder.DescendingOrder,
            transform,
        )
        point = (scene_pos.x(), scene_pos.y())
        for item in items:
            obj = getattr(item, "obj", None)
            if obj is None or obj.type == "connector":
                continue
            live_geometry = getattr(item, "live_geometry", None)
            geom = live_geometry() if callable(live_geometry) else {}
            if "width" in geom and "height" in geom:
                box = logical_box_for_item(item)
                if box is None:
                    continue
                rotation = float(geom.get("rotation", 0.0))
                if point_in_obb(point, box, rotation) or item.shape().contains(
                    item.mapFromScene(scene_pos)
                ):
                    return obj
                continue
            if item.shape().contains(item.mapFromScene(scene_pos)):
                return obj
        return None

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
