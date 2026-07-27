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


def parse_hex_color(color: str) -> tuple[int, int, int]:
    """`"#RRGGBB"` 形式の文字列を (r, g, b) の 0-255 整数タプルに変換する。

    形式不正（`#` 始まりでない・長さ不正・16進変換不可）は `ValueError` を送出する。
    """
    if not isinstance(color, str) or len(color) != 7 or not color.startswith("#"):
        raise ValueError(f"invalid color: {color!r}")
    try:
        r = int(color[1:3], 16)
        g = int(color[3:5], 16)
        b = int(color[5:7], 16)
    except ValueError as exc:
        raise ValueError(f"invalid color: {color!r}") from exc
    return (r, g, b)


def load_mask_gray(
    document: Document | None, obj: ImageObject, expected_size: tuple[int, int] | None = None
) -> np.ndarray | None:
    """`obj.mask_src` を PIL mode "L" で読み込み uint8 [H,W] 配列にする。

    `mask_src` が未設定（None/空文字）なら None を返す。読込失敗時は
    `load_source_rgba` と同様に warn + stderr print して None を返す（マスク無し扱いで
    表示を継続する）。`expected_size=(width, height)` が与えられ実寸と異なる場合は
    warn した上で `Image.NEAREST` によりリサイズして合わせる（防御的処理）。
    """
    src = getattr(obj, "mask_src", None)
    if not src:
        return None
    try:
        path = resolve_asset_path(document, src) if document is not None else src
        with Image.open(path) as im:
            gray = im.convert("L")
            if expected_size is not None and gray.size != tuple(expected_size):
                message = (
                    f"charta: マスク画像の寸法が一致しません mask_src={src!r} "
                    f"実寸={gray.size} 期待寸={tuple(expected_size)}"
                )
                warnings.warn(message, stacklevel=2)
                gray = gray.resize(tuple(expected_size), Image.NEAREST)
            arr = np.asarray(gray, dtype=np.uint8)
        return np.ascontiguousarray(arr)
    except Exception as exc:  # noqa: BLE001 - マスク読込失敗は呼び出し側でマスク無し扱いにする
        message = f"charta: マスク画像の読込に失敗しました mask_src={src!r}: {exc}"
        warnings.warn(message, stacklevel=2)
        print(message, file=sys.stderr)
        return None


def apply_mask_overlay(
    arr: np.ndarray,
    mask: np.ndarray,
    color: str | None,
    opacity: float,
) -> np.ndarray:
    """マスクに基づき対象外領域を覆う（契約書冒頭の意味論どおり・全ベクトル化）。

    `coverage = (1 - mask/255) * clip(opacity, 0, 1)` を画素ごとの覆い強度とし、
    `color` が `"#RRGGBB"` のとき rgb をブレンドして alpha は素通し、`color is None`
    （透明=切り取り）のとき alpha を減衰させて rgb は素通しにする。`arr` と `mask` の
    H,W が不一致なら `ValueError`（防御は呼び出し側 `load_mask_gray`/`apply_crop` の責務）。
    """
    if arr.shape[0] != mask.shape[0] or arr.shape[1] != mask.shape[1]:
        raise ValueError(f"mask size mismatch: arr={arr.shape[:2]} mask={mask.shape[:2]}")

    op = float(np.clip(opacity, 0.0, 1.0))
    if op == 0.0 or bool(np.all(mask == 255)):
        return np.ascontiguousarray(arr)

    coverage = (1.0 - mask.astype(np.float32) / 255.0) * op
    coverage = coverage[..., np.newaxis]  # [H,W,1]

    if color is not None:
        r, g, b = parse_hex_color(color)
        color_rgb = np.array([r, g, b], dtype=np.float32)
        rgb = arr[..., :3].astype(np.float32)
        rgb = rgb * (1.0 - coverage) + color_rgb * coverage
        rgb = np.clip(np.round(rgb), 0.0, 255.0).astype(np.uint8)
        alpha = arr[..., 3:4]
        out = np.concatenate([rgb, alpha], axis=-1)
    else:
        rgb = arr[..., :3]
        alpha = arr[..., 3:4].astype(np.float32)
        alpha = np.clip(np.round(alpha * (1.0 - coverage)), 0.0, 255.0).astype(np.uint8)
        out = np.concatenate([rgb, alpha], axis=-1)

    return np.ascontiguousarray(out.astype(np.uint8))


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
    """crop・brightness/contrast・マスク覆いを反映した最終 RGBA 配列を返す（読込失敗時 None）。"""
    arr = load_source_rgba(document, obj)
    if arr is None:
        return None
    source_size = (int(arr.shape[1]), int(arr.shape[0]))  # (width, height)
    arr = apply_crop(arr, getattr(obj, "crop", None))
    arr = apply_brightness_contrast(arr, obj.brightness, obj.contrast)
    arr = apply_mask_if_any(document, obj, arr, source_size)
    return arr


def apply_mask_if_any(
    document: Document | None,
    obj: ImageObject,
    arr: np.ndarray,
    source_size: tuple[int, int] | None,
) -> np.ndarray:
    """`mask_src` が有効かつ `mask_enabled` のときマスクを読み込み覆いを適用して返す。

    マスク未設定・無効化・読込失敗時は `arr` をそのまま返す（表示継続を優先）。
    `ImageItem`（キャンバス表示）がキャッシュ済み配列に対して同じ合成を再現するための
    共有関数。マスクは crop 前の元画像座標で保存されているため、`arr` に適用済みの
    crop と同一の crop をマスクにも適用してから重ねる。
    """
    mask_src = getattr(obj, "mask_src", None)
    if not mask_src or not getattr(obj, "mask_enabled", True):
        return arr
    mask = load_mask_gray(document, obj, expected_size=source_size)
    if mask is None:
        return arr
    mask = apply_crop(mask, getattr(obj, "crop", None))
    return apply_mask_overlay(arr, mask, obj.mask_color, obj.mask_opacity)


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
