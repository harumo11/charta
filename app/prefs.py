"""アプリ全体の環境設定（プロジェクト非依存・JSON 永続化）。

保存先は `$CHARTA_CONFIG_DIR` > `$XDG_CONFIG_HOME/charta` > `~/.config/charta` の
`prefs.json`。壊れたファイル・未知キー・欠落キーに寛容（例外を外に出さない）。
document/project.json（`app/model/document.py`・`app/model/serialize.py`）とは
無関係——こちらはユーザー環境そのものの設定であり、プロジェクトを跨いで生きる。

Qt 非依存（`app/prefs.py` は PySide6 を import しない）。呼び出し側
（`app/ui/main_window.py`）が `Preferences` を読み書きし、Qt の
`QColorDialog` カスタムスウォッチ等へ反映する。
"""

from __future__ import annotations

import json
import os
import tempfile
import warnings
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any

#: prefs.json のファイル名。config_dir() の直下に置く。
_PREFS_FILENAME = "prefs.json"


@dataclass
class Preferences:
    """ユーザー環境設定（§CLAUDE.md 全体に横断する、単一ユーザー前提の設定）。

    フィールドの意味は `.claude/working` の契約（環境設定/カラーパレット）を参照。
    ここでの既定値は「設定ファイルが無いときの charta の従来の挙動」と一致させる
    （既存ユーザーが設定を作るまでは今までどおりに動く、という後方互換の要件）。
    """

    version: int = 1
    # パレット
    palette_id: str = ""  # "" = パレットなし。app.model.palettes の id
    initial_color: str | None = None  # 新規図形の線色/文字色。None = 従来既定(#000000)
    # 新規オブジェクトの既定
    default_font_family: str = "Noto Sans CJK JP"
    default_font_size: float = 18.0
    default_stroke_width: float = 2.0
    default_connector_routing: str = "orthogonal"  # "orthogonal" | "straight"
    # 新規アートボードの既定
    artboard_width_px: float = 1920.0
    artboard_height_px: float = 1080.0
    artboard_width_mm: float = 170.0
    artboard_dpi: int = 300
    artboard_background: str = "#FFFFFF"
    # 自動保存・書き出し
    autosave_interval_s: int = 30  # 0 = 自動保存 OFF
    export_outline_text: bool = False
    export_transparent_png: bool = False
    export_confirm: bool = True  # False = 確認ダイアログを出さず既定値で書き出す
    # 自動記憶（設定ダイアログには出さない）
    window_geometry: list[int] | None = None  # [x, y, w, h]
    grid_visible: bool = False
    snap_enabled: bool = True

    def to_dict(self) -> dict[str, Any]:
        """`dataclasses.asdict` そのまま（全フィールドが JSON 化可能な素朴な型のため）。"""
        return asdict(self)

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> Preferences:
        """未知キー無視・欠落は既定値・型が合わない値は既定値に落とす（例外を出さない）。"""
        defaults = cls()
        return cls(
            version=_coerce_int(d.get("version"), defaults.version),
            palette_id=_coerce_str(d.get("palette_id"), defaults.palette_id),
            initial_color=_coerce_optional_str(d.get("initial_color"), defaults.initial_color),
            default_font_family=_coerce_str(
                d.get("default_font_family"), defaults.default_font_family
            ),
            default_font_size=_coerce_float(d.get("default_font_size"), defaults.default_font_size),
            default_stroke_width=_coerce_float(
                d.get("default_stroke_width"), defaults.default_stroke_width
            ),
            default_connector_routing=_coerce_str(
                d.get("default_connector_routing"), defaults.default_connector_routing
            ),
            artboard_width_px=_coerce_float(d.get("artboard_width_px"), defaults.artboard_width_px),
            artboard_height_px=_coerce_float(
                d.get("artboard_height_px"), defaults.artboard_height_px
            ),
            artboard_width_mm=_coerce_float(d.get("artboard_width_mm"), defaults.artboard_width_mm),
            artboard_dpi=_coerce_int(d.get("artboard_dpi"), defaults.artboard_dpi),
            artboard_background=_coerce_str(
                d.get("artboard_background"), defaults.artboard_background
            ),
            autosave_interval_s=_coerce_int(
                d.get("autosave_interval_s"), defaults.autosave_interval_s
            ),
            export_outline_text=_coerce_bool(
                d.get("export_outline_text"), defaults.export_outline_text
            ),
            export_transparent_png=_coerce_bool(
                d.get("export_transparent_png"), defaults.export_transparent_png
            ),
            export_confirm=_coerce_bool(d.get("export_confirm"), defaults.export_confirm),
            window_geometry=_coerce_geometry(d.get("window_geometry"), defaults.window_geometry),
            grid_visible=_coerce_bool(d.get("grid_visible"), defaults.grid_visible),
            snap_enabled=_coerce_bool(d.get("snap_enabled"), defaults.snap_enabled),
        )


def _coerce_bool(value: Any, default: bool) -> bool:
    return value if isinstance(value, bool) else default


def _coerce_int(value: Any, default: int) -> int:
    # bool は int のサブクラスなので先に弾く（True/False が紛れ込まないように）。
    if isinstance(value, bool):
        return default
    return value if isinstance(value, int) else default


def _coerce_float(value: Any, default: float) -> float:
    if isinstance(value, bool):
        return default
    if isinstance(value, (int, float)):
        return float(value)
    return default


def _coerce_str(value: Any, default: str) -> str:
    return value if isinstance(value, str) else default


def _coerce_optional_str(value: Any, default: str | None) -> str | None:
    if value is None:
        return None
    return value if isinstance(value, str) else default


def _coerce_geometry(value: Any, default: list[int] | None) -> list[int] | None:
    if value is None:
        return None
    if (
        isinstance(value, list)
        and len(value) == 4
        and all(isinstance(v, (int, float)) and not isinstance(v, bool) for v in value)
    ):
        return [int(v) for v in value]
    return default


def config_dir() -> Path:
    """設定ディレクトリ。`$CHARTA_CONFIG_DIR` > `$XDG_CONFIG_HOME/charta` > `~/.config/charta`。"""
    override = os.environ.get("CHARTA_CONFIG_DIR")
    if override:
        return Path(override)
    xdg_config_home = os.environ.get("XDG_CONFIG_HOME")
    if xdg_config_home:
        return Path(xdg_config_home) / "charta"
    return Path.home() / ".config" / "charta"


def prefs_path() -> Path:
    """`config_dir()/prefs.json`。"""
    return config_dir() / _PREFS_FILENAME


def load_prefs() -> Preferences:
    """`prefs_path()` から読み込む。

    ファイルが無ければ（初回起動）静かに既定値を返す。存在するが JSON として
    壊れている場合のみ `warnings.warn` を 1 回発してから既定値を返す
    （読めない設定を毎起動サイレントに握りつぶすと、ユーザーが原因に気付けない
    まま設定を失い続けるため）。
    """
    path = prefs_path()
    try:
        raw = path.read_text(encoding="utf-8")
    except OSError:
        return Preferences()

    try:
        data = json.loads(raw)
    except json.JSONDecodeError as exc:
        warnings.warn(
            f"charta: 環境設定ファイルが壊れています。既定値を使用します path={path}: {exc}",
            stacklevel=2,
        )
        return Preferences()

    if not isinstance(data, dict):
        warnings.warn(
            f"charta: 環境設定ファイルの形式が不正です。既定値を使用します path={path}",
            stacklevel=2,
        )
        return Preferences()

    return Preferences.from_dict(data)


def save_prefs(prefs: Preferences) -> None:
    """`prefs_path()` へアトミックに書き込む（tmp に書いて `os.replace`）。"""
    path = prefs_path()
    path.parent.mkdir(parents=True, exist_ok=True)

    fd, tmp_name = tempfile.mkstemp(
        dir=str(path.parent), prefix=f".{_PREFS_FILENAME}.", suffix=".tmp"
    )
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump(prefs.to_dict(), f, indent=2, ensure_ascii=False)
        os.replace(tmp_name, path)
    except BaseException:
        try:
            os.unlink(tmp_name)
        except OSError:
            pass
        raise
