"""環境設定（`app/prefs.py`）の永続化（Qt 不要）。

`Preferences` は project.json とは無関係の、プロジェクトを跨いで生きる設定
なので、実ユーザーの `~/.config/charta` を汚さないよう `_isolated_prefs`
（`tests/conftest.py`、autouse）が `CHARTA_CONFIG_DIR` を隔離している前提で書く。
"""

from __future__ import annotations

import json
import warnings
from pathlib import Path

import pytest

from app.prefs import Preferences, config_dir, load_prefs, prefs_path, save_prefs


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
