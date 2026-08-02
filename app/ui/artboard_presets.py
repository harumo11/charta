"""アートボード物理サイズプリセットのデータ定義（Qt 非依存、P2契約 §2）。

旧 `app/ui/artboard_dialog.py`（P2 でファイル削除）から `ARTBOARD_PRESETS`/
`ARTBOARD_CUSTOM_LABEL`/`preset_px_size` を移設し、プリセットを7件に拡張した。
`PropertyPanel` の artboard モード（未選択時のアートボード設定フォーム）が使う。
"""

from __future__ import annotations

# アートボードプリセット(§9): (表示名, width_mm, target_dpi, アスペクト比w, アスペクト比h)。
ARTBOARD_PRESETS: list[tuple[str, float, int, int, int]] = [
    ("1カラム figure (84mm, 300dpi, 4:3)", 84.0, 300, 4, 3),
    ("2カラム figure (174mm, 300dpi, 4:3)", 174.0, 300, 4, 3),
    ("Nature 単段 (89mm, 300dpi, 4:3)", 89.0, 300, 4, 3),
    ("Nature 1.5段 (120mm, 300dpi, 4:3)", 120.0, 300, 4, 3),
    ("Nature 1.5段広 (136mm, 300dpi, 4:3)", 136.0, 300, 4, 3),
    ("Nature 2段 (183mm, 300dpi, 4:3)", 183.0, 300, 4, 3),
    ("スライド 16:9 (250mm相当, 150dpi)", 250.0, 150, 16, 9),
]
ARTBOARD_CUSTOM_LABEL = "カスタム"


def preset_px_size(
    width_mm: float, target_dpi: int, aspect_w: int, aspect_h: int
) -> tuple[int, int]:
    """物理サイズプリセットから width_px/height_px を算出する。"""
    width_px = round(width_mm / 25.4 * target_dpi)
    height_px = round(width_px * aspect_h / aspect_w)
    return max(width_px, 1), max(height_px, 1)
