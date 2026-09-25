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

import dataclasses
import math
from typing import TYPE_CHECKING, Any

from app.graphics import diagnostics
from app.graphics.routing import resolved_bounding_box
from app.graphics.strokes import FILL_TYPES

if TYPE_CHECKING:
    from app.model.document import Document
    from app.model.objects import BaseObject

CHECK_NAMES = diagnostics.CHECK_NAMES

#: 文字採寸が必要な検査。これ以外しか要求されていないなら `QFontMetricsF` を呼ばない。
_CHECKS_NEEDING_TEXT_METRICS = frozenset({"text_overflow"})

_TEXTUAL_TYPES = frozenset({"text", "math"})
#: `update_objects` で width/height を直接書ける型（幾何が box のもの）。
#: line/arrow は p1/p2、connector はアンカーが真実源なので縮められない。
_RESIZABLE_TYPES = frozenset({"rect", "ellipse", "image", "text", "math", "freehand", "curve"})


def _text_sizes(obj: BaseObject) -> tuple[tuple[float, float] | None, tuple[float, float] | None]:
    """text の実レイアウト寸法と内容にちょうど合う寸法を採る。

    `TextItem._text_layout_rect` と**同じ経路**（`font_for` +
    `text_outline.measure_text` の QTextLayout エンジン）を通す。式を写すと
    DPI ピン留め規則や折返しモードがずれて、シーンに出ている見た目と
    診断結果が食い違う。
    """
    from app.export.text_outline import measure_text
    from app.scene.items.text_item import default_text_size, font_for

    if obj.type != "text" or not obj.text:
        return (None, None)
    font = font_for(obj)
    layout_size = measure_text(obj.text, font, max(float(obj.width), 1.0))
    return (layout_size, default_text_size(obj.text, font))


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
        fill=getattr(obj, "fill", None) if obj.type in FILL_TYPES else None,
        stroke=getattr(obj, "stroke", None) if obj.type in FILL_TYPES else None,
        stroke_width=(float(getattr(obj, "stroke_width", 0.0)) if obj.type in FILL_TYPES else 0.0),
        color=getattr(obj, "color", None) if obj.type in _TEXTUAL_TYPES else None,
        background=getattr(obj, "background", None) if obj.type == "text" else None,
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
_cache: tuple[tuple[Any, ...], list[dict[str, Any]], diagnostics.DocumentSnapshot] | None = None


def collect_detailed(
    document: Document,
    checks: tuple[str, ...] | None = None,
    ids: tuple[int, ...] | None = None,
) -> tuple[list[dict[str, Any]], diagnostics.DocumentSnapshot]:
    """所見とスナップショットを返す（GUI スレッド用の同期経路）。

    同じ `revision` に対する同じ検査の 2 度目以降はキャッシュを返す
    （`render(include=["warnings"])` の直後に `critique` を呼ぶ、という
    エージェントの自然な使い方で再計算しないため）。
    """
    global _cache
    key = (document.uid, document.revision, tuple(sorted(checks)) if checks else None)
    if _cache is not None and _cache[0] == key:
        _key, findings, snapshot = _cache
    else:
        snapshot = build_snapshot(document, checks)
        findings = diagnostics.analyze(snapshot, checks)
        _cache = (key, findings, snapshot)
    return (filter_by_ids(findings, ids), snapshot)


def collect(
    document: Document,
    checks: tuple[str, ...] | None = None,
    ids: tuple[int, ...] | None = None,
) -> list[dict[str, Any]]:
    """診断を実行して所見の一覧を返す。"""
    return collect_detailed(document, checks, ids)[0]


def filter_by_ids(
    findings: list[dict[str, Any]], ids: tuple[int, ...] | None
) -> list[dict[str, Any]]:
    if ids is None:
        return list(findings)
    wanted = set(ids)
    return [f for f in findings if wanted & diagnostics.referenced_ids(f)]


def invalidate_cache() -> None:
    """キャッシュを捨てる（テストと、ドキュメント差し替え時のため）。"""
    global _cache
    _cache = None


# --------------------------------------------------------------------------
# 修正案（そのまま送れる corrected_call）
# --------------------------------------------------------------------------


def _object(snapshot: diagnostics.DocumentSnapshot, oid: Any) -> diagnostics.ObjectSnapshot | None:
    return next((o for o in snapshot.objects if o.id == oid), None)


def suggest_fix(
    finding: dict[str, Any], snapshot: diagnostics.DocumentSnapshot
) -> dict[str, Any] | None:
    """所見 1 件に対する `{tool, arguments, note}`。作れなければ None。

    ここで返すものは**そのまま送れる呼び出し**であること。`tests/test_agent_methods.py`
    の `test_corrected_calls_bind_to_the_real_signature` が実シグネチャに束縛できる
    ことを恒久的に守る（提示した修正案が古くなる、が潰したかった失敗そのもの）。
    """
    code = finding.get("code")
    obj = _object(snapshot, finding.get("id"))
    if obj is None:
        return None
    artboard = snapshot.artboard

    if code == "offscreen":
        x, y, w, h = obj.box
        # アートボード内へ収まる位置へクランプする。
        to_x = min(max(x, 0.0), max(artboard.width_px - w, 0.0))
        to_y = min(max(y, 0.0), max(artboard.height_px - h, 0.0))
        return {
            "tool": "move_objects",
            "arguments": {"items": [{"id": obj.id, "to": [round(to_x, 1), round(to_y, 1)]}]},
            "note": "アートボード内へ収まる位置に移動します",
        }
    if code == "clipped":
        x, y, w, h = finding.get("bbox", obj.box)
        if not finding.get("fits", True):
            # アートボードより大きいので、動かしても必ずどこかが切れる。
            # 位置だけ直す案を返すと、送り返しても同じ警告が出続けて往復が
            # 終わらない（収束しない修正案は嘘と同じ）。幾何が box の型だけ
            # 縮めて収める案を返し、それ以外は素直に案なしにする。
            if obj.type in _RESIZABLE_TYPES:
                scale = min(artboard.width_px / w, artboard.height_px / h)
                return {
                    "tool": "update_objects",
                    "arguments": {
                        "items": [
                            {
                                "id": obj.id,
                                "x": 0.0,
                                "y": 0.0,
                                "width": round(obj.box[2] * scale, 1),
                                "height": round(obj.box[3] * scale, 1),
                            }
                        ]
                    },
                    "note": "アートボードより大きいので、縦横比を保って収まる大きさに縮めます",
                }
            return None
        to_x = min(max(x, 0.0), artboard.width_px - w)
        to_y = min(max(y, 0.0), artboard.height_px - h)
        # bbox は回転後の外接矩形なので、移動量に直してから元の x/y に足す。
        return {
            "tool": "move_objects",
            "arguments": {
                "items": [{"id": obj.id, "dx": round(to_x - x, 1), "dy": round(to_y - y, 1)}]
            },
            "note": "アートボードに収まる位置へずらします",
        }
    if code == "degenerate":
        return {
            "tool": "update_objects",
            "arguments": {"items": [{"id": obj.id, "width": 120.0, "height": 80.0}]},
            "note": "幅・高さを正の値にします（値は目安なので図に合わせて変えてください）",
        }
    if code == "invisible":
        # 2026-09-25 訂正: 塗りではなく線を戻す（`diagnostics.check_invisible`
        # の docstring 参照）。開曲線に塗りを与えると塊になり（曲線は
        # `DEFAULT_SHAPE_FILL` の対象外というユーザー決定に反する）、画像の上の
        # 意図的な `fill=null` の枠を不透明な灰色で塗りつぶして下を隠して
        # しまう、という 2 つの実害があったため、rect/ellipse/curve のすべてで
        # 塗りは一切提案しない。既定値は `CurveObject`（唯一 stroke が非 None の
        # 既定を持つ塗り型）の dataclass 既定から取り、ハードコードを避ける。
        from app.model.objects import CurveObject

        curve_defaults = {f.name: f.default for f in dataclasses.fields(CurveObject)}
        item: dict[str, Any] = {"id": obj.id}
        # `is_stroked`（`app.graphics.strokes`）と同じ判定にする: 空文字も
        # None と同じ「線なし」。ここがずれると、`stroke=""` の壊れた/手編集の
        # project.json では何も補わない corrected_call を返してしまい、送り
        # 返しても `invisible` が消えない（収束しない修正案は嘘と同じ）。
        if not obj.stroke:
            item["stroke"] = curve_defaults["stroke"]
        if obj.stroke_width <= 0.0:
            item["stroke_width"] = curve_defaults["stroke_width"]
        return {
            "tool": "update_objects",
            "arguments": {"items": [item]},
            "note": (
                "塗りも線も無く不可視なので線を戻します"
                "（開曲線が塗りの塊になったり、下にある画像を塗りで隠したり"
                "しないよう、塗りは提案しません）"
            ),
        }
    if code == "overlap":
        return {
            "tool": "move_objects",
            "arguments": {
                "items": [
                    {
                        "id": obj.id,
                        "relative": {
                            "to": finding["other_id"],
                            "side": "below",
                            "gap": 40,
                            "align": "center",
                        },
                    }
                ]
            },
            "note": "相手の下へずらします（side を変えれば別の方向にも寄せられます）",
        }
    if code == "occluded":
        return {
            "tool": "order_objects",
            "arguments": {"ids": [obj.id], "action": "front"},
            "note": "覆われている側を前面に出します",
        }
    if code == "text_overflow":
        if finding.get("kind") == "host_shape":
            # **ここを「自然サイズにリサイズ」にしてはいけない。** ラベルは既に
            # 自然サイズなことが多く、その場合 no-op になって同じ警告が永久に
            # 出続ける（実機デモで無限ループを踏んだ）。ラベル先に収まる大きさなら
            # 中央へ寄せ、収まらないならフォントを縮める — どちらも必ず状況が動く。
            host = _object(snapshot, finding.get("host_id"))
            if host is None:
                return None
            host_x, host_y, host_w, host_h = host.box
            _tx, _ty, text_w, text_h = obj.box
            if text_w <= 0.0 or text_h <= 0.0:
                return None
            if text_w <= host_w and text_h <= host_h:
                return {
                    "tool": "move_objects",
                    "arguments": {
                        "items": [
                            {
                                "id": obj.id,
                                "relative": {"to": host.id, "side": "inside", "align": "center"},
                            }
                        ]
                    },
                    "note": "ラベルをラベル先の中央へ収めます",
                }
            # **箱とフォントを一緒に縮める。** フォントだけ縮めても文字の箱は
            # 変わらないので host からはみ出したままで直らない。箱だけ縮めると
            # 今度は own_box のあふれが出て、自然サイズへ戻され、また host から
            # はみ出す — と振動する。両方を同じ比率で縮めるのが唯一収束する形。
            scale = min(host_w / text_w, host_h / text_h)
            new_w = text_w * scale
            new_h = text_h * scale
            item: dict[str, Any] = {
                "id": obj.id,
                "x": round(host_x + (host_w - new_w) / 2.0, 1),
                "y": round(host_y + (host_h - new_h) / 2.0, 1),
                "width": round(new_w, 1),
                "height": round(new_h, 1),
            }
            if obj.font_size > 0.0:
                item["font_size"] = max(round(obj.font_size * scale, 1), 1.0)
            return {
                "tool": "update_objects",
                "arguments": {"items": [item]},
                "note": "ラベル先より大きいので、箱とフォントを同じ比率で縮めて中央に収めます",
            }
        if obj.text_natural_size is not None:
            w, h = obj.text_natural_size
            return {
                "tool": "update_objects",
                "arguments": {
                    "items": [{"id": obj.id, "width": round(w, 1), "height": round(h, 1)}]
                },
                "note": "文字がちょうど収まる寸法に広げます",
            }
        return {
            "tool": "update_objects",
            "arguments": {"items": [{"id": obj.id, "font_size": max(obj.font_size - 2.0, 1.0)}]},
            "note": "箱を変えずに収めるならフォントを小さくします",
        }
    if code == "low_contrast":
        from app.graphics import legibility

        background = finding["background"]
        color = legibility.readable_color(background)
        if obj.opacity < 1.0 and (
            legibility.contrast_ratio(
                legibility.blend_over(color, background, obj.opacity), background
            )
            < finding["required"]
        ):
            # 半透明だと、文字色をどれだけ振っても合成後の色が背景側へ沈んで
            # しまい、色の変更だけでは収束しない（`diagnostics._effective_background`
            # の二重合成 docstring 参照）。不透明度も 1.0 に戻し、そのときの
            # 背景（自前背景があればそれ、無ければ `background` がそのまま
            # 不透明時の下地と一致する）に対して読める色を選び直す。
            full_background = obj.background if obj.background is not None else background
            color = legibility.readable_color(full_background)
            return {
                "tool": "update_objects",
                "arguments": {"items": [{"id": obj.id, "color": color, "opacity": 1.0}]},
                "note": "半透明では文字色だけでは読みやすくならないため、不透明度も 1.0 に戻します",
            }
        return {
            "tool": "update_objects",
            "arguments": {"items": [{"id": obj.id, "color": color}]},
            "note": "背景に対してコントラストの取れる文字色にします",
        }
    if code == "small_text":
        from app.graphics import legibility

        target_px = legibility.point_size_to_px(
            diagnostics.MIN_EFFECTIVE_PT, artboard.width_px, artboard.width_mm
        )
        if target_px <= 0.0:
            return None
        # 切り上げる。丸めで下振れすると、修正案を送っても同じ警告がもう一度出る
        # （エージェントが往復し続ける最悪の失敗モード）。
        font_size = math.ceil(max(target_px, obj.font_size) * 10.0) / 10.0
        return {
            "tool": "update_objects",
            "arguments": {"items": [{"id": obj.id, "font_size": font_size}]},
            "note": f"出力実寸で {diagnostics.MIN_EFFECTIVE_PT}pt になる font_size にします",
        }
    return None


def with_suggestions(
    findings: list[dict[str, Any]], snapshot: diagnostics.DocumentSnapshot
) -> list[dict[str, Any]]:
    """各所見に `corrected_call` を添えて返す（元の所見は書き換えない）。"""
    enriched: list[dict[str, Any]] = []
    for finding in findings:
        corrected = suggest_fix(finding, snapshot)
        enriched.append(finding if corrected is None else {**finding, "corrected_call": corrected})
    return enriched
