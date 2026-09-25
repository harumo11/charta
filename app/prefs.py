"""アプリ全体の環境設定（プロジェクト非依存・JSON 永続化）。

保存先は `$CHARTA_CONFIG_DIR` > `$XDG_CONFIG_HOME/charta` > `~/.config/charta` の
`prefs.json`。壊れたファイル・未知キー・欠落キーに寛容（例外を外に出さない）。
document/project.json（`app/model/document.py`・`app/model/serialize.py`）とは
無関係——こちらはユーザー環境そのものの設定であり、プロジェクトを跨いで生きる。

Qt 非依存（`app/prefs.py` は PySide6 を import しない）。呼び出し側
（`app/ui/main_window.py`）が `Preferences` を読み書きし、`palette_by_id(palette_id)` を
`PropertyPanel`/`MaskEditPanel`/環境設定ダイアログの `ColorSwatchButton.set_palette`
（ドロップダウン項目）と `SimpleColorDialog`（パレット行）へ渡す
（2026-09-25: 独自の色ダイアログ導入に伴い、Qt 標準の色ダイアログは廃止済み）。
"""

from __future__ import annotations

import json
import math
import os
import re
import tempfile
import warnings
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any

#: prefs.json のファイル名。config_dir() の直下に置く。
_PREFS_FILENAME = "prefs.json"

#: 値域クランプの定数（`app/ui/prefs_dialog.py` のスピンボックス range と同じ値を
#: ここに一本化し、ずれないようにする。所見: from_dict が値域検証を持たず、
#: 手編集/破損した prefs.json の値がそのまま Qt の C++ 側に渡って起動不能になる）。
ARTBOARD_PX_MIN = 1.0
ARTBOARD_PX_MAX = 20000.0
ARTBOARD_MM_MIN = 1.0
ARTBOARD_MM_MAX = 2000.0
ARTBOARD_DPI_MIN = 72
ARTBOARD_DPI_MAX = 1200
FONT_SIZE_MIN = 6.0
FONT_SIZE_MAX = 128.0
STROKE_WIDTH_MIN = 0.0
STROKE_WIDTH_MAX = 50.0
AUTOSAVE_INTERVAL_MIN = 0
AUTOSAVE_INTERVAL_MAX = 600
CONNECTOR_ROUTING_VALUES = ("orthogonal", "straight")
#: `app/math/mathtext_render.py` の `MATH_FONTSETS` と同じ集合（値を変えるときは
#: 両方直すこと。`app/prefs.py` は app 配下を一切 import しない独立モジュールなので
#: 定数は共有できない）。不正名は matplotlib が `ValueError` を投げ `MathRenderError`
#: に化けて**全数式がプレースホルダになる**ため、ここのホワイトリストがその防波堤。
MATH_FONTSET_VALUES = ("cm", "stix", "stixsans", "dejavusans", "dejavuserif")
_HEX_COLOR_RE = re.compile(r"^#[0-9A-Fa-f]{6}$")


def _clamp_float(value: float, lo: float, hi: float) -> float:
    return max(lo, min(hi, value))


def _clamp_int(value: int, lo: int, hi: int) -> int:
    return max(lo, min(hi, value))


def _is_hex_color(value: Any) -> bool:
    return isinstance(value, str) and bool(_HEX_COLOR_RE.match(value))


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
    # 新規図形の線色/文字色。None = このフィールドで上書きしない(dataclass 既定のまま)。
    # 2026-09-25 決定で rect/ellipse の stroke 既定が None(線なし)になったため、
    # rect/ellipse には効かない(「なし」が既定のフィールドは初期色で埋めない。
    # `ToolManager._apply_pref_defaults`)。line/arrow/freehand/connector/curve の stroke
    # （curve の stroke 既定は "#000000" で None ではないため対象）と
    # text/math の文字色には従来どおり効く。
    initial_color: str | None = None
    # 新規オブジェクトの既定
    default_font_family: str = "Noto Sans CJK JP"
    default_font_size: float = 18.0
    default_stroke_width: float = 2.0
    default_connector_routing: str = "orthogonal"  # "orthogonal" | "straight"
    # 数式フォント（項目6-wiring契約）。`app.math.mathtext_render.current_math_fontset()`
    # のプロセス全体の現在値を起動時/環境設定確定時にここから初期化する。新規オブジェクトの
    # 既定と同じ節に置くが、実際には既存の math オブジェクトの見た目にも即時反映される
    # （`MainWindow._refresh_math_rendering`）点が他の「新規作成の既定」フィールドと異なる。
    math_fontset: str = "cm"
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
    # クリップボードコピーの透過（項目7契約）。`export_transparent_png` とは**別フィールド**
    # （貼り付け先の PowerPoint は不透過が欲しい／ファイル書き出しは透過が欲しい、が普通に
    # 併存するため）。設定ダイアログには出さず、ヘッダーバーのコピーボタンのドロップダウン
    # メニュー（「透過背景でコピー」チェック項目）から直接切り替える。
    copy_transparent: bool = False
    # 自動記憶（設定ダイアログには出さない）
    window_geometry: list[int] | None = None  # [x, y, w, h]
    grid_visible: bool = False
    snap_enabled: bool = True

    def to_dict(self) -> dict[str, Any]:
        """`dataclasses.asdict` そのまま（全フィールドが JSON 化可能な素朴な型のため）。"""
        return asdict(self)

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> Preferences:
        """未知キー無視・欠落は既定値・型が合わない値は既定値に落とす（例外を出さない）。

        型が合っていても値域外・無限大/NaN・列挙外・色として不正な文字列は
        本体（Qt の C++ 層）を壊し得るため、ここでクランプ/ホワイトリスト
        検証まで行う（所見: from_dict は型しか見ておらず、手編集・破損・
        旧/他機の prefs.json で起動不能になっていた）。クランプ幅は
        `app/ui/prefs_dialog.py` のスピンボックス range と同じ値をこのモジュール
        の定数として共有する。
        """
        defaults = cls()

        default_connector_routing = _coerce_str(
            d.get("default_connector_routing"), defaults.default_connector_routing
        )
        if default_connector_routing not in CONNECTOR_ROUTING_VALUES:
            default_connector_routing = defaults.default_connector_routing

        math_fontset = _coerce_str(d.get("math_fontset"), defaults.math_fontset)
        if math_fontset not in MATH_FONTSET_VALUES:
            math_fontset = defaults.math_fontset

        artboard_background = _coerce_str(
            d.get("artboard_background"), defaults.artboard_background
        )
        if not _is_hex_color(artboard_background):
            artboard_background = defaults.artboard_background

        initial_color = _coerce_optional_str(d.get("initial_color"), defaults.initial_color)
        if initial_color is not None and not _is_hex_color(initial_color):
            initial_color = None

        return cls(
            version=_coerce_int(d.get("version"), defaults.version),
            palette_id=_coerce_str(d.get("palette_id"), defaults.palette_id),
            initial_color=initial_color,
            default_font_family=_coerce_str(
                d.get("default_font_family"), defaults.default_font_family
            ),
            default_font_size=_clamp_float(
                _coerce_float(d.get("default_font_size"), defaults.default_font_size),
                FONT_SIZE_MIN,
                FONT_SIZE_MAX,
            ),
            default_stroke_width=_clamp_float(
                _coerce_float(d.get("default_stroke_width"), defaults.default_stroke_width),
                STROKE_WIDTH_MIN,
                STROKE_WIDTH_MAX,
            ),
            default_connector_routing=default_connector_routing,
            math_fontset=math_fontset,
            artboard_width_px=_clamp_float(
                _coerce_float(d.get("artboard_width_px"), defaults.artboard_width_px),
                ARTBOARD_PX_MIN,
                ARTBOARD_PX_MAX,
            ),
            artboard_height_px=_clamp_float(
                _coerce_float(d.get("artboard_height_px"), defaults.artboard_height_px),
                ARTBOARD_PX_MIN,
                ARTBOARD_PX_MAX,
            ),
            artboard_width_mm=_clamp_float(
                _coerce_float(d.get("artboard_width_mm"), defaults.artboard_width_mm),
                ARTBOARD_MM_MIN,
                ARTBOARD_MM_MAX,
            ),
            artboard_dpi=_clamp_int(
                _coerce_int(d.get("artboard_dpi"), defaults.artboard_dpi),
                ARTBOARD_DPI_MIN,
                ARTBOARD_DPI_MAX,
            ),
            artboard_background=artboard_background,
            autosave_interval_s=_clamp_int(
                _coerce_int(d.get("autosave_interval_s"), defaults.autosave_interval_s),
                AUTOSAVE_INTERVAL_MIN,
                AUTOSAVE_INTERVAL_MAX,
            ),
            export_outline_text=_coerce_bool(
                d.get("export_outline_text"), defaults.export_outline_text
            ),
            export_transparent_png=_coerce_bool(
                d.get("export_transparent_png"), defaults.export_transparent_png
            ),
            export_confirm=_coerce_bool(d.get("export_confirm"), defaults.export_confirm),
            copy_transparent=_coerce_bool(d.get("copy_transparent"), defaults.copy_transparent),
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
        result = float(value)
        # JSON の `Infinity`/`NaN`（`json.loads` は受理する）が本体側の
        # `int(round(...))`/Qt の C++ setter で OverflowError を引き起こすため、
        # ここで弾く（所見: 起動不能になる実測ケース）。
        if not math.isfinite(result):
            return default
        return result
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

    ファイルが無ければ（初回起動）静かに既定値を返す。存在するが読めない
    （JSON 破損・非 UTF-8・権限エラー等）場合は `warnings.warn` を 1 回発してから
    既定値を返す（読めない設定を毎起動サイレントに握りつぶすと、ユーザーが
    原因に気付けないまま設定を失い続けるため）。無音にしてよいのは
    「ファイルが無い」＝初回起動のときだけで、それ以外（`PermissionError` 等の
    `OSError` や非 UTF-8 バイト列による `UnicodeDecodeError`）は同じ警告を出す
    （所見: 従来は `except OSError` のみで、非 UTF-8 は捕捉されず例外が漏れ、
    権限エラーは初回起動と区別できず無警告だった）。
    """
    path = prefs_path()
    try:
        raw = path.read_text(encoding="utf-8")
    except FileNotFoundError:
        return Preferences()
    except (OSError, UnicodeDecodeError) as exc:
        warnings.warn(
            f"charta: 環境設定ファイルを読み込めません。既定値を使用します path={path}: {exc}",
            stacklevel=2,
        )
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
    """`prefs_path()` へアトミックに書き込む（tmp に書いて `os.replace`）。

    書き込み不能（ディスクフル・`CHARTA_CONFIG_DIR` が書けない・権限エラー等）
    でも例外を外に出さない——`warnings.warn` を 1 回発するだけにする
    （`load_prefs` と対称。所見: 従来は `OSError` を再送出しており、
    `MainWindow.closeEvent`/グリッド・スナップのトグルハンドラ等、呼び出し側が
    無防備な箇所すべてに例外が漏れ、`closeEvent` では `super().closeEvent()`
    より前に例外が飛ぶため終了処理の残りが実行されなかった）。
    書いた内容は `os.fsync` してから `os.replace` する（電源断で 0 バイトの
    ファイルが残ることを避けるため）。
    """
    path = prefs_path()
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        fd, tmp_name = tempfile.mkstemp(
            dir=str(path.parent), prefix=f".{_PREFS_FILENAME}.", suffix=".tmp"
        )
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as f:
                json.dump(prefs.to_dict(), f, indent=2, ensure_ascii=False)
                f.flush()
                os.fsync(f.fileno())
            os.replace(tmp_name, path)
        except BaseException:
            try:
                os.unlink(tmp_name)
            except OSError:
                pass
            raise
    except OSError as exc:
        warnings.warn(
            f"charta: 環境設定ファイルを保存できません path={path}: {exc}",
            stacklevel=2,
        )


def update_prefs(**changes: Any) -> Preferences:
    """ディスク上の prefs を読み直し、`changes` のキーだけ上書きして保存する。

    `save_prefs` はインスタンスが持つ `Preferences` を丸ごと書き戻すため、
    複数プロセス（GUI + `--no-agent-server` 無しのヘッドレス常駐等、§15）が
    同時に起動していると、片方の保存がもう片方の変更を後勝ちで消してしまう
    （所見）。ウィンドウジオメトリ/グリッド/スナップの自動記憶や、環境設定
    ダイアログの確定など「このフィールドだけ確実に反映したい」呼び出しは、
    `self.prefs` を直接 `save_prefs` するのではなくこちらを使うことで、
    他インスタンスが書いた無関係なフィールドを保存のたびに巻き戻さない。
    戻り値は保存後の（ディスクの最新値をベースにした）`Preferences`。
    """
    current = load_prefs()
    for key, value in changes.items():
        setattr(current, key, value)
    save_prefs(current)
    return current
