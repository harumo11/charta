"""AgentAPI: エージェント制御の公開ファサード（GUI スレッド上でのみ呼ぶ）。

すべてのメソッドが JSON にできる `dict` を返し、失敗は `AgentError` を送出する。
`host.py`（ソケット）と `exec_env.py`（charta_exec）はこの上に乗るだけなので、
テストはここに集中させられる。

不変条件:

* 座標は常にアートボード px。画像 px を扱うのは `render` の戻り値だけで、
  そこには `region` / `scale_x` / `scale_y` / 変換式が必ず同梱される。
* **1 呼び出し = 1 undo マクロ**（ラベルは `AI: …`）。コマンドが 1 個なら
  マクロを開かない（空マクロは履歴に幽霊エントリを残す）。
* **検証が全件通るまで 1 つも適用しない。** 検証は純粋関数なので中断コストが
  ゼロで、エージェントのリトライが冪等になる。
* 既存経路（`EditController` / `commands` / `serialize` / `export`）を呼ぶだけで、
  ロジックを再実装しない。特にコネクタ端点の固定化は絶対に書き直さない。
"""

from __future__ import annotations

import base64
from collections.abc import Iterator
from contextlib import contextmanager
from typing import TYPE_CHECKING, Any

from PySide6.QtCore import QBuffer, QByteArray, QRectF, QTimer
from PySide6.QtGui import QFont, QUndoCommand

from app.agent import paths, render, schema
from app.agent.validate import AgentError, FieldError, batch_error, check_locked, check_type_name
from app.agent.validate import validate_values as _validate_values
from app.commands.commands import (
    AddObjectCommand,
    ReorderCommand,
    SetArtboardCommand,
    SetGeometryCommand,
    SetPropertyCommand,
)
from app.export.pdf_exporter import export_pdf
from app.export.png_exporter import export_png
from app.export.svg_exporter import document_to_svg, export_svg
from app.graphics.routing import (
    anchor_set_for_object,
    compute_endpoints,
    resolved_bounding_box,
)
from app.model.document import Artboard, Document, Physical
from app.model.geometry import bounding_box, translate_geom
from app.model.objects import OBJECT_REGISTRY, BaseObject, new_object
from app.model.serialize import PROJECT_JSON_NAME, load_document, save_document

if TYPE_CHECKING:
    from app.ui.main_window import MainWindow

#: 1 回のバッチで扱える上限（レイテンシではなく暴走の歯止め）。
CREATE_BATCH_MAX = 200
UPDATE_BATCH_MAX = 500

#: `get_svg` の返却バイト数上限（画像は Base64 で丸ごと入るため）。
SVG_MAX_BYTES = 100_000

#: undo ラベルの接頭辞。人間が履歴でエージェントの仕業を識別できるようにする。
UNDO_PREFIX = "AI: "

_ALIGN_MODES = ("left", "right", "top", "bottom", "center_h", "center_v")
_ARRANGE_ACTIONS = (*_ALIGN_MODES, "distribute_h", "distribute_v")
_ORDER_ACTIONS = ("front", "back", "forward", "backward", "group", "ungroup")
_EXPORT_KINDS = ("png", "pdf", "svg")
_PROJECT_ACTIONS = ("new", "open", "save", "save_as")

#: `place_image` / `export_file` などがファイルを触るときの拡張子。
_IMAGE_SUFFIXES = (".png", ".jpg", ".jpeg", ".bmp", ".webp")

#: `create_objects` では作れず専用ツールが要る型 -> ツール名。
_TOOL_CREATED_TYPES: dict[str, str] = {"image": "place_image", "connector": "connect_objects"}


class _LazyMacro:
    """最初の push があったときだけ `beginMacro` する undo マクロ。"""

    def __init__(self, undo_stack: Any, label: str) -> None:
        self._undo = undo_stack
        self._label = label
        self._open = False
        self.count = 0

    def open(self) -> None:
        """マクロを先に開く。

        自分で push しないコード（`ImageImportController.import_image_file` は
        undo スタックへ直接 push する）を取り込みたいときだけ使う。呼んだ以上、
        1 つも push されなければ空エントリが残るので、必ず「押されることが
        確定してから」呼ぶこと。
        """
        if not self._open:
            self._undo.beginMacro(self._label)
            self._open = True

    def push(self, command: QUndoCommand) -> None:
        self.open()
        self._undo.push(command)
        self.count += 1

    def close(self) -> None:
        if self._open:
            self._undo.endMacro()
            self._open = False


class AgentAPI:
    """`MainWindow` を包むエージェント向けファサード。"""

    def __init__(self, window: MainWindow) -> None:
        self._window = window
        self._highlight_timer: QTimer | None = None
        self._highlight_items: list[Any] = []
        self._jobs: Any = None

    # ------------------------------------------------------------------
    # 内部ヘルパ
    # ------------------------------------------------------------------

    @property
    def _scene(self) -> Any:
        return self._window.scene

    @property
    def _document(self) -> Document:
        return self._window.scene.document

    @property
    def _undo(self) -> Any:
        return self._window.undo_stack

    @property
    def _edit(self) -> Any:
        return self._window._edit

    def _ok(self, **payload: Any) -> dict[str, Any]:
        doc = self._document
        return {"ok": True, "doc_uid": doc.uid, "revision": doc.revision, **payload}

    def _require_revision(self, expect_revision: int | None) -> None:
        if expect_revision is None:
            return
        current = self._document.revision
        if current != expect_revision:
            raise AgentError(
                "revision_conflict",
                f"ドキュメントが変更されています（期待 {expect_revision} / 現在 {current}）。"
                "get_scene で読み直してください",
                expected=expect_revision,
                current=current,
            )

    def _resolve(self, oid: Any, *, index: int | None = None) -> BaseObject:
        if not isinstance(oid, int) or isinstance(oid, bool):
            raise AgentError("type_mismatch", f"id は整数である必要があります（{oid!r}）")
        obj = self._document.object_by_id(oid)
        if obj is None:
            raise AgentError(
                "unknown_id",
                f"id={oid} のオブジェクトはありません",
                id=oid,
                index=index,
                available=[o.id for o in self._document.objects],
            )
        return obj

    def _resolve_many(self, ids: Any) -> list[BaseObject]:
        if not isinstance(ids, list | tuple):
            raise AgentError("type_mismatch", "ids は整数の配列である必要があります")
        return [self._resolve(oid) for oid in ids]

    @contextmanager
    def _macro(self, label: str) -> Iterator[_LazyMacro]:
        """1 呼び出しを 1 undo エントリにまとめる。

        **遅延オープン**: 最初の push が来るまで `beginMacro` を呼ばない。
        1 つも push されなければマクロを開かないので、履歴に空の幽霊エントリが
        残らない（既存コードの `_reorder_selected` と同じ作法）。
        push は即座に実行される（`QUndoStack.push` は `redo()` を即時実行する）ので、
        push 後に `document.index_of()` などで最新状態を読める。
        """
        macro = _LazyMacro(self._undo, UNDO_PREFIX + label)
        try:
            yield macro
        finally:
            macro.close()

    def _label(self, custom: str | None, default: str) -> str:
        return custom if custom else default

    def _allowed_roots(self) -> list[Any]:
        return paths.default_allowed_roots(self._window._project_dir)

    def _check_path(self, path: str, *, must_exist: bool = False) -> str:
        import os

        roots = self._allowed_roots()
        if not paths.is_within(path, roots):
            raise AgentError(
                "path_denied",
                f"{path} は許可されたディレクトリの外です。"
                f"環境変数 {paths.ALLOWED_PATHS_ENV} で追加できます",
                allowed_roots=[str(r) for r in roots],
            )
        resolved = str(os.path.abspath(os.path.expanduser(path)))
        if must_exist and not os.path.exists(resolved):
            raise AgentError("file_not_found", f"ファイルがありません: {resolved}")
        return resolved

    # ------------------------------------------------------------------
    # 観測
    # ------------------------------------------------------------------

    def describe_state(self) -> dict[str, Any]:
        """アプリの現在状態。busy の理由・選択・undo 履歴・機能可用性を開示する。

        `busy` が返ってきた理由をエージェントが推測しなくて済むようにするのが目的。
        """
        from app.agent.host import busy_state  # 循環 import 回避のため関数内

        window = self._window
        doc = self._document
        try:
            from app.ai import sam3

            sam3_available = sam3.is_available()
        except Exception:  # noqa: BLE001 - 可用性判定は失敗しても致命的でない
            sam3_available = False

        crop_item = self._scene.active_crop_item()
        crop_id = getattr(getattr(crop_item, "obj", None), "id", None)
        undo = self._undo
        return self._ok(
            project_dir=window._project_dir,
            saved=window._project_dir is not None,
            artboard=schema.artboard_info(doc),
            object_count=len(doc.objects),
            tool=window.tool_manager.current_tool(),
            selection=[o.id for o in self._scene.selected_objects()],
            busy=busy_state(window),
            crop_mode={"active": crop_item is not None, "object_id": crop_id},
            mask_mode={"active": self._scene.active_mask_session() is not None},
            undo={
                "index": undo.index(),
                "count": undo.count(),
                "can_undo": undo.canUndo(),
                "can_redo": undo.canRedo(),
                "undo_text": undo.undoText(),
                "redo_text": undo.redoText(),
            },
            capabilities={
                "sam3_available": sam3_available,
                "exec_enabled": getattr(window, "_agent_exec_enabled", False),
            },
            limits={
                "create_batch_max": CREATE_BATCH_MAX,
                "update_batch_max": UPDATE_BATCH_MAX,
                "render_max_edge": render.MAX_MAX_EDGE,
                "svg_max_bytes": SVG_MAX_BYTES,
            },
        )

    def describe_schema(self, type: str | None = None) -> dict[str, Any]:  # noqa: A002
        """全オブジェクト型の編集可能キー・範囲・enum・幾何契約。"""
        try:
            result = schema.describe_schema(self._document, type)
        except KeyError:
            error = check_type_name(type)
            assert error is not None
            raise AgentError(error.code, error.message, **error.extra) from None
        return {"ok": True, **result}

    def _object_summary(self, obj: BaseObject, detail: str) -> dict[str, Any]:
        document = self._document
        # コネクタは source_point/target_point が遅れうるのでアンカーから解き直す。
        box = resolved_bounding_box(document, obj)
        entry: dict[str, Any] = {
            "id": obj.id,
            "type": obj.type,
            "name": obj.name,
            "z_index": document.index_of(obj),
            "bbox": list(box),
            "visible": obj.visible,
            "locked": obj.locked,
            "opacity": obj.opacity,
        }
        if obj.group_id is not None:
            entry["group_id"] = obj.group_id
        if detail == "full":
            full = obj.to_dict()
            # 巨大配列はそのまま返さない（freehand は数百点になりうる）。
            points = full.get("points")
            if isinstance(points, list) and len(points) > 32:
                full["points"] = f"<{len(points)} 点を省略>"
            entry["properties"] = full
        else:
            for key in ("text", "latex", "src", "fill", "stroke", "stroke_width", "font_size"):
                if hasattr(obj, key):
                    entry[key] = getattr(obj, key)
            if obj.GEOMETRY == "endpoints":
                entry["p1"] = list(obj.p1)
                entry["p2"] = list(obj.p2)
            elif obj.GEOMETRY == "connector":
                entry["source_id"] = obj.source_id
                entry["target_id"] = obj.target_id
                entry["source_anchor"] = obj.source_anchor
                entry["target_anchor"] = obj.target_anchor
                entry["routing"] = obj.routing
        return entry

    def get_scene(
        self,
        ids: list[int] | None = None,
        types: list[str] | None = None,
        intersecting: list[float] | None = None,
        detail: str = "summary",
        format: str = "json",  # noqa: A002
    ) -> dict[str, Any]:
        """存在するオブジェクトの一覧（id / 型 / 名前 / bbox / z順 / 選択状態）。"""
        if detail not in ("summary", "full"):
            raise AgentError(
                "invalid_enum", "detail は 'summary' か 'full' です", allowed=["summary", "full"]
            )
        if format not in ("json", "outline"):
            raise AgentError(
                "invalid_enum", "format は 'json' か 'outline' です", allowed=["json", "outline"]
            )

        document = self._document
        objects = list(document.objects)
        if ids is not None:
            wanted = set(ids)
            objects = [o for o in objects if o.id in wanted]
        if types is not None:
            wanted_types = set(types)
            objects = [o for o in objects if o.type in wanted_types]
        if intersecting is not None:
            if len(intersecting) != 4:
                raise AgentError("type_mismatch", "intersecting は [x, y, w, h] です")
            rx, ry, rw, rh = (float(v) for v in intersecting)
            region = QRectF(rx, ry, rw, rh)
            objects = [
                o for o in objects if region.intersects(QRectF(*resolved_bounding_box(document, o)))
            ]

        entries = [self._object_summary(o, detail) for o in objects]
        payload: dict[str, Any] = {
            "artboard": schema.artboard_info(document),
            "selection": [o.id for o in self._scene.selected_objects()],
            "objects": entries,
            "warnings": render.offscreen_warnings(document),
        }
        if format == "outline":
            payload["outline"] = _format_outline(document, entries)
        return self._ok(**payload)

    def get_svg(self, outline_text: bool = False, max_bytes: int = SVG_MAX_BYTES) -> dict[str, Any]:
        """SVG テキストとしての「読める」ビュー。画像を含む図では非常に大きくなる。"""
        svg = document_to_svg(self._document, outline_text=outline_text)
        data = svg.encode("utf-8")
        if len(data) > max_bytes:
            raise AgentError(
                "too_large",
                f"SVG が {len(data)} バイトあり上限 {max_bytes} を超えます。"
                "render_canvas（PNG）を使うか max_bytes を上げてください",
                bytes=len(data),
                max_bytes=max_bytes,
            )
        return self._ok(svg=svg, bytes=len(data))

    def render(
        self,
        source: str = "artboard",
        region: list[float] | None = None,
        object_ids: list[int] | None = None,
        padding: float = 24.0,
        max_edge: int = render.DEFAULT_MAX_EDGE,
        overlay: str = "none",
        transparent: bool = False,
        inline: bool = False,
    ) -> dict[str, Any]:
        """キャンバスを PNG にして **ファイルパスを返す**。

        インライン base64 を既定にしないのは、MCP クライアント側で画像が
        テキストとして数万トークン消費し、出力上限にも掛かるため。
        エージェントは返ってきたパスを組込みの読み取りツールで開くこと。
        """
        if source not in ("artboard", "window"):
            raise AgentError(
                "invalid_enum",
                "source は 'artboard'（書き出しと同じクリーンな図）か"
                " 'window'（人間が見ている画面）です",
                allowed=["artboard", "window"],
            )
        document = self._document

        if source == "window":
            image, view = render.render_window(self._window.view, max_edge=max_edge)
        else:
            target_region: tuple[float, float, float, float] | None = None
            if object_ids:
                target_region = render.union_region(document, object_ids, padding=padding)
                if target_region is None:
                    raise AgentError(
                        "unknown_id", f"object_ids {object_ids} に該当するオブジェクトがありません"
                    )
            elif region is not None:
                if len(region) != 4:
                    raise AgentError("type_mismatch", "region は [x, y, w, h] です")
                target_region = (
                    float(region[0]),
                    float(region[1]),
                    float(region[2]),
                    float(region[3]),
                )
            image, view = render.render_document(
                document, region=target_region, max_edge=max_edge, transparent=transparent
            )

        if overlay != "none":
            selected = {o.id for o in self._scene.selected_objects()}
            render.draw_overlay(image, document, view, mode=overlay, selected_ids=selected)

        payload: dict[str, Any] = {
            "view": {
                **view.to_dict(),
                "overlay": overlay,
                "artboard": schema.artboard_info(document),
            },
            "objects": render.object_boxes(document, view),
            "warnings": render.offscreen_warnings(document),
        }
        if inline:
            payload["image_base64"] = _png_base64(image)
            payload["mime_type"] = "image/png"
        else:
            payload["path"] = render.save_render(image, document, tag=f"{source}:{overlay}")
            payload["read_hint"] = "この path を読み取りツールで開いてください（base64 より安い）"
        return self._ok(**payload)

    # ------------------------------------------------------------------
    # 生成
    # ------------------------------------------------------------------

    def _auto_size(self, type_name: str, values: dict[str, Any]) -> dict[str, Any]:
        """text / math で width / height が省かれたら内容に合わせて採寸する。"""
        if values.get("width") and values.get("height"):
            return values
        if type_name == "text":
            from app.scene.items.text_item import default_text_size

            font = QFont(values.get("font_family", "Noto Sans CJK JP"))
            font.setPointSizeF(max(float(values.get("font_size", 18.0)), 1.0))
            font.setBold(bool(values.get("bold", False)))
            font.setItalic(bool(values.get("italic", False)))
            width, height = default_text_size(values.get("text", ""), font)
        elif type_name == "math":
            from app.scene.items.math_item import natural_math_size

            width, height = natural_math_size(
                values.get("latex", ""),
                float(values.get("font_size", 18.0)),
                values.get("color", "#000000"),
            )
        else:
            return values
        return {
            **values,
            "width": values.get("width") or width,
            "height": values.get("height") or height,
        }

    def create_objects(
        self,
        objects: list[dict[str, Any]],
        insert_at: str = "front",
        select: bool = False,
        undo_label: str | None = None,
        expect_revision: int | None = None,
    ) -> dict[str, Any]:
        """複数のオブジェクトを 1 undo ステップで作成する。"""
        self._require_revision(expect_revision)
        if not isinstance(objects, list) or not objects:
            raise AgentError("type_mismatch", "objects は 1 件以上の配列である必要があります")
        if len(objects) > CREATE_BATCH_MAX:
            raise AgentError(
                "too_large",
                f"1 回に作成できるのは {CREATE_BATCH_MAX} 件までです（{len(objects)} 件）",
            )
        if insert_at not in ("front", "back"):
            raise AgentError(
                "invalid_enum", "insert_at は 'front' か 'back' です", allowed=["front", "back"]
            )

        document = self._document
        errors: list[FieldError] = []
        planned: list[tuple[str, dict[str, Any]]] = []

        for index, spec in enumerate(objects):
            if not isinstance(spec, dict):
                errors.append(FieldError("type_mismatch", "各要素はオブジェクトです", index=index))
                continue
            values = dict(spec)
            type_name = values.pop("type", None)
            type_error = check_type_name(type_name)
            if type_error is not None:
                type_error.index = index
                errors.append(type_error)
                continue
            if type_name in _TOOL_CREATED_TYPES:
                tool = _TOOL_CREATED_TYPES[type_name]
                errors.append(
                    FieldError(
                        "not_editable",
                        f"{type_name} は create_objects では作れません。{tool} を使ってください",
                        index=index,
                        extra={"tool": tool},
                    )
                )
                continue
            coerced, value_errors = _validate_values(type_name, values, index=index)
            if value_errors:
                errors.extend(value_errors)
                continue
            math_error = _check_math_renderable(type_name, coerced, index=index)
            if math_error is not None:
                errors.append(math_error)
                continue
            # 採寸は検証後に行う（不正な値で採寸してもエラーが分かりにくくなるだけ）。
            planned.append((type_name, self._auto_size(type_name, coerced)))

        if errors:
            raise batch_error(errors)

        created: list[BaseObject] = []
        with self._macro(self._label(undo_label, f"{len(planned)} 個作成")) as macro:
            for type_name, values in planned:
                obj = new_object(type_name, document.new_id(), **values)
                macro.push(
                    AddObjectCommand(document, obj, text=UNDO_PREFIX + f"{type_name} を作成")
                )
                created.append(obj)
            if insert_at == "back":
                # push 済みなので index_of は最新。同じマクロ内で背面へ送る。
                for obj in reversed(created):
                    old_index = document.index_of(obj)
                    if old_index != 0:
                        macro.push(ReorderCommand(document, obj, 0, old_index))

        if select:
            self.set_selection([o.id for o in created])

        return self._ok(
            created=[
                {
                    "id": o.id,
                    "type": o.type,
                    "z_index": document.index_of(o),
                    "bbox": list(resolved_bounding_box(document, o)),
                }
                for o in created
            ]
        )

    def place_image(
        self,
        path: str,
        x: float | None = None,
        y: float | None = None,
        width: float | None = None,
        select: bool = False,
        undo_label: str | None = None,
    ) -> dict[str, Any]:
        """画像ファイルを `assets/` に複製して配置する。"""
        resolved = self._check_path(path, must_exist=True)
        if not resolved.lower().endswith(_IMAGE_SUFFIXES):
            raise AgentError(
                "type_mismatch",
                f"対応していない画像形式です（{_IMAGE_SUFFIXES} のいずれか）",
                allowed=list(_IMAGE_SUFFIXES),
            )
        # 空マクロ（無操作の undo エントリ）を残さないため、マクロを開く前に
        # 読めることを確かめる。`import_image_file` も同じ検証を最初に行う。
        try:
            from PIL import Image

            with Image.open(resolved) as probe:
                probe.verify()
        except Exception as exc:  # noqa: BLE001 - 読めない理由をそのまま返す
            raise AgentError(
                "file_not_found", f"画像を読み込めません: {resolved}（{exc}）"
            ) from exc

        importer = self._window._image_import
        importer._ensure_base_dir_for_import()

        center = None
        if x is not None and y is not None:
            center = (float(x), float(y))
        errors: list[str] = []
        # 幾何調整があるときだけマクロで包む（取り込み自体は 1 コマンドなので
        # 調整不要なら包む必要が無く、空マクロのリスクもゼロになる）。
        # `import_image_file` は undo スタックへ直接 push するので、マクロは
        # 呼び出しより前に開いておく必要がある。
        needs_adjust = width is not None or center is not None
        with self._macro(self._label(undo_label, "画像を配置")) as macro:
            if needs_adjust:
                macro.open()
            obj = importer.import_image_file(resolved, center=center, errors=errors, select=False)
            if obj is None:
                raise AgentError("file_not_found", "画像の取り込みに失敗しました", details=errors)
            updates: dict[str, Any] = {}
            if width is not None and obj.width > 0:
                scale = float(width) / obj.width
                updates = {"width": float(width), "height": obj.height * scale}
            if center is not None:
                final_w = updates.get("width", obj.width)
                final_h = updates.get("height", obj.height)
                updates["x"] = center[0] - final_w / 2.0
                updates["y"] = center[1] - final_h / 2.0
            if updates:
                old = {k: getattr(obj, k) for k in updates}
                macro.push(
                    SetGeometryCommand(
                        self._document, obj, updates, old, text=UNDO_PREFIX + "画像サイズを調整"
                    )
                )
        if select:
            self.set_selection([obj.id])
        return self._ok(
            created=[
                {
                    "id": obj.id,
                    "type": "image",
                    "src": obj.src,
                    "bbox": list(resolved_bounding_box(self._document, obj)),
                }
            ]
        )

    def connect_objects(
        self,
        connections: list[dict[str, Any]],
        undo_label: str | None = None,
        expect_revision: int | None = None,
    ) -> dict[str, Any]:
        """図形どうしを追従するコネクタで結ぶ。"""
        self._require_revision(expect_revision)
        if not isinstance(connections, list) or not connections:
            raise AgentError("type_mismatch", "connections は 1 件以上の配列です")

        document = self._document
        errors: list[FieldError] = []
        planned: list[dict[str, Any]] = []

        for index, spec in enumerate(connections):
            if not isinstance(spec, dict):
                errors.append(FieldError("type_mismatch", "各要素はオブジェクトです", index=index))
                continue
            values = dict(spec)
            source_id = values.get("source_id")
            target_id = values.get("target_id")
            if source_id == target_id:
                errors.append(
                    FieldError(
                        "self_reference",
                        "source_id と target_id は別である必要があります",
                        index=index,
                    )
                )
                continue
            bad = False
            for key in ("source_id", "target_id"):
                oid = values.get(key)
                if oid is None:
                    continue
                obj = document.object_by_id(oid) if isinstance(oid, int) else None
                if obj is None:
                    errors.append(
                        FieldError("unknown_id", f"{key}={oid} が見つかりません", index=index)
                    )
                    bad = True
                elif obj.type == "connector":
                    errors.append(
                        FieldError("type_mismatch", "コネクタ同士は接続できません", index=index)
                    )
                    bad = True
            if bad:
                continue
            values.setdefault("source_anchor", "nearest")
            values.setdefault("target_anchor", "nearest")
            values.setdefault("arrow_end", "triangle")
            coerced, value_errors = _validate_values("connector", values, index=index)
            if value_errors:
                errors.extend(value_errors)
                continue
            planned.append(coerced)

        if errors:
            raise batch_error(errors)

        created: list[BaseObject] = []
        with self._macro(self._label(undo_label, f"コネクタ {len(planned)} 本")) as macro:
            for values in planned:
                obj = new_object("connector", document.new_id(), **values)
                # 端点座標を先に解いておく（rebind 前でも幾何が正しくなるように）。
                src_set = anchor_set_for_object(
                    document.object_by_id(obj.source_id) if obj.source_id is not None else None
                )
                tgt_set = anchor_set_for_object(
                    document.object_by_id(obj.target_id) if obj.target_id is not None else None
                )
                p1, p2 = compute_endpoints(
                    src_set,
                    (obj.source_point[0], obj.source_point[1]),
                    obj.source_anchor,
                    tgt_set,
                    (obj.target_point[0], obj.target_point[1]),
                    obj.target_anchor,
                )
                obj.source_point = [p1[0], p1[1]]
                obj.target_point = [p2[0], p2[1]]
                macro.push(AddObjectCommand(document, obj, text=UNDO_PREFIX + "コネクタを作成"))
                created.append(obj)
        self._scene.rebind_connectors()
        return self._ok(
            created=[
                {
                    "id": o.id,
                    "type": "connector",
                    "source_id": o.source_id,
                    "target_id": o.target_id,
                    "bbox": list(resolved_bounding_box(document, o)),
                }
                for o in created
            ]
        )

    # ------------------------------------------------------------------
    # 編集
    # ------------------------------------------------------------------

    def update_objects(
        self,
        updates: list[dict[str, Any]],
        force: bool = False,
        undo_label: str | None = None,
        expect_revision: int | None = None,
    ) -> dict[str, Any]:
        """複数オブジェクトのプロパティを 1 undo ステップで変更する。"""
        self._require_revision(expect_revision)
        if not isinstance(updates, list) or not updates:
            raise AgentError("type_mismatch", "updates は 1 件以上の配列です")
        if len(updates) > UPDATE_BATCH_MAX:
            raise AgentError("too_large", f"1 回の更新は {UPDATE_BATCH_MAX} 件までです")

        errors: list[FieldError] = []
        planned: list[tuple[BaseObject, dict[str, Any]]] = []

        for index, spec in enumerate(updates):
            if not isinstance(spec, dict) or "set" not in spec:
                errors.append(FieldError("type_mismatch", "各要素は {id, set} です", index=index))
                continue
            target_ids = spec.get("ids") or ([spec["id"]] if "id" in spec else [])
            if not target_ids:
                errors.append(FieldError("type_mismatch", "id か ids が必要です", index=index))
                continue
            values = spec["set"]
            if not isinstance(values, dict) or not values:
                errors.append(FieldError("type_mismatch", "set は空でない辞書です", index=index))
                continue
            for oid in target_ids:
                obj = self._document.object_by_id(oid) if isinstance(oid, int) else None
                if obj is None:
                    errors.append(
                        FieldError(
                            "unknown_id",
                            f"id={oid} のオブジェクトはありません",
                            index=index,
                            id=oid,
                        )
                    )
                    continue
                locked_error = check_locked(obj, force, index=index)
                if locked_error is not None:
                    errors.append(locked_error)
                    continue
                coerced, value_errors = _validate_values(
                    obj.type, values, obj_id=obj.id, index=index
                )
                if value_errors:
                    errors.extend(value_errors)
                    continue
                # 数式は「適用してから描画に失敗」だとサイレント no-op になるので先に検証する。
                merged = {
                    "latex": obj.latex if obj.type == "math" else "",
                    "font_size": getattr(obj, "font_size", 18.0),
                    "color": getattr(obj, "color", "#000000"),
                    **coerced,
                }
                math_error = _check_math_renderable(obj.type, merged, index=index, obj_id=obj.id)
                if math_error is not None:
                    errors.append(math_error)
                    continue
                planned.append((obj, coerced))

        if errors:
            raise batch_error(errors)

        document = self._document
        geometry_keys = set(schema.GEOMETRY_TRUTH_KEYS["box"]) | {"p1", "p2"}
        applied: list[dict[str, Any]] = []
        with self._macro(self._label(undo_label, f"プロパティ変更 ({len(planned)} 件)")) as macro:
            for obj, values in planned:
                geom = {k: v for k, v in values.items() if k in geometry_keys}
                scalars = {k: v for k, v in values.items() if k not in geometry_keys}
                if geom:
                    old = {k: _copy_value(getattr(obj, k)) for k in geom}
                    macro.push(
                        SetGeometryCommand(document, obj, geom, old, text=UNDO_PREFIX + "幾何変更")
                    )
                for key, value in scalars.items():
                    macro.push(
                        SetPropertyCommand(
                            document, obj, key, value, _copy_value(getattr(obj, key))
                        )
                    )
                applied.append({"id": obj.id, "changed": sorted(values)})
        return self._ok(updated=applied)

    def move_objects(
        self,
        moves: list[dict[str, Any]],
        undo_label: str | None = None,
        expect_revision: int | None = None,
    ) -> dict[str, Any]:
        """オブジェクトを移動する。**幾何種別を問わず動く**唯一の移動手段。

        box 型は x/y、line/arrow は p1 と p2 を一緒に、コネクタは固定端点を動かす。
        """
        self._require_revision(expect_revision)
        if not isinstance(moves, list) or not moves:
            raise AgentError("type_mismatch", "moves は 1 件以上の配列です")

        document = self._document
        planned: list[tuple[BaseObject, dict[str, Any], dict[str, Any]]] = []
        for index, spec in enumerate(moves):
            if not isinstance(spec, dict) or "id" not in spec:
                raise AgentError(
                    "type_mismatch", "各要素は {id, dx, dy} か {id, to} です", index=index
                )
            obj = self._resolve(spec["id"], index=index)
            # ここは意図的に生の bbox を使う（`translate_geom` が動かすのと同じ
            # フィールドを基準にしないと `to` の計算がずれるため）。
            box = bounding_box(obj)
            if "to" in spec and spec["to"] is not None:
                to = spec["to"]
                if not isinstance(to, list | tuple) or len(to) != 2:
                    raise AgentError("type_mismatch", "to は [x, y] です", index=index)
                anchor = spec.get("anchor", "top_left")
                if anchor not in ("top_left", "center"):
                    raise AgentError(
                        "invalid_enum",
                        "anchor は 'top_left' か 'center' です",
                        allowed=["top_left", "center"],
                    )
                if anchor == "center":
                    dx = float(to[0]) - (box[0] + box[2] / 2.0)
                    dy = float(to[1]) - (box[1] + box[3] / 2.0)
                else:
                    dx = float(to[0]) - box[0]
                    dy = float(to[1]) - box[1]
            else:
                dx = float(spec.get("dx", 0.0))
                dy = float(spec.get("dy", 0.0))
            if abs(dx) < 1e-9 and abs(dy) < 1e-9:
                continue
            old_geom, new_geom = translate_geom(obj, dx, dy)
            planned.append((obj, new_geom, old_geom))

        with self._macro(self._label(undo_label, f"{len(planned)} 個を移動")) as macro:
            for obj, new_geom, old_geom in planned:
                macro.push(
                    SetGeometryCommand(document, obj, new_geom, old_geom, text=UNDO_PREFIX + "移動")
                )
        return self._ok(
            moved=[
                {"id": o.id, "bbox": list(resolved_bounding_box(document, o))}
                for o, _, _ in planned
            ]
        )

    def delete_objects(self, ids: list[int], undo_label: str | None = None) -> dict[str, Any]:
        """オブジェクトを削除する（接続していたコネクタの端点は自動で固定化される）。"""
        objs = self._resolve_many(ids)
        deleted = self._edit.delete_objects(
            objs, text=UNDO_PREFIX + self._label(undo_label, "削除")
        )
        return self._ok(deleted=deleted)

    def duplicate_objects(
        self, ids: list[int], select: bool = False, undo_label: str | None = None
    ) -> dict[str, Any]:
        objs = self._resolve_many(ids)
        created = self._edit.duplicate_objects(
            objs, text=UNDO_PREFIX + self._label(undo_label, "複製"), select=select
        )
        return self._ok(
            created=[
                {
                    "id": o.id,
                    "type": o.type,
                    "bbox": list(resolved_bounding_box(self._document, o)),
                }
                for o in created
            ]
        )

    # ------------------------------------------------------------------
    # 配置
    # ------------------------------------------------------------------

    def arrange_objects(self, ids: list[int], action: str, force: bool = False) -> dict[str, Any]:
        """整列（左/右/上/下/水平中央/垂直中央）または等間隔分布。"""
        if action not in _ARRANGE_ACTIONS:
            raise AgentError(
                "invalid_enum",
                f"action は {list(_ARRANGE_ACTIONS)} のいずれかです",
                allowed=list(_ARRANGE_ACTIONS),
            )
        objs = self._resolve_many(ids)
        if action.startswith("distribute_"):
            axis = action.split("_", 1)[1]
            moved = self._edit.distribute_objects(
                objs, axis, text=UNDO_PREFIX + f"分布 ({axis})", force=force
            )
            required = 3
        else:
            moved = self._edit.align_objects(
                objs, action, text=UNDO_PREFIX + f"整列 ({action})", force=force
            )
            required = 2
        return self._ok(
            moved=[
                {"id": o.id, "bbox": list(resolved_bounding_box(self._document, o))} for o in moved
            ],
            note=(
                None
                if moved
                else f"対象が {required} 個未満か、既に整列済みのため何も動きませんでした"
            ),
        )

    def order_objects(self, ids: list[int], action: str, force: bool = False) -> dict[str, Any]:
        """z順の変更（front/back/forward/backward）とグループ化/解除。"""
        if action not in _ORDER_ACTIONS:
            raise AgentError(
                "invalid_enum",
                f"action は {list(_ORDER_ACTIONS)} のいずれかです",
                allowed=list(_ORDER_ACTIONS),
            )
        objs = self._resolve_many(ids)
        if action == "group":
            group_id = self._edit.group_objects(objs, force=force)
            return self._ok(group_id=group_id, note=None if group_id else "対象が 2 個未満です")
        if action == "ungroup":
            released = self._edit.ungroup_objects(objs)
            return self._ok(ungrouped=[o.id for o in released])
        moved = self._edit.reorder_objects(
            objs, action, text=UNDO_PREFIX + f"z順 ({action})", force=force
        )
        document = self._document
        return self._ok(reordered=[{"id": o.id, "z_index": document.index_of(o)} for o in moved])

    # ------------------------------------------------------------------
    # アートボード・入出力・履歴
    # ------------------------------------------------------------------

    def set_artboard(
        self,
        width_px: int | None = None,
        height_px: int | None = None,
        width_mm: float | None = None,
        target_dpi: int | None = None,
        background: str | None = None,
    ) -> dict[str, Any]:
        current = self._document.artboard
        if background is not None:
            from app.agent.validate import coerce

            _value, error = coerce("background", background, {"kind": "color"})
            if error is not None:
                raise AgentError(error.code, error.message, **error.extra)
        new = Artboard(
            width_px=int(width_px) if width_px is not None else current.width_px,
            height_px=int(height_px) if height_px is not None else current.height_px,
            physical=Physical(
                width_mm=float(width_mm) if width_mm is not None else current.physical.width_mm,
                target_dpi=(
                    int(target_dpi) if target_dpi is not None else current.physical.target_dpi
                ),
            ),
            background=background if background is not None else current.background,
        )
        if new.width_px <= 0 or new.height_px <= 0:
            raise AgentError("out_of_range", "アートボードの幅・高さは正の値である必要があります")
        self._undo.push(
            SetArtboardCommand(self._document, new, current, text=UNDO_PREFIX + "アートボード設定")
        )
        return self._ok(artboard=schema.artboard_info(self._document))

    def export_file(
        self,
        kind: str,
        path: str,
        transparent: bool = False,
        outline_text: bool = True,
    ) -> dict[str, Any]:
        if kind not in _EXPORT_KINDS:
            raise AgentError(
                "invalid_enum", f"kind は {list(_EXPORT_KINDS)} です", allowed=list(_EXPORT_KINDS)
            )
        resolved = self._check_path(path)
        document = self._document
        if kind == "png":
            export_png(document, resolved, transparent=transparent)
        elif kind == "pdf":
            export_pdf(document, resolved, outline_text=outline_text)
        else:
            export_svg(document, resolved, outline_text=outline_text)
        import os

        return self._ok(path=resolved, kind=kind, bytes=os.path.getsize(resolved))

    def manage_project(self, action: str, path: str | None = None) -> dict[str, Any]:
        if action not in _PROJECT_ACTIONS:
            raise AgentError(
                "invalid_enum",
                f"action は {list(_PROJECT_ACTIONS)} です",
                allowed=list(_PROJECT_ACTIONS),
            )
        window = self._window
        if action == "new":
            window._replace_document(_new_document())
            window._project_dir = None
            return self._ok(project_dir=None)
        if action == "open":
            if not path:
                raise AgentError(
                    "type_mismatch", "open には path（プロジェクトディレクトリ）が必要です"
                )
            resolved = self._check_path(path, must_exist=True)
            import os

            if not os.path.exists(os.path.join(resolved, PROJECT_JSON_NAME)):
                raise AgentError(
                    "file_not_found", f"{resolved} に {PROJECT_JSON_NAME} がありません"
                )
            window._replace_document(load_document(resolved))
            window._project_dir = resolved
            return self._ok(project_dir=resolved)
        # save / save_as
        target = path or window._project_dir
        if not target:
            raise AgentError("type_mismatch", "保存先が未設定です。path を指定してください")
        resolved = self._check_path(target)
        save_document(self._document, resolved)
        window._project_dir = resolved
        return self._ok(project_dir=resolved)

    # ------------------------------------------------------------------
    # SAM3 選択的マスキング（非同期ジョブ）
    # ------------------------------------------------------------------

    @property
    def jobs(self) -> Any:
        """`JobManager`（初回アクセス時に生成する）。"""
        if self._jobs is None:
            from app.agent.jobs import JobManager

            self._jobs = JobManager(self, self._window)
        return self._jobs

    def mask_image(
        self,
        object_id: int,
        prompt: str = "",
        boxes: list[list[float]] | None = None,
        color: str | None = "#FFFFFF",
        opacity: float = 0.8,
        threshold: float = 0.5,
    ) -> dict[str, Any]:
        """SAM3 で対象物をセグメンテーションし、対象外を覆う／切り抜く（**非同期**）。

        推論は数秒〜数分（初回はモデルのダウンロード）かかるため、即座に `job_id` を
        返して裏で走らせる。進捗と結果は `get_job` で取る。
        """
        from app.ai import sam3

        if not sam3.is_available():
            raise AgentError(
                "missing_dependency",
                "SAM3 が利用できません（torch / transformers 未導入）。"
                "`uv sync --group sam` で導入してください",
                install="uv sync --group sam",
            )
        obj = self._resolve(object_id)
        if obj.type != "image":
            raise AgentError(
                "type_mismatch",
                f"mask_image は image 型にだけ使えます（id={object_id} は {obj.type}）",
                id=object_id,
            )
        if not prompt and not boxes:
            raise AgentError(
                "type_mismatch",
                "prompt（テキスト）か boxes（[[x1,y1,x2,y2], ...] 元画像座標）の"
                "少なくとも一方が必要です",
            )
        if color is not None:
            from app.agent.validate import coerce

            _value, error = coerce("mask_color", color, {"kind": "color_opt", "nullable": True})
            if error is not None:
                raise AgentError(error.code, error.message, **error.extra)
        if not 0.0 <= opacity <= 1.0:
            raise AgentError("out_of_range", "opacity は 0.0-1.0 です", minimum=0.0, maximum=1.0)

        try:
            job = self.jobs.start_mask_image(object_id, prompt, boxes, color, opacity, threshold)
        except ValueError as exc:
            raise AgentError("file_not_found", str(exc), id=object_id) from exc
        return self._ok(
            **job.to_dict(),
            note="get_job で進捗と結果を確認してください（完了時に自動でマスクが適用されます）",
        )

    def get_job(self, job_id: str | None = None) -> dict[str, Any]:
        """非同期ジョブの状態。`job_id` 省略で全件返す。"""
        if job_id is None:
            return self._ok(jobs=self.jobs.snapshot())
        job = self.jobs.get(job_id)
        if job is None:
            raise AgentError(
                "unknown_id",
                f"ジョブ {job_id} はありません",
                available=[j["job_id"] for j in self.jobs.snapshot()],
            )
        return self._ok(**job.to_dict())

    # ------------------------------------------------------------------
    # 履歴
    # ------------------------------------------------------------------

    def history(self, direction: str = "undo", steps: int = 1) -> dict[str, Any]:
        if direction not in ("undo", "redo"):
            raise AgentError(
                "invalid_enum", "direction は 'undo' か 'redo' です", allowed=["undo", "redo"]
            )
        undo = self._undo
        applied: list[str] = []
        for _ in range(max(1, int(steps))):
            if direction == "undo":
                if not undo.canUndo():
                    break
                applied.append(undo.undoText())
                undo.undo()
            else:
                if not undo.canRedo():
                    break
                applied.append(undo.redoText())
                undo.redo()
        return self._ok(
            direction=direction,
            applied=applied,
            can_undo=undo.canUndo(),
            can_redo=undo.canRedo(),
        )

    # ------------------------------------------------------------------
    # 人間との協調
    # ------------------------------------------------------------------

    def set_selection(self, ids: list[int]) -> dict[str, Any]:
        """人間の選択状態を明示的に変更する（生成系は既定でこれを呼ばない）。"""
        objs = self._resolve_many(ids)
        self._scene.clearSelection()
        for obj in objs:
            item = self._scene.item_for(obj)
            if item is not None:
                item.setSelected(True)
        return self._ok(selection=[o.id for o in self._scene.selected_objects()])

    def highlight_objects(
        self, ids: list[int], label: str = "", duration_ms: int = 4000
    ) -> dict[str, Any]:
        """一時的なマーカーで対象を指し示す（ドキュメントには何も足さない）。

        マーカーは `CanvasScene._items` にも `Document.objects` にも入らないので、
        保存・スナップ・レイヤーパネル・3 系統の書き出しのいずれにも漏れない。
        """
        from app.agent.highlight import HighlightItem

        objs = self._resolve_many(ids)
        self._clear_highlights()
        for obj in objs:
            item = HighlightItem(resolved_bounding_box(self._document, obj), label or str(obj.id))
            self._scene.addItem(item)
            self._highlight_items.append(item)
        duration = max(500, min(30_000, int(duration_ms)))
        timer = QTimer(self._window)
        timer.setSingleShot(True)
        timer.timeout.connect(self._clear_highlights)
        timer.start(duration)
        self._highlight_timer = timer
        return self._ok(highlighted=[o.id for o in objs], duration_ms=duration)

    def _clear_highlights(self) -> None:
        for item in self._highlight_items:
            item_scene = item.scene()
            if item_scene is not None:
                item_scene.removeItem(item)
        self._highlight_items = []
        if self._highlight_timer is not None:
            self._highlight_timer.stop()
            self._highlight_timer = None


# --------------------------------------------------------------------------
# モジュール関数
# --------------------------------------------------------------------------


def _check_math_renderable(
    type_name: str,
    values: dict[str, Any],
    *,
    index: int | None = None,
    obj_id: int | None = None,
) -> FieldError | None:
    """math の latex を**適用前に**実レンダリングして検証する。

    適用してから失敗すると「モデルには入ったが絵は変わらない」というサイレント
    no-op になり、エージェントは成功したと誤認する。matplotlib のパーサ出力
    （"Unknown symbol: \\foo ..."）はそのまま actionable なので透過して返す。
    """
    if type_name != "math" or "latex" not in values:
        return None
    from app.math.mathtext_render import MathRenderError, get_math_svg

    try:
        get_math_svg(
            values["latex"], float(values.get("font_size", 18.0)), values.get("color", "#000000")
        )
    except MathRenderError as exc:
        return FieldError(
            code="math_render_failed",
            message=f"latex を描画できません: {exc}"
            "（mathtext は LaTeX のサブセット。\\usepackage 不可・日本語不可）",
            key="latex",
            index=index,
            id=obj_id,
        )
    return None


def _copy_value(value: Any) -> Any:
    """undo 用の旧値を撮る。リストは参照を共有しないようコピーする。"""
    if isinstance(value, list):
        return [_copy_value(v) for v in value]
    return value


def _png_base64(image: Any) -> str:
    buffer = QBuffer()
    buffer.open(QBuffer.OpenModeFlag.WriteOnly)
    if not image.save(buffer, "PNG"):
        raise AgentError("internal_error", "PNG のエンコードに失敗しました")
    data = QByteArray(buffer.data())
    buffer.close()
    return base64.b64encode(bytes(data)).decode("ascii")


def _new_document() -> Document:
    from app.ui.main_window import _default_document

    return _default_document()


def _format_outline(document: Document, entries: list[dict[str, Any]]) -> str:
    """大きなドキュメントを 1 オブジェクト 1 行で読ませるためのテキスト表。"""
    info = schema.artboard_info(document)
    lines = [
        f"artboard {info['width_px']}x{info['height_px']} px | "
        f"{info['physical']['width_mm']}mm @{info['physical']['target_dpi']}dpi "
        f"-> export {info['export_px'][0]}x{info['export_px'][1]} | "
        f"bg {info['background']} | revision {document.revision}",
        f"{'z':<3} {'id':<5} {'type':<10} {'name':<14} geometry",
    ]
    for entry in sorted(entries, key=lambda e: e["z_index"]):
        bbox = entry["bbox"]
        geom = f"x={bbox[0]:.0f} y={bbox[1]:.0f} w={bbox[2]:.0f} h={bbox[3]:.0f}"
        if "p1" in entry:
            geom = f"p1={tuple(entry['p1'])} p2={tuple(entry['p2'])}"
        elif "source_id" in entry:
            geom = f"{entry['source_id']} -> {entry['target_id']} ({entry['routing']})"
        lines.append(
            f"{entry['z_index']:<3} {entry['id']:<5} {entry['type']:<10} "
            f"{(entry['name'] or '-')[:14]:<14} {geom}"
        )
    return "\n".join(lines)


def known_types() -> list[str]:
    return sorted(OBJECT_REGISTRY)
