"""project.json の読み書き（§3・§6）。Qt 非依存。"""

from __future__ import annotations

import json
import os
import shutil
from pathlib import Path
from typing import TYPE_CHECKING, Any

from PIL import Image

from app.model.document import Document

if TYPE_CHECKING:
    import numpy as np

PROJECT_JSON_NAME = "project.json"


def document_to_json(doc: Document) -> dict[str, Any]:
    """Document を project.json 相当の辞書に変換する。"""
    return doc.to_dict()


def document_from_json(d: dict[str, Any]) -> Document:
    """project.json 相当の辞書から Document を復元する。"""
    return Document.from_dict(d)


def save_document(doc: Document, project_dir: str | os.PathLike[str]) -> None:
    """project_dir/ に project.json・assets/・exports/ を作成して保存する。

    画像の assets/ 複製は M3 で実装する。M1 では assets/ ディレクトリ作成のみ。
    """
    root = Path(project_dir)
    root.mkdir(parents=True, exist_ok=True)
    (root / "assets").mkdir(parents=True, exist_ok=True)
    (root / "exports").mkdir(parents=True, exist_ok=True)

    data = document_to_json(doc)
    project_json_path = root / PROJECT_JSON_NAME
    with project_json_path.open("w", encoding="utf-8") as f:
        json.dump(data, f, indent=2, ensure_ascii=False)

    doc.base_dir = str(os.path.abspath(project_dir))


def load_document(project_dir: str | os.PathLike[str]) -> Document:
    """project_dir/project.json を読み Document を返す。"""
    root = Path(project_dir)
    project_json_path = root / PROJECT_JSON_NAME
    with project_json_path.open(encoding="utf-8") as f:
        data = json.load(f)
    doc = document_from_json(data)
    doc.base_dir = str(os.path.abspath(project_dir))
    return doc


def migrate_assets(
    old_base_dir: str | os.PathLike[str], new_project_dir: str | os.PathLike[str]
) -> None:
    """`old_base_dir/assets` を `new_project_dir/assets` へ複製する。

    未保存プロジェクト（一時ディレクトリ基点）や既存プロジェクトを別ディレクトリへ
    「名前を付けて保存」するとき、取り込み済み画像を引き継ぐ。undo 履歴から復元され
    得る画像も含めるため、オブジェクト参照の走査ではなくディレクトリ全体を複製する。
    assets/ が無い・移行元と移行先が同一の場合は何もしない。
    """
    src = Path(old_base_dir) / "assets"
    if not src.is_dir():
        return
    dest = Path(new_project_dir) / "assets"
    if src.resolve() == dest.resolve():
        return
    shutil.copytree(src, dest, dirs_exist_ok=True)


def resolve_asset_path(doc: Document, src: str) -> str:
    """画像 `src` を実ファイルパスへ解決する。

    絶対パスはそのまま返す。相対パスは `doc.base_dir` と連結する。
    `doc.base_dir` が None の場合は `src` をそのまま返す。
    """
    if os.path.isabs(src):
        return src
    if doc.base_dir is None:
        return src
    return os.path.join(doc.base_dir, src)


def import_image(doc: Document, src_path: str) -> str:
    """画像ファイルを `assets/` に一意な名前で複製し、相対パスを返す。

    `doc.base_dir` が未設定（プロジェクト未保存）の場合は RuntimeError。
    """
    if doc.base_dir is None:
        raise RuntimeError("doc.base_dir is not set; save the project before importing images")
    assets_dir = Path(doc.base_dir) / "assets"
    assets_dir.mkdir(parents=True, exist_ok=True)

    ext = Path(src_path).suffix.lower()

    used_numbers: set[int] = set()
    for p in assets_dir.glob("img_*"):
        stem = p.stem
        _, _, num_part = stem.partition("_")
        if num_part.isdigit():
            used_numbers.add(int(num_part))

    n = 1
    while n in used_numbers:
        n += 1
    name = f"img_{n:03d}{ext}"

    dest = assets_dir / name
    shutil.copy2(src_path, dest)
    return f"assets/{name}"


def save_mask_png(doc: Document, mask: np.ndarray) -> str:
    """uint8 グレースケール配列 [H,W] を `assets/mask_NNN.png` に保存し相対パスを返す。

    `doc.base_dir` が未設定（プロジェクト未保存）の場合は RuntimeError。
    連番は `import_image` と同方式: `assets_dir.glob("mask_*")` の stem
    "mask_NNN" から使用済み番号を集め、最小の未使用 n で `mask_{n:03d}.png` とする。
    """
    if doc.base_dir is None:
        raise RuntimeError("doc.base_dir is not set; save the project before importing images")
    assets_dir = Path(doc.base_dir) / "assets"
    assets_dir.mkdir(parents=True, exist_ok=True)

    used_numbers: set[int] = set()
    for p in assets_dir.glob("mask_*"):
        stem = p.stem
        _, _, num_part = stem.partition("_")
        if num_part.isdigit():
            used_numbers.add(int(num_part))

    n = 1
    while n in used_numbers:
        n += 1
    name = f"mask_{n:03d}.png"

    dest = assets_dir / name
    Image.fromarray(mask, mode="L").save(dest)
    return f"assets/{name}"
