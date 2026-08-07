"""診断のスナップショット作成（Qt 依存の薄い層）。

判定そのものは `app/graphics/diagnostics.py`（Qt 非依存の純関数）にある。
ここは `Document` を読み、文字の実レイアウトを `QFontMetricsF` で採寸して
`DocumentSnapshot` に写す役だけを持つ。

**この 2 段構えにしてある理由**（安易に 1 枚にまとめないこと）:

1. **応答性**。スナップショットさえ作れば、解析は `Document` にも Qt にも
   触らないので、そのままワーカースレッドへ出せる（`critique(async_=True)`）。
   同じ関数の中で `Document` を読みながら計算していたら、GUI スレッドから
   出せず、大きな図で画面が固まる。
2. **遅延評価**。要求された検査に必要な値だけを採寸する。文字採寸は
   `QFontMetricsF` を通るので、コントラストだけ見たい呼び出しで全テキストを
   採寸するのは無駄。
3. **キャッシュ**。`Document.revision` が変わらない限り結果は変わらないので、
   直近 1 件を憶えておけば「render の warnings → critique」のような連続呼び出しが
   ただになる。
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from app.graphics import diagnostics
from app.graphics.routing import resolved_bounding_box

if TYPE_CHECKING:
    from app.model.document import Document
    from app.model.objects import BaseObject

CHECK_NAMES = diagnostics.CHECK_NAMES

#: 文字採寸が必要な検査。これ以外しか要求されていないなら `QFontMetricsF` を呼ばない。
_CHECKS_NEEDING_TEXT_METRICS = frozenset({"text_overflow"})

_FILL_TYPES = frozenset({"rect", "ellipse"})
_TEXTUAL_TYPES = frozenset({"text", "math"})


def _text_sizes(obj: BaseObject) -> tuple[tuple[float, float] | None, tuple[float, float] | None]:
    """text の実レイアウト寸法と内容にちょうど合う寸法を `QFontMetricsF` で採る。

    `TextItem._text_layout_rect` と**同じ経路**（`font_for` + 折返し付き
    `boundingRect`）を通す。式を写すと DPI ピン留め規則がずれて、シーンに
    出ている見た目と診断結果が食い違う。
    """
    from PySide6.QtCore import QRectF, Qt
    from PySide6.QtGui import QFontMetricsF

    from app.scene.items.text_item import ALIGN_MAP, default_text_size, font_for

    if obj.type != "text" or not obj.text:
        return (None, None)
    font = font_for(obj)
    metrics = QFontMetricsF(font)
    align = ALIGN_MAP.get(obj.align, Qt.AlignmentFlag.AlignLeft)
    flags = int(align) | int(Qt.AlignmentFlag.AlignTop) | int(Qt.TextFlag.TextWordWrap)
    rect = metrics.boundingRect(
        QRectF(0.0, 0.0, max(float(obj.width), 1.0), 1_000_000.0), flags, obj.text
    )
    return ((rect.width(), rect.height()), default_text_size(obj.text, font))


def _object_snapshot(
    document: Document, obj: BaseObject, z_index: int, *, measure_text: bool
) -> diagnostics.ObjectSnapshot:
    layout_size: tuple[float, float] | None = None
    natural_size: tuple[float, float] | None = None
    if measure_text and obj.type == "text":
        layout_size, natural_size = _text_sizes(obj)
    return diagnostics.ObjectSnapshot(
        id=obj.id,
        type=obj.type,
        name=obj.name,
        box=resolved_bounding_box(document, obj),
        rotation=float(getattr(obj, "rotation", 0.0)),
        opacity=float(obj.opacity),
        visible=bool(obj.visible),
        z_index=z_index,
        group_id=getattr(obj, "group_id", None),
        fill=getattr(obj, "fill", None) if obj.type in _FILL_TYPES else None,
        color=getattr(obj, "color", None) if obj.type in _TEXTUAL_TYPES else None,
        has_alpha=bool(getattr(obj, "has_alpha", False)),
        text=str(getattr(obj, "text", "")),
        font_size=float(getattr(obj, "font_size", 0.0)),
        text_layout_size=layout_size,
        text_natural_size=natural_size,
    )


def build_snapshot(
    document: Document, checks: tuple[str, ...] | None = None
) -> diagnostics.DocumentSnapshot:
    """`Document` から解析用スナップショットを作る（**GUI スレッドで呼ぶこと**）。

    `checks` に文字採寸を要する検査が無ければ `QFontMetricsF` を呼ばない。
    """
    enabled = set(CHECK_NAMES if checks is None else checks)
    measure_text = bool(enabled & _CHECKS_NEEDING_TEXT_METRICS)
    artboard = document.artboard
    return diagnostics.DocumentSnapshot(
        artboard=diagnostics.ArtboardSnapshot(
            width_px=float(artboard.width_px),
            height_px=float(artboard.height_px),
            width_mm=float(artboard.physical.width_mm),
            target_dpi=int(artboard.physical.target_dpi),
            background=artboard.background,
        ),
        objects=tuple(
            _object_snapshot(document, obj, index, measure_text=measure_text)
            for index, obj in enumerate(document.objects)
        ),
        doc_uid=document.uid,
        revision=document.revision,
    )


#: 直近 1 件の結果キャッシュ。キーは (doc_uid, revision, checks)。
#: `revision` はモデル変更のたびに進むので、古い結果が返ることはない。
_cache: tuple[tuple[Any, ...], list[dict[str, Any]]] | None = None


def collect(
    document: Document,
    checks: tuple[str, ...] | None = None,
    ids: tuple[int, ...] | None = None,
) -> list[dict[str, Any]]:
    """診断を実行して所見の一覧を返す（GUI スレッド用の同期経路）。

    同じ `revision` に対する同じ検査の 2 度目以降はキャッシュを返す
    （`render(include=["warnings"])` の直後に `critique` を呼ぶ、という
    エージェントの自然な使い方で再計算しないため）。
    """
    global _cache
    key = (document.uid, document.revision, tuple(sorted(checks)) if checks else None)
    if _cache is not None and _cache[0] == key:
        findings = _cache[1]
    else:
        findings = diagnostics.analyze(build_snapshot(document, checks), checks)
        _cache = (key, findings)
    if ids is None:
        return list(findings)
    wanted = set(ids)
    return [f for f in findings if wanted & diagnostics._referenced_ids(f)]


def invalidate_cache() -> None:
    """キャッシュを捨てる（テストと、ドキュメント差し替え時のため）。"""
    global _cache
    _cache = None
