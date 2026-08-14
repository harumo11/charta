"""環境設定（`app/prefs.py`）の永続化（Qt 不要）。

`Preferences` は project.json とは無関係の、プロジェクトを跨いで生きる設定
なので、実ユーザーの `~/.config/charta` を汚さないよう `_isolated_prefs`
（`tests/conftest.py`、autouse）が `CHARTA_CONFIG_DIR` を隔離している前提で書く。
"""

from __future__ import annotations

import json
import math
import os
import warnings
from pathlib import Path

import pytest

from app.prefs import (
    ARTBOARD_DPI_MAX,
    ARTBOARD_DPI_MIN,
    ARTBOARD_PX_MAX,
    ARTBOARD_PX_MIN,
    AUTOSAVE_INTERVAL_MAX,
    FONT_SIZE_MAX,
    Preferences,
    config_dir,
    load_prefs,
    prefs_path,
    save_prefs,
    update_prefs,
)


def test_config_dir_respects_charta_config_dir_env(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    override = tmp_path / "my-config"
    monkeypatch.setenv("CHARTA_CONFIG_DIR", str(override))
    assert config_dir() == override
    assert prefs_path() == override / "prefs.json"


def test_load_prefs_without_file_returns_defaults_silently() -> None:
    """初回起動（設定ファイルが無い）は警告なしで既定値を返す。"""
    assert not prefs_path().exists()
    with warnings.catch_warnings():
        warnings.simplefilter("error")
        prefs = load_prefs()
    assert prefs == Preferences()


def test_save_then_load_roundtrips_all_fields() -> None:
    original = Preferences(
        palette_id="material",
        initial_color="#FF0000",
        default_font_family="Test Font",
        default_font_size=24.0,
        default_stroke_width=3.5,
        default_connector_routing="straight",
        artboard_width_px=800.0,
        artboard_height_px=600.0,
        artboard_width_mm=85.0,
        artboard_dpi=600,
        artboard_background="#000000",
        autosave_interval_s=0,
        export_outline_text=True,
        export_transparent_png=True,
        export_confirm=False,
        window_geometry=[10, 20, 640, 480],
        grid_visible=True,
        snap_enabled=False,
    )
    save_prefs(original)
    assert prefs_path().exists()

    loaded = load_prefs()
    assert loaded == original


def test_to_dict_from_dict_roundtrip_in_memory() -> None:
    prefs = Preferences(palette_id="okabe_ito", initial_color=None, window_geometry=None)
    assert Preferences.from_dict(prefs.to_dict()) == prefs


def test_save_prefs_creates_parent_directory(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    nested = tmp_path / "does" / "not" / "exist" / "yet"
    monkeypatch.setenv("CHARTA_CONFIG_DIR", str(nested))
    assert not nested.exists()
    save_prefs(Preferences())
    assert (nested / "prefs.json").exists()


def test_load_prefs_with_broken_json_warns_once_and_returns_defaults() -> None:
    path = prefs_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("{not valid json", encoding="utf-8")

    with warnings.catch_warnings(record=True) as caught:
        warnings.simplefilter("always")
        prefs = load_prefs()

    assert prefs == Preferences()
    assert len(caught) == 1


def test_load_prefs_with_non_dict_json_warns_once_and_returns_defaults() -> None:
    path = prefs_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps([1, 2, 3]), encoding="utf-8")

    with warnings.catch_warnings(record=True) as caught:
        warnings.simplefilter("always")
        prefs = load_prefs()

    assert prefs == Preferences()
    assert len(caught) == 1


def test_from_dict_ignores_unknown_keys() -> None:
    d = Preferences().to_dict()
    d["totally_unknown_future_key"] = "surprise"
    prefs = Preferences.from_dict(d)
    assert prefs == Preferences()


def test_from_dict_fills_missing_keys_with_defaults() -> None:
    prefs = Preferences.from_dict({"palette_id": "apple"})
    assert prefs.palette_id == "apple"
    assert prefs.default_font_family == Preferences().default_font_family
    assert prefs.window_geometry is None


def test_from_dict_falls_back_to_default_on_type_mismatch() -> None:
    d = {
        "artboard_dpi": "not-an-int",
        "default_font_size": "not-a-float",
        "export_confirm": "not-a-bool",
        "palette_id": 12345,
        "window_geometry": "not-a-list",
        "initial_color": 999,
    }
    prefs = Preferences.from_dict(d)
    defaults = Preferences()
    assert prefs.artboard_dpi == defaults.artboard_dpi
    assert prefs.default_font_size == defaults.default_font_size
    assert prefs.export_confirm == defaults.export_confirm
    assert prefs.palette_id == defaults.palette_id
    assert prefs.window_geometry is None
    assert prefs.initial_color == defaults.initial_color


def test_from_dict_rejects_bool_for_int_and_float_fields() -> None:
    """`isinstance(True, int)` が真になる罠を踏んでいないことの回帰。"""
    prefs = Preferences.from_dict({"artboard_dpi": True, "default_font_size": False})
    defaults = Preferences()
    assert prefs.artboard_dpi == defaults.artboard_dpi
    assert prefs.default_font_size == defaults.default_font_size


def test_from_dict_accepts_none_for_optional_fields() -> None:
    prefs = Preferences.from_dict({"initial_color": None, "window_geometry": None})
    assert prefs.initial_color is None
    assert prefs.window_geometry is None


# --------------------------------------------------------------------------
# 値域クランプ・ホワイトリスト（所見: from_dict は型しか見ておらず、手編集/破損/
# 旧・他機の prefs.json で起動不能になっていた）
# --------------------------------------------------------------------------


def test_from_dict_rejects_infinity_and_nan_floats() -> None:
    """`json.loads` は `Infinity`/`NaN` を受理するため、ここで弾かないと
    `MainWindow` 側の `int(round(...))` が `OverflowError` を投げて起動不能になる。
    """
    d = {
        "artboard_width_px": math.inf,
        "artboard_height_px": -math.inf,
        "default_font_size": math.nan,
    }
    prefs = Preferences.from_dict(d)
    defaults = Preferences()
    assert prefs.artboard_width_px == defaults.artboard_width_px
    assert prefs.artboard_height_px == defaults.artboard_height_px
    assert prefs.default_font_size == defaults.default_font_size
    assert math.isfinite(prefs.artboard_width_px)


def test_from_dict_clamps_out_of_range_numeric_fields() -> None:
    d = {
        "artboard_width_px": -5.0,
        "artboard_height_px": ARTBOARD_PX_MAX + 100000.0,
        "artboard_dpi": 0,
        "default_font_size": 99999.0,
        "default_stroke_width": -1.0,
        # 実測で起動不能になった巨大値（shiboken の int 範囲を超える）。
        "autosave_interval_s": 10_000_000_000_000,
    }
    prefs = Preferences.from_dict(d)
    assert prefs.artboard_width_px == ARTBOARD_PX_MIN
    assert prefs.artboard_height_px == ARTBOARD_PX_MAX
    assert ARTBOARD_DPI_MIN <= prefs.artboard_dpi <= ARTBOARD_DPI_MAX
    assert prefs.artboard_dpi != 0
    assert prefs.default_font_size == FONT_SIZE_MAX
    assert prefs.default_stroke_width == 0.0
    assert prefs.autosave_interval_s == AUTOSAVE_INTERVAL_MAX


def test_from_dict_rejects_unknown_connector_routing() -> None:
    prefs = Preferences.from_dict({"default_connector_routing": "diagonal"})
    assert prefs.default_connector_routing == Preferences().default_connector_routing


def test_from_dict_rejects_invalid_colors() -> None:
    d = {
        "artboard_background": "not-a-color",
        "initial_color": "also-not-a-color",
    }
    prefs = Preferences.from_dict(d)
    assert prefs.artboard_background == Preferences().artboard_background
    assert prefs.initial_color is None


def test_from_dict_accepts_valid_hex_colors() -> None:
    prefs = Preferences.from_dict({"artboard_background": "#abcdef", "initial_color": "#123ABC"})
    assert prefs.artboard_background == "#abcdef"
    assert prefs.initial_color == "#123ABC"


# --------------------------------------------------------------------------
# load_prefs: FileNotFoundError のみ無音、それ以外(非UTF-8/権限)は warn（所見）
# --------------------------------------------------------------------------


def test_load_prefs_with_non_utf8_bytes_warns_and_returns_defaults() -> None:
    """`read_text(encoding='utf-8')` の `UnicodeDecodeError` は従来 `OSError` の
    `except` で捕捉されず、外へ漏れて `MainWindow` の起動が止まっていた。
    """
    path = prefs_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(b"\xff\xfe\x00\x01broken")

    with warnings.catch_warnings(record=True) as caught:
        warnings.simplefilter("always")
        prefs = load_prefs()

    assert prefs == Preferences()
    assert len(caught) == 1


def test_load_prefs_with_unreadable_file_warns_instead_of_silently_defaulting() -> None:
    """ファイルが存在するのに読めない(権限エラー等)場合は、初回起動と区別して警告する。"""
    path = prefs_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(Preferences().to_dict()), encoding="utf-8")
    path.chmod(0o000)
    try:
        if os.access(path, os.R_OK):
            pytest.skip("root 実行等で権限エラーを再現できない環境")
        with warnings.catch_warnings(record=True) as caught:
            warnings.simplefilter("always")
            prefs = load_prefs()
        assert prefs == Preferences()
        assert len(caught) == 1
    finally:
        path.chmod(0o644)


# --------------------------------------------------------------------------
# save_prefs: 書き込み失敗でも例外を出さない（所見。closeEvent/トグルハンドラ等
# 無防備な呼び出し口すべてを一度に守るため、save_prefs 自体を非送出にする）
# --------------------------------------------------------------------------


def test_save_prefs_does_not_raise_when_config_dir_is_unwritable(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    unwritable_parent = tmp_path / "readonly"
    unwritable_parent.mkdir()
    unwritable_parent.chmod(0o500)
    monkeypatch.setenv("CHARTA_CONFIG_DIR", str(unwritable_parent / "charta"))
    try:
        if os.access(unwritable_parent, os.W_OK):
            pytest.skip("root 実行等で書き込み不能を再現できない環境")
        with warnings.catch_warnings(record=True) as caught:
            warnings.simplefilter("always")
            save_prefs(Preferences())  # 例外を出さないこと自体がアサーション
        assert len(caught) == 1
    finally:
        unwritable_parent.chmod(0o700)


# --------------------------------------------------------------------------
# update_prefs: フィールド単位のマージ保存（所見: save_prefs の全体上書きは
# 複数プロセス間で後勝ちにより無関係なフィールドを消してしまう）
# --------------------------------------------------------------------------


def test_update_prefs_merges_single_field_without_clobbering_others() -> None:
    save_prefs(Preferences(palette_id="material", artboard_dpi=600))

    # 別プロセス相当: 古いスナップショットに基づいて grid_visible だけ変えたい。
    update_prefs(grid_visible=True)

    loaded = load_prefs()
    assert loaded.grid_visible is True
    assert loaded.palette_id == "material"
    assert loaded.artboard_dpi == 600


def test_update_prefs_without_existing_file_starts_from_defaults() -> None:
    assert not prefs_path().exists()
    update_prefs(snap_enabled=False)
    loaded = load_prefs()
    assert loaded.snap_enabled is False
    assert loaded.palette_id == Preferences().palette_id
