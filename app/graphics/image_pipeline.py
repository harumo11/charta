"""画像処理パイプライン（Qt 非依存）。

`app/scene/items/image_item.py` の `ImageItem` が表示用に使っていた画像処理ロジックを
自由関数として抽出したもの（ロジック無変更）。`Document`/モデルのみから最終ビットマップ
（crop・brightness/contrast 反映済み RGBA、または PNG base64）を得たい呼び出し元
（SVG エクスポート等）が `ImageItem` インスタンス無しで呼べるようにするための置き場。

表示パイプラインは PIL 読込 → numpy RGBA → crop スライス → brightness/contrast
（ベクトル化）の順。すべて numpy に委譲し Python の for ループで画素処理をしない
（CLAUDE.md §13）。
"""

from __future__ import annotations

import base64
import io
import sys
import warnings
from typing import TYPE_CHECKING

import numpy as np
from PIL import Image

from app.model.objects import ImageObject
from app.model.serialize import resolve_asset_path

if TYPE_CHECKING:
    from app.model.document import Document


def load_source_rgba(document: Document, obj: ImageObject) -> np.ndarray | None:
    """`obj.src` を PIL で読み込み RGBA numpy 配列にする。失敗時は warn して None を返す。"""
    src = obj.src
    try:
        path = resolve_asset_path(document, src) if document is not None else src
        with Image.open(path) as im:
            rgba = im.convert("RGBA")
            arr = np.asarray(rgba, dtype=np.uint8)
        return np.ascontiguousarray(arr)
    except (
        Exception
    ) as exc:  # noqa: BLE001 - 画像読込失敗は呼び出し側でプレースホルダ等の対応をする
        message = f"charta: 画像の読込に失敗しました src={src!r}: {exc}"
        warnings.warn(message, stacklevel=2)
        print(message, file=sys.stderr)
        return None


def apply_crop(arr: np.ndarray, crop: list[float] | None) -> np.ndarray:
    """crop 矩形（元画像座標）でスライスする。crop が無ければ入力をそのまま返す。

    現 `ImageItem._apply_crop`（image_item.py:166 付近）と同一ロジック。
    """
    h, w = arr.shape[0], arr.shape[1]
    if not crop:
        return arr
    x, y, cw, ch = crop
    x0 = int(np.clip(round(x), 0, w))
    y0 = int(np.clip(round(y), 0, h))
    x1 = int(np.clip(round(x + cw), 0, w))
    y1 = int(np.clip(round(y + ch), 0, h))
    if x1 <= x0 or y1 <= y0:
        return arr
    return arr[y0:y1, x0:x1]


def apply_brightness_contrast(arr: np.ndarray, brightness: float, contrast: float) -> np.ndarray:
    """brightness/contrast をベクトル化して適用する（RGB のみ、alpha は素通し）。

    現 `ImageItem._apply_brightness_contrast`（image_item.py:180 付近）と同一ロジック。
    """
    if brightness == 0.0 and contrast == 0.0:
        return np.ascontiguousarray(arr)
    rgb = arr[..., :3].astype(np.float32)
    alpha = arr[..., 3:4]
    f = 1.0 + float(contrast)
    rgb = (rgb - 128.0) * f + 128.0
    rgb = rgb + float(brightness) * 255.0
    rgb = np.clip(rgb, 0.0, 255.0).astype(np.uint8)
    return np.ascontiguousarray(np.concatenate([rgb, alpha], axis=-1))


def build_processed_rgba(document: Document, obj: ImageObject) -> np.ndarray | None:
    """crop・brightness/contrast を反映した最終 RGBA 配列を返す（読込失敗時 None）。"""
    arr = load_source_rgba(document, obj)
    if arr is None:
        return None
    arr = apply_crop(arr, getattr(obj, "crop", None))
    arr = apply_brightness_contrast(arr, obj.brightness, obj.contrast)
    return arr


def processed_png_base64(document: Document, obj: ImageObject) -> str | None:
    """最終ビットマップを PNG 化して base64 文字列を返す（失敗時 None）。"""
    arr = build_processed_rgba(document, obj)
    if arr is None:
        return None
    image = Image.fromarray(arr, mode="RGBA")
    buffer = io.BytesIO()
    image.save(buffer, format="PNG")
    return base64.b64encode(buffer.getvalue()).decode("ascii")


def compute_default_size(
    orig_width: float, orig_height: float, artboard_width: float, artboard_height: float
) -> tuple[float, float]:
    """取り込み時の既定表示サイズを計算する。

    アートボードより大きい場合はアスペクト比を保って縮小する。元画素は保持し、
    表示側でスケールするだけなので画質は劣化しない。
    """
    w = float(orig_width)
    h = float(orig_height)
    if w <= 0.0 or h <= 0.0:
        return (max(w, 1.0), max(h, 1.0))
    if w <= artboard_width and h <= artboard_height:
        return (w, h)
    scale = min(artboard_width / w, artboard_height / h)
    return (w * scale, h * scale)
