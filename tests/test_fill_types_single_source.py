"""`FILL_TYPES`（塗り/線を持つ型: rect/ellipse/curve）が単一の真実源になっていることの回帰テスト。

2026-09-25 レビュー2巡目 finding #1/#7（area X）: `app.graphics.diagnostics._FILL_TYPES` と
`app.agent.diagnose._FILL_TYPES` が同じ集合を別々に定義しており、コメントの
「2 か所を必ず揃えること」という運用頼みの同期になっていた。`app.graphics.strokes.FILL_TYPES`
（Qt 非依存・`app.tools.tool_manager` からも import 済みの層）に一本化した。

2026-09-25 task3: 当時は `diagnostics._FILL_TYPES` を `tool_manager.py` が import
している旧名として残していたが、`tool_manager.py` 自身が `strokes.FILL_TYPES` の
直接 import へ移った（唯一の外部 importer だった）ため、`diagnostics.py` 側の
エイリアスは削除し `diagnostics.FILL_TYPES`（`strokes` からの直接 import）を
そのまま公開名として使う。本テストもそれに合わせて更新する。
"""

from __future__ import annotations

from app.agent import diagnose
from app.graphics import diagnostics
from app.graphics.strokes import FILL_TYPES


def test_diagnostics_and_diagnose_share_the_same_fill_types_object() -> None:
    assert diagnostics.FILL_TYPES is FILL_TYPES
    assert diagnose.FILL_TYPES is FILL_TYPES


def test_diagnostics_no_longer_carries_the_old_private_alias() -> None:
    """task3: `_FILL_TYPES` エイリアスは `tool_manager.py` が旧名を import して
    いたことだけが存在理由だった。`tool_manager.py` が `strokes.FILL_TYPES` の
    直接 import へ移った今、`diagnostics.py` 側にエイリアスを残す理由が無い。
    """
    assert not hasattr(diagnostics, "_FILL_TYPES")


def test_fill_types_is_rect_ellipse_curve() -> None:
    assert FILL_TYPES == frozenset({"rect", "ellipse", "curve"})
