"""SAM3 実推論スモークテスト（contract_sam3 モジュール C・C-2）。

torch/transformers（sam 依存グループ）とネットワーク経由のモデルダウンロードを要する
重量スクリプト。CI やヘッドレステストでは実行しない（`tests/test_sam3_inference.py` が
`is_available()`/GPU 判定つきの任意実行を担う）。

usage: uv run --group sam python scripts/smoke_sam3.py IMAGE [TEXT]
       [--box X1,Y1,X2,Y2[,LABEL]]... [--out DIR]
"""

from __future__ import annotations

import argparse
import sys
import time
from pathlib import Path

_REPO_ROOT = str(Path(__file__).resolve().parent.parent)
if _REPO_ROOT not in sys.path:
    sys.path.insert(0, _REPO_ROOT)

import numpy as np  # noqa: E402
from PIL import Image  # noqa: E402

from app.ai import sam3  # noqa: E402
from app.graphics.image_pipeline import apply_mask_overlay  # noqa: E402


def _parse_box(raw: str) -> sam3.BoxPrompt:
    """ "X1,Y1,X2,Y2[,LABEL]" を `BoxPrompt` にパースする（LABEL 省略時 1=positive）。"""
    parts = [p.strip() for p in raw.split(",")]
    if len(parts) not in (4, 5):
        raise argparse.ArgumentTypeError(
            f"--box は X1,Y1,X2,Y2[,LABEL] 形式で指定してください: {raw!r}"
        )
    x1, y1, x2, y2 = (float(v) for v in parts[:4])
    label = int(parts[4]) if len(parts) == 5 else 1
    return (x1, y1, x2, y2), label


def _build_arg_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="SAM3 実推論スモークテスト")
    parser.add_argument("image", help="入力画像パス")
    parser.add_argument("text", nargs="?", default=None, help="対象物のテキストプロンプト")
    parser.add_argument(
        "--box",
        action="append",
        default=[],
        dest="boxes",
        type=_parse_box,
        help="ボックスプロンプト X1,Y1,X2,Y2[,LABEL]（複数指定可・LABEL省略時1=positive）",
    )
    parser.add_argument("--out", default=".", help="出力先ディレクトリ（既定: カレント）")
    return parser


def main() -> int:
    args = _build_arg_parser().parse_args()

    if not sam3.is_available():
        print("エラー: sam 依存グループが未導入です（uv sync --group sam）", file=sys.stderr)
        return 1
    if not args.text and not args.boxes:
        print("エラー: TEXT か --box の少なくとも一方を指定してください。", file=sys.stderr)
        return 1

    try:
        image = Image.open(args.image).convert("RGB")

        engine = sam3.get_engine()
        print("SAM3 モデルをロード中…（初回は数 GB のダウンロード）")
        t0 = time.monotonic()
        engine.load()
        print(f"ロード完了: {time.monotonic() - t0:.1f} 秒")

        engine.set_image(image)
        candidates = engine.detect(args.text, args.boxes or None)
    except sam3.Sam3Error as exc:
        print(f"エラー: {exc}", file=sys.stderr)
        return 1
    except Exception as exc:  # noqa: BLE001 - スモークスクリプトは全例外をエラー終了に変換する
        print(f"エラー: {exc}", file=sys.stderr)
        return 1

    if not candidates:
        print("候補が見つかりませんでした。")
        return 2

    print(f"候補数: {len(candidates)}")
    for i, candidate in enumerate(candidates):
        print(f"  [{i}] score={candidate.score:.4f} box={candidate.box}")

    size = (image.width, image.height)
    mask = sam3.combined_mask_u8(candidates, size)
    assert mask is not None  # 候補は accepted 既定 True のためここには来ない

    out_dir = Path(args.out)
    out_dir.mkdir(parents=True, exist_ok=True)

    mask_path = out_dir / "smoke_sam3_mask.png"
    Image.fromarray(mask, mode="L").save(mask_path)
    print(f"マスク保存: {mask_path}")

    arr = np.asarray(image.convert("RGBA"), dtype=np.uint8)
    overlay = apply_mask_overlay(arr, mask, "#FFFFFF", 0.8)
    overlay_path = out_dir / "smoke_sam3_overlay.png"
    Image.fromarray(overlay, mode="RGBA").save(overlay_path)
    print(f"合成プレビュー保存: {overlay_path}")

    return 0


if __name__ == "__main__":
    sys.exit(main())
