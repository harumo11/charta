"""ビルトインのカラーパレット（Qt 非依存・model 層）。

環境設定（`app/prefs.py` の `Preferences.palette_id`）で選んだパレットを、
(a) 色スウォッチ（`ColorSwatchButton`）のドロップダウン項目と `SimpleColorDialog`
のパレット行、(b) 新規オブジェクトの初期色、(c) プロジェクトの named styles
（`document.styles`、`app/model/styles.py`）の 3 箇所へ反映するための語彙をここに置く。

パレットの hex 値はユーザー承認済みの契約値をそのまま逐語使用する（丸めない・
並べ替えない）。
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class Palette:
    """1 つのビルトインカラーパレット。"""

    id: str  # "material" 等（snake_case、prefs.palette_id と対応）
    name: str  # 表示名（日本語可）
    note: str  # 一行説明（設定ダイアログに表示）
    colors: tuple[str, ...]  # "#RRGGBB" × 8


PALETTES: tuple[Palette, ...] = (
    Palette(
        id="material",
        name="Material Design",
        note="Google Material Design の 500 番台。彩度高めで UI・スライド向け",
        colors=(
            "#F44336",
            "#FF9800",
            "#FFC107",
            "#4CAF50",
            "#009688",
            "#2196F3",
            "#3F51B5",
            "#9C27B0",
        ),
    ),
    Palette(
        id="apple",
        name="Apple システムカラー",
        note="iOS/macOS のシステムカラー（実測値）。明るく現代的",
        colors=(
            "#FF3B30",
            "#FF9500",
            "#FFCC00",
            "#34C759",
            "#30B0C7",
            "#007AFF",
            "#5856D6",
            "#AF52DE",
        ),
    ),
    Palette(
        id="seaborn_deep",
        name="seaborn deep",
        note="Python 可視化の定番。彩度控えめで論文図向け",
        colors=(
            "#4C72B0",
            "#DD8452",
            "#55A868",
            "#C44E52",
            "#8172B3",
            "#937860",
            "#DA8BC3",
            "#8C8C8C",
        ),
    ),
    Palette(
        id="tableau10",
        name="Tableau 10",
        note="Tableau の既定。バランス型の現代的定番",
        colors=(
            "#4E79A7",
            "#F28E2B",
            "#E15759",
            "#76B7B2",
            "#59A14F",
            "#EDC948",
            "#B07AA1",
            "#9C755F",
        ),
    ),
    Palette(
        id="okabe_ito",
        name="Okabe-Ito（カラーユニバーサル）",
        note="色覚多様性に配慮した科学図の標準",
        colors=(
            "#E69F00",
            "#56B4E9",
            "#009E73",
            "#F0E442",
            "#0072B2",
            "#D55E00",
            "#CC79A7",
            "#000000",
        ),
    ),
    Palette(
        id="kusumi",
        name="くすみカラー",
        note="低彩度・中明度で統一した柔らかいトーン",
        colors=(
            "#C89EA3",
            "#C68B77",
            "#C9A86A",
            "#9CAF88",
            "#8FA8BF",
            "#A79AB2",
            "#B5A79A",
            "#6E6A67",
        ),
    ),
)


def palette_by_id(palette_id: str) -> Palette | None:
    """`palette_id` に対応する `Palette`。見つからなければ `None`。"""
    for palette in PALETTES:
        if palette.id == palette_id:
            return palette
    return None


def palette_style_bundles(palette: Palette) -> dict[str, dict[str, str]]:
    """named styles 登録用の束を作る（`document.styles` にマージする形）。

    `{"<palette.id>-1": {"fill": c, "stroke": c, "color": c}, ...}` を 1 始まりで
    パレットの色数ぶん返す。`fill`/`stroke` は図形（rect/ellipse/line/arrow）用、
    `color` は text/math 用のキー（`app/model/styles.py` の text/math 非対称の説明を
    参照）。`apply_style` は対象オブジェクトの型が持たないキーを黙って `skipped` に
    するので、同じ束を図形・文字混在の選択に配ってもエラーにならない。
    """
    bundles: dict[str, dict[str, str]] = {}
    for i, color in enumerate(palette.colors, start=1):
        bundles[f"{palette.id}-{i}"] = {"fill": color, "stroke": color, "color": color}
    return bundles


# 色ドロップダウン（要望8/11）の「基本色」8色。パレットが未選択（既定 palette_id=""）
# のときのフォールバックにも使う。「ほんとに基本の数種類」というユーザー要望どおり、
# 黒・灰・薄灰（新既定の図形塗り色）・白・赤・黄・緑・青の8色に絞る。
BASIC_COLORS: tuple[str, ...] = (
    "#000000",
    "#808080",
    "#D9D9D9",
    "#FFFFFF",
    "#FF0000",
    "#FFFF00",
    "#00B050",
    "#0070C0",
)


def dropdown_colors(palette: Palette | None) -> tuple[str, ...]:
    """色ドロップダウン（`ColorSwatchButton` のメニュー）に出す色の並びを返す。

    パレットの8色（`palette` が `None` なら `BASIC_COLORS`）の後ろに、黒・白を
    大文字小文字を無視して既出でなければ追加する（線色が黒・塗りが白という
    定番が、どのパレットを選んでいても常に選べるようにするため）。すべて
    大文字 hex で返す。
    """
    base = palette.colors if palette is not None else BASIC_COLORS
    result = [c.upper() for c in base]
    seen = {c.upper() for c in result}
    for extra in ("#000000", "#FFFFFF"):
        if extra not in seen:
            result.append(extra)
            seen.add(extra)
    return tuple(result)
