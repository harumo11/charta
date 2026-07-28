"""エージェント制御サーバが使うファイルパス（ランタイムディレクトリ・許可リスト）。

Qt 非依存。ソケット・レンダリング画像・インスタンス一覧はすべて
`$XDG_RUNTIME_DIR/charta/`（無ければ一時ディレクトリ）配下に置く。
このディレクトリは 0700、ソケットは 0600 にする。**ポートを開かないので、
ブラウザからは到達できない**——ローカル HTTP サーバの DNS リバインディング
（CVE-2025-66416 系）がそもそも成立しない、というのがこの設計の要点。
"""

from __future__ import annotations

import os
import tempfile
from pathlib import Path

_APP_DIR_NAME = "charta"
_RENDER_SUBDIR = "renders"

#: レンダリング画像のリングバッファ長。これを超えた古いものから消す。
RENDER_KEEP = 20

#: 環境変数でファイル操作の許可パスを追加できる（":" 区切り）。
ALLOWED_PATHS_ENV = "CHARTA_AGENT_PATHS"


def runtime_dir() -> Path:
    """`$XDG_RUNTIME_DIR/charta`（無ければ一時ディレクトリ）。0700 で作る。"""
    base = os.environ.get("XDG_RUNTIME_DIR")
    root = Path(base) if base else Path(tempfile.gettempdir())
    path = root / _APP_DIR_NAME
    path.mkdir(parents=True, exist_ok=True)
    try:
        path.chmod(0o700)
    except OSError:  # pragma: no cover - 一部のファイルシステムでは chmod 不可
        pass
    return path


def render_dir() -> Path:
    """レンダリング PNG の出力先。"""
    path = runtime_dir() / _RENDER_SUBDIR
    path.mkdir(parents=True, exist_ok=True)
    return path


def socket_path(pid: int | None = None) -> Path:
    """このプロセスの Unix ドメインソケットのパス。"""
    return runtime_dir() / f"{pid if pid is not None else os.getpid()}.sock"


def instances_path() -> Path:
    """起動中インスタンス一覧の JSON（ブリッジが接続先を選ぶのに使う）。"""
    return runtime_dir() / "instances.json"


def prune_renders(keep: int = RENDER_KEEP) -> None:
    """レンダリング画像を新しい順に `keep` 枚だけ残して削除する。"""
    try:
        files = sorted(render_dir().glob("*.png"), key=lambda p: p.stat().st_mtime, reverse=True)
    except OSError:  # pragma: no cover
        return
    for stale in files[keep:]:
        try:
            stale.unlink()
        except OSError:  # pragma: no cover
            pass


def default_allowed_roots(project_dir: str | None) -> list[Path]:
    """ファイル引数として既定で許可するディレクトリ。

    `charta_exec` を有効にしている以上これはセキュリティ境界ではなく**事故防止**
    （エージェントが `~/.config` などを取り違えて上書きするのを止める）。
    足りなければ `CHARTA_AGENT_PATHS` で足せる。
    """
    roots: list[Path] = [runtime_dir(), Path.cwd()]
    if project_dir:
        roots.append(Path(project_dir))
    home = Path.home()
    for name in ("Pictures", "ピクチャ", "Desktop", "Documents"):
        candidate = home / name
        if candidate.is_dir():
            roots.append(candidate)
    extra = os.environ.get(ALLOWED_PATHS_ENV, "")
    roots.extend(Path(p).expanduser() for p in extra.split(os.pathsep) if p)
    return roots


def is_within(path: str | os.PathLike[str], roots: list[Path]) -> bool:
    """`path` が `roots` のいずれかの配下か（シンボリックリンクを解決して判定）。"""
    target = Path(path).expanduser().resolve()
    for root in roots:
        try:
            resolved_root = root.expanduser().resolve()
        except OSError:  # pragma: no cover
            continue
        if target == resolved_root or resolved_root in target.parents:
            return True
    return False
