"""ツール状態機械（select/rect/ellipse/line）: シーンへのマウス入力を解釈する(§8)。

モデルは直接変更せず、`app.commands.commands` のコマンドを `scene.undo_stack` に push する。
"""

from __future__ import annotations

import math
from collections.abc import Callable
from typing import TYPE_CHECKING, Any, NamedTuple

from PySide6.QtCore import QObject, QPointF, QRectF, Qt, Signal
from PySide6.QtGui import QColor, QFont, QPainterPath, QPen, QTransform, QUndoCommand
from PySide6.QtWidgets import (
    QGraphicsItem,
    QGraphicsLineItem,
    QGraphicsPathItem,
    QGraphicsRectItem,
)

from app.commands.commands import AddObjectCommand, SetGeometryCommand, SetPropertyCommand
from app.model.objects import (
    BaseObject,
    ConnectorObject,
    EllipseObject,
    FreehandObject,
    LineObject,
    MathObject,
    RectObject,
    TextObject,
    geometry_kind,
)

if TYPE_CHECKING:
    from app.scene.canvas_scene import CanvasScene

_TOOLS = ("select", "rect", "ellipse", "line", "arrow", "freehand", "text", "math", "connector")
_DRAW_TOOLS = ("rect", "ellipse", "line", "arrow")

# 移動判定/最小生成サイズのしきい値(px)。浮動小数の厳密等値比較を避けるための距離マージン。
_MOVE_EPS = 1.0
# freehand: 前点からこの距離(px)以上離れたら新しい点として採用する。
_FREEHAND_MIN_DIST = 2.0
# math: 新規生成時の既定 latex とレンダリング失敗時のフォールバック最小サイズ(px)。
_MATH_DEFAULT_LATEX = "E = mc^2"
_MATH_MIN_SIZE = 20.0

_MouseHandler = Callable[[Any, QPointF], bool]


class _ToolHandlers(NamedTuple):
    """1 ツール分の press/move/release ハンドラ束。`ToolManager._handlers` の値型。"""

    press: _MouseHandler
    move: _MouseHandler
    release: _MouseHandler


class ToolManager(QObject):
    """現在のツール名を保持し、マウスイベントを各ツールの振る舞いに振り分ける。"""

    tool_changed = Signal(str)

    def __init__(self, scene: CanvasScene) -> None:
        super().__init__()
        self.scene = scene
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
        }
        for _draw_tool in _DRAW_TOOLS:
            self._handlers[_draw_tool] = _ToolHandlers(
                self._draw_press, self._draw_move, self._draw_release
            )

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
        self._clear_snap_guides()
        if self._tool == name:
            return
        # crop の確定は「実際にツールが替わる」ときのみ行う（早期 return より
        # 後に置く）。同一ツールボタンの再クリックで crop 中の編集が不意に
        # 確定されないようにするため（レビュー所見 nit）。
        self._commit_active_crop()
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

    def handle_mouse_press(self, event: Any, scene_pos: QPointF) -> bool:
        handlers = self._handlers.get(self._tool)
        if handlers is None:
            return False
        return handlers.press(event, scene_pos)

    def handle_mouse_move(self, event: Any, scene_pos: QPointF) -> bool:
        handlers = self._handlers.get(self._tool)
        if handlers is None:
            return False
        return handlers.move(event, scene_pos)

    def handle_mouse_release(self, event: Any, scene_pos: QPointF) -> bool:
        handlers = self._handlers.get(self._tool)
        if handlers is None:
            return False
        return handlers.release(event, scene_pos)

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

        undo_stack = self.scene.undo_stack
        if undo_stack is None:
            # undo_stack 未設定時はモデルを直接変更しない(§13)。select と挙動を揃える。
            return True
        undo_stack.push(AddObjectCommand(document, obj))

        self.set_tool("select")
        new_item = self.scene.item_for(obj)
        if new_item is not None:
            self.scene.clearSelection()
            new_item.setSelected(True)
        return True

    def _cancel_preview(self) -> None:
        if self._preview_item is not None:
            item_scene = self._preview_item.scene()
            if item_scene is not None:
                item_scene.removeItem(self._preview_item)
            self._preview_item = None
        self._draw_start = None

    def _finish_creation(self, obj: Any) -> bool:
        """AddObjectCommand を push し select ツールへ戻して新規オブジェクトを選択する。

        undo_stack 未設定時はモデルを変更しない(§13 と同じ方針)。
        """
        undo_stack = self.scene.undo_stack
        if undo_stack is None:
            return True
        undo_stack.push(AddObjectCommand(self.scene.document, obj))
        self.set_tool("select")
        new_item = self.scene.item_for(obj)
        if new_item is not None:
            self.scene.clearSelection()
            new_item.setSelected(True)
        return True

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
        """既定 latex を実レンダリングし、`QSvgRenderer.defaultSize()` から px 寸法を得る。

        matplotlib/QSvgRenderer が例外を投げてもヘッドレスでクラッシュしない
        よう `MathRenderError` はフォールバック寸法で吸収する(§9.4 と同じ方針)。
        """
        import warnings

        from PySide6.QtCore import QByteArray
        from PySide6.QtSvg import QSvgRenderer

        from app.math.mathtext_render import MathRenderError, render_latex_to_svg

        defaults = MathObject(id=0)
        try:
            svg = render_latex_to_svg(_MATH_DEFAULT_LATEX, defaults.font_size, defaults.color)
            renderer = QSvgRenderer(QByteArray(svg.encode("utf-8")))
            if renderer.isValid():
                size = renderer.defaultSize()
                width = max(float(size.width()), _MATH_MIN_SIZE)
                height = max(float(size.height()), _MATH_MIN_SIZE)
                return (width, height)
        except MathRenderError as exc:
            warnings.warn(f"math tool: default latex render failed: {exc}", stacklevel=2)
        return (_MATH_MIN_SIZE, _MATH_MIN_SIZE)

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
            routing="straight",
            arrow_end="triangle",
        )
        return self._finish_creation(obj)
