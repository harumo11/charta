"""エージェント向けレンダリング（クリーンな図 / 実ウィンドウ / 注釈オーバーレイ）。

2 つの「見え方」を区別して提供する。片方しか無いと、エージェントは自分が
見ていないものについて断言してしまう:

* ``artboard`` — `Document` から作った使い捨てシーンを描く。選択ハンドル・
  グリッド・スナップガイド・crop オーバーレイを含まない、書き出しと同じ図。
* ``window``   — `view.grab()`。**人間が今まさに見ている画面**。編集中の状態や
  開いているダイアログが写る。

サイズ規律: 縮小前提で大きく描くのではなく、**目標サイズに直接描く**。
ベクタなので目標解像度で描くほうが鮮明かつ安い。

注釈オーバーレイは `scene.render()` の**後で QImage に直接** `QPainter` で描く。
モデルにもライブシーンにも触らないので、書き出しに混入する余地が無い。
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass
from typing import Any

from PySide6.QtCore import QRectF, Qt
from PySide6.QtGui import QColor, QFont, QImage, QPainter, QPen

from app.agent import paths
from app.graphics.routing import connector_endpoints_from_model, resolved_bounding_box
from app.model.document import Document
from app.model.objects import BaseObject
from app.scene.canvas_scene import CanvasScene

#: 既定の長辺ピクセル数。1024 で画像トークン ≈790。研究図の確認にはこれで足りる。
DEFAULT_MAX_EDGE = 1024
MIN_MAX_EDGE = 256
MAX_MAX_EDGE = 2048

#: オーバーレイの配色（スナップガイドと同じアクセント色を使う）。
_ACCENT = QColor(255, 0, 170)
_ACCENT_SOFT = QColor(255, 0, 170, 150)
_LABEL_TEXT = QColor(255, 255, 255)
_HALO = QColor(255, 255, 255, 200)

_OVERLAY_MODES = ("none", "labels", "boxes", "full")

Region = tuple[float, float, float, float]


@dataclass(frozen=True)
class ViewInfo:
    """レンダリング結果とアートボード座標の対応。

    エージェントは画像を見て「ここに矢印を置いて」と言うので、画像 px →
    アートボード px の変換規則を必ず一緒に返す必要がある。
    """

    region: Region
    image_width: int
    image_height: int
    scale_x: float
    scale_y: float
    source: str

    def to_image(self, x: float, y: float) -> tuple[float, float]:
        return ((x - self.region[0]) * self.scale_x, (y - self.region[1]) * self.scale_y)

    def to_image_box(self, box: tuple[float, float, float, float]) -> list[float]:
        ix, iy = self.to_image(box[0], box[1])
        return [ix, iy, box[2] * self.scale_x, box[3] * self.scale_y]

    def to_dict(self) -> dict[str, Any]:
        return {
            "source": self.source,
            "region": {
                "x": self.region[0],
                "y": self.region[1],
                "width": self.region[2],
                "height": self.region[3],
            },
            "image": {"width": self.image_width, "height": self.image_height, "format": "png"},
            "scale_x": self.scale_x,
            "scale_y": self.scale_y,
            "mapping": (
                "artboard_x = region.x + image_x / scale_x ; "
                "artboard_y = region.y + image_y / scale_y"
            ),
        }


def clamp_max_edge(max_edge: int | None) -> int:
    if max_edge is None:
        return DEFAULT_MAX_EDGE
    return max(MIN_MAX_EDGE, min(MAX_MAX_EDGE, int(max_edge)))


def full_region(document: Document) -> Region:
    artboard = document.artboard
    return (0.0, 0.0, float(artboard.width_px), float(artboard.height_px))


def union_region(document: Document, object_ids: list[int], padding: float = 24.0) -> Region | None:
    """`object_ids` の bbox の和集合（＋余白）。1 つも見つからなければ None。"""
    boxes: list[tuple[float, float, float, float]] = []
    for oid in object_ids:
        obj = document.object_by_id(oid)
        if obj is not None:
            boxes.append(resolved_bounding_box(document, obj))
    if not boxes:
        return None
    x0 = min(b[0] for b in boxes) - padding
    y0 = min(b[1] for b in boxes) - padding
    x1 = max(b[0] + b[2] for b in boxes) + padding
    y1 = max(b[1] + b[3] for b in boxes) + padding
    # 幅ゼロ（水平な直線だけを指定した場合など）は潰れないよう最小サイズを持たせる。
    return (x0, y0, max(x1 - x0, 1.0), max(y1 - y0, 1.0))


def _image_size(region: Region, max_edge: int) -> tuple[int, int]:
    rw, rh = max(region[2], 1e-6), max(region[3], 1e-6)
    scale = max_edge / max(rw, rh)
    return (max(1, round(rw * scale)), max(1, round(rh * scale)))


def render_document(
    document: Document,
    *,
    region: Region | None = None,
    max_edge: int = DEFAULT_MAX_EDGE,
    transparent: bool = False,
) -> tuple[QImage, ViewInfo]:
    """`document` の指定領域を目標サイズに直接描いて返す。

    `scene.render` には `IgnoreAspectRatio` を渡す。目標サイズは region の
    アスペクト比から計算しているので実質等倍だが、丸め誤差でレターボックスが
    入ると座標対応が 1px ずれるため明示的に無効化する。真のスケールは
    丸め後の `scale_x` / `scale_y` として返す。
    """
    region = region if region is not None else full_region(document)
    img_w, img_h = _image_size(region, clamp_max_edge(max_edge))
    view = ViewInfo(
        region=region,
        image_width=img_w,
        image_height=img_h,
        scale_x=img_w / max(region[2], 1e-6),
        scale_y=img_h / max(region[3], 1e-6),
        source="artboard",
    )

    # 使い捨てシーンは必ず閉じる（`CanvasScene.close`）。
    with CanvasScene(document) as scene:
        if transparent:
            scene.set_background_visible(False)
        image = QImage(img_w, img_h, QImage.Format.Format_ARGB32)
        if transparent:
            image.fill(Qt.GlobalColor.transparent)
        else:
            image.fill(QColor(document.artboard.background))
        painter = QPainter(image)
        if not painter.isActive():
            raise OSError("レンダリング用の QPainter を初期化できませんでした")
        try:
            painter.setRenderHint(QPainter.RenderHint.Antialiasing, True)
            painter.setRenderHint(QPainter.RenderHint.SmoothPixmapTransform, True)
            painter.setRenderHint(QPainter.RenderHint.TextAntialiasing, True)
            scene.render(
                painter,
                QRectF(0.0, 0.0, float(img_w), float(img_h)),
                QRectF(*region),
                Qt.AspectRatioMode.IgnoreAspectRatio,
            )
        finally:
            painter.end()
    return image, view


def render_window(view_widget: Any, max_edge: int = DEFAULT_MAX_EDGE) -> tuple[QImage, ViewInfo]:
    """人間が見ている `CanvasView` をそのまま撮る（編集中の状態が写る）。

    `region` はビューポートに映っているアートボード領域を報告するので、
    ここに写っているものの座標もアートボード px に戻せる。
    """
    pixmap = view_widget.grab()
    image = pixmap.toImage()
    src_w = max(image.width(), 1)
    src_h = max(image.height(), 1)
    limit = clamp_max_edge(max_edge)
    if max(src_w, src_h) > limit:
        image = image.scaled(
            limit if src_w >= src_h else round(src_w * limit / src_h),
            limit if src_h > src_w else round(src_h * limit / src_w),
            Qt.AspectRatioMode.KeepAspectRatio,
            Qt.TransformationMode.SmoothTransformation,
        )

    scene_rect = view_widget.mapToScene(view_widget.viewport().rect()).boundingRect()
    region: Region = (
        scene_rect.x(),
        scene_rect.y(),
        max(scene_rect.width(), 1e-6),
        max(scene_rect.height(), 1e-6),
    )
    return image, ViewInfo(
        region=region,
        image_width=image.width(),
        image_height=image.height(),
        scale_x=image.width() / region[2],
        scale_y=image.height() / region[3],
        source="window",
    )


# --------------------------------------------------------------------------
# オブジェクトのメタデータ（どのピクセルがどの id か）
# --------------------------------------------------------------------------


def _endpoints_of(document: Document, obj: BaseObject) -> tuple[tuple[float, float], ...]:
    """端点系オブジェクトの端点（アートボード座標）。box 型は空タプル。"""
    if obj.GEOMETRY == "endpoints":
        return ((float(obj.p1[0]), float(obj.p1[1])), (float(obj.p2[0]), float(obj.p2[1])))
    if obj.GEOMETRY == "connector":
        return connector_endpoints_from_model(document, obj)
    return ()


def object_boxes(
    document: Document, view: ViewInfo, *, visible_only: bool = True
) -> list[dict[str, Any]]:
    """各オブジェクトの bbox を **画像 px とアートボード px の両方で** 返す。

    これがあればオーバーレイを描かなくても「どのピクセルがどの id か」が解ける。
    """
    result: list[dict[str, Any]] = []
    for obj in document.objects:
        if visible_only and not obj.visible:
            continue
        box = resolved_bounding_box(document, obj)
        entry: dict[str, Any] = {
            "id": obj.id,
            "type": obj.type,
            "name": obj.name,
            "artboard_bbox": list(box),
            "image_bbox": view.to_image_box(box),
            "visible": obj.visible,
            "locked": obj.locked,
        }
        endpoints = _endpoints_of(document, obj)
        if endpoints:
            entry["endpoints"] = {
                "p1": list(endpoints[0]),
                "p2": list(endpoints[1]),
                "p1_image": list(view.to_image(*endpoints[0])),
                "p2_image": list(view.to_image(*endpoints[1])),
            }
        result.append(entry)
    return result


def offscreen_warnings(document: Document) -> list[dict[str, Any]]:
    """アートボードの外に完全に出ているオブジェクト（描いたのに見えない、の主因）。"""
    artboard = document.artboard
    aw, ah = float(artboard.width_px), float(artboard.height_px)
    warnings: list[dict[str, Any]] = []
    for obj in document.objects:
        if not obj.visible:
            continue
        x, y, w, h = resolved_bounding_box(document, obj)
        if x + w < 0 or y + h < 0 or x > aw or y > ah:
            warnings.append(
                {
                    "code": "offscreen",
                    "id": obj.id,
                    "message": f"オブジェクト {obj.id} ({obj.type}) は"
                    f" bbox {[x, y, w, h]} でアートボードの外にあり、描画されません",
                }
            )
        elif obj.GEOMETRY == "box" and (w <= 0.0 or h <= 0.0):
            warnings.append(
                {
                    "code": "degenerate",
                    "id": obj.id,
                    "message": f"オブジェクト {obj.id} ({obj.type}) は"
                    f" width={w} height={h} のため不可視です",
                }
            )
    return warnings


# --------------------------------------------------------------------------
# 注釈オーバーレイ
# --------------------------------------------------------------------------


def _draw_label(painter: QPainter, x: float, y: float, text: str, occupied: list[QRectF]) -> None:
    """id ラベルのチップを、既に置いたチップと重ならない位置に描く。"""
    metrics = painter.fontMetrics()
    tw = metrics.horizontalAdvance(text) + 8
    th = metrics.height() + 2
    candidates = [
        QRectF(x, y - th - 2, tw, th),  # 左上の外
        QRectF(x, y + 2, tw, th),  # 左上の内
        QRectF(x - tw - 2, y - th - 2, tw, th),
        QRectF(x, y - th - 2 - th - 2, tw, th),
    ]
    rect = candidates[0]
    for candidate in candidates:
        if not any(candidate.intersects(other) for other in occupied):
            rect = candidate
            break
    occupied.append(rect)

    painter.setPen(QPen(_HALO, 2.0))
    painter.setBrush(_ACCENT)
    painter.drawRect(rect)
    painter.setPen(QPen(_LABEL_TEXT))
    painter.drawText(rect.adjusted(4, 0, -4, 0), Qt.AlignmentFlag.AlignCenter, text)


def draw_overlay(
    image: QImage,
    document: Document,
    view: ViewInfo,
    mode: str = "full",
    selected_ids: set[int] | None = None,
) -> None:
    """`image` に id ラベル・bbox・端点ドットを直接描く（モデルには触らない）。

    `mode`: "none" | "labels" | "boxes" | "full"
    """
    if mode == "none":
        return
    if mode not in _OVERLAY_MODES:
        raise ValueError(f"未知の overlay モード: {mode!r}（{list(_OVERLAY_MODES)} のいずれか）")
    draw_boxes = mode in ("boxes", "full")
    draw_labels = mode in ("labels", "full")
    selected = selected_ids or set()

    painter = QPainter(image)
    if not painter.isActive():  # pragma: no cover - offscreen では基本起きない
        return
    try:
        painter.setRenderHint(QPainter.RenderHint.Antialiasing, True)
        font = QFont()
        font.setPixelSize(11)
        painter.setFont(font)
        occupied: list[QRectF] = []

        for obj in document.objects:
            if not obj.visible:
                continue
            box = resolved_bounding_box(document, obj)
            ix, iy, iw, ih = view.to_image_box(box)
            is_selected = obj.id in selected

            if draw_boxes:
                pen = QPen(_ACCENT_SOFT, 2.0 if is_selected else 1.0)
                if not is_selected:
                    pen.setStyle(Qt.PenStyle.DashLine)
                painter.setPen(pen)
                painter.setBrush(Qt.BrushStyle.NoBrush)
                endpoints = _endpoints_of(document, obj)
                if endpoints:
                    # 端点系は bbox より端点そのものが要る情報。
                    painter.setBrush(_ACCENT)
                    for i, point in enumerate(endpoints):
                        px, py = view.to_image(*point)
                        painter.setBrush(_ACCENT if i == 0 else Qt.BrushStyle.NoBrush)
                        painter.drawEllipse(QRectF(px - 3.0, py - 3.0, 6.0, 6.0))
                    painter.setBrush(Qt.BrushStyle.NoBrush)
                else:
                    painter.drawRect(QRectF(ix, iy, iw, ih))

            if draw_labels:
                _draw_label(painter, ix, iy, f"{obj.id}", occupied)
    finally:
        painter.end()


# --------------------------------------------------------------------------
# ファイル出力
# --------------------------------------------------------------------------


def save_render(image: QImage, document: Document, tag: str = "") -> str:
    """レンダリング結果を `$XDG_RUNTIME_DIR/charta/renders/` に書き、絶対パスを返す。

    **MCP でインライン返却しないのが要点。** base64 画像はクライアント側で
    テキストとして数万トークン消費し、出力上限にも掛かる。パスを返して
    エージェントの組込み Read に読ませるほうが 10 倍以上安く、上限にも掛からない。
    """
    digest = hashlib.sha1(
        f"{document.uid}:{document.revision}:{tag}:{image.width()}x{image.height()}".encode()
    ).hexdigest()[:8]
    path = paths.render_dir() / f"{document.uid[:8]}-r{document.revision}-{digest}.png"
    if not image.save(str(path), "PNG"):
        raise OSError(f"レンダリング画像の保存に失敗しました: {path}")
    paths.prune_renders()
    return str(path)
