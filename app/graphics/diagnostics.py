"""図の破綻を機械可読に検出する（Qt 非依存の純関数）。

**なぜスナップショット経由なのか**: 判定そのものは座標と色の計算でしかないが、
文字の実レイアウト採寸だけは Qt（`QFontMetricsF`）が要る。そこで
「GUI スレッドで採寸込みのスナップショットを作る」（`app/agent/diagnose.py`）と
「スナップショットを解析する」（本モジュール）に割り、後者を Qt にも
`Document` にも触らない純関数にしてある。この形なので、

- 解析はワーカースレッドへそのまま出せる（`Document` を触らないので競合しない）、
- 検査項目を絞れば絞った分だけ計算が減る（遅延評価）、
- テストが fixture なしで書ける、

の 3 つが同時に成り立つ。

検出するもの（`CHECK_NAMES`）:

| code | 意味 |
|---|---|
| `offscreen` | アートボードの外にあり描画されない |
| `degenerate` | 幅か高さが 0 で見えない |
| `overlap` | オブジェクト同士が部分的に重なっている |
| `occluded` | 不透明なオブジェクトに完全に覆われている |
| `text_overflow` | 文字が自分の箱／ラベル先の図形からあふれている |
| `low_contrast` | 文字色と背景色のコントラストが WCAG AA を下回る |
| `small_text` | 出力実寸で小さすぎる |

**誤警告を出さないことを、検出漏れより優先する。** エージェントは警告を信じて
修正呼び出しを送るので、嘘の警告は嘘の修正を生む。判断に足る情報が無い場合
（画像の上の文字色など）は黙る。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from app.graphics import boxes as bx
from app.graphics import legibility

Box = tuple[float, float, float, float]

#: 検査項目の全名称。`collect` / `critique` の `checks` で絞れる。
CHECK_NAMES: tuple[str, ...] = (
    "offscreen",
    "degenerate",
    "overlap",
    "occluded",
    "text_overflow",
    "low_contrast",
    "small_text",
)

#: 重なり面積がこの比（小さい方の面積に対する比）を下回るなら黙る。
#: 隣り合わせに置いた図形が線幅ぶん触れている程度で警告しないための下限。
OVERLAP_MIN_RATIO = 0.10
#: 出力実寸でこれを下回る文字を警告する（pt）。
MIN_EFFECTIVE_PT = 6.0
#: 不透明とみなす opacity の下限。
OPAQUE_THRESHOLD = 0.99
#: 文字あふれをこの px 未満なら黙る（採寸の丸め誤差を警告にしない）。
OVERFLOW_TOLERANCE_PX = 1.0

#: 面を持たず、重なり判定に意味がない型。
_AREALESS_TYPES = frozenset({"line", "arrow", "freehand", "connector"})
#: 文字を持つ型。
_TEXTUAL_TYPES = frozenset({"text", "math"})


@dataclass(frozen=True)
class ObjectSnapshot:
    """診断に必要な 1 オブジェクトの値だけを写したもの（Qt にもモデルにも依存しない）。"""

    id: int
    type: str
    name: str
    box: Box
    rotation: float
    opacity: float
    visible: bool
    z_index: int
    group_id: int | None = None
    #: rect / ellipse の塗り。None は透明。
    fill: str | None = None
    #: text / math の文字色。
    color: str | None = None
    #: image がアルファチャンネルを持つか（持つなら背後を隠しきらない）。
    has_alpha: bool = False
    #: text / math の内容とサイズ。
    text: str = ""
    font_size: float = 0.0
    #: GUI スレッドで `QFontMetricsF` により採寸した実レイアウト寸法 (w, h)。
    text_layout_size: tuple[float, float] | None = None
    #: 内容にちょうど合う既定寸法 (w, h)。修正案の width/height に使う。
    text_natural_size: tuple[float, float] | None = None

    @property
    def areal(self) -> bool:
        """面を持つ（重なりを論じられる）型か。"""
        return self.type not in _AREALESS_TYPES

    @property
    def opaque(self) -> bool:
        """背後を隠しきるか。"""
        if self.opacity < OPAQUE_THRESHOLD or not self.visible:
            return False
        if self.type in ("rect", "ellipse"):
            return self.fill is not None
        if self.type == "image":
            return not self.has_alpha
        # text / math はほぼグリフの隙間なので、覆い隠すとはみなさない。
        return False


@dataclass(frozen=True)
class ArtboardSnapshot:
    width_px: float
    height_px: float
    width_mm: float
    target_dpi: int
    background: str


@dataclass(frozen=True)
class DocumentSnapshot:
    """`analyze` の入力。GUI スレッドで作り、ワーカースレッドへ渡せる。"""

    artboard: ArtboardSnapshot
    objects: tuple[ObjectSnapshot, ...] = field(default_factory=tuple)
    doc_uid: str = ""
    revision: int = 0


def _finding(code: str, obj: ObjectSnapshot, message: str, **extra: Any) -> dict[str, Any]:
    return {"code": code, "id": obj.id, "severity": "warn", "message": message, **extra}


# --------------------------------------------------------------------------
# 個別の検査
# --------------------------------------------------------------------------


def check_bounds(snapshot: DocumentSnapshot, *, offscreen: bool, degenerate: bool) -> list[dict]:
    """アートボード外・退化した寸法。

    回転を考慮する（`rotated_aabb`）。回転した矩形の実際のはみ出しは、回転を
    無視した bbox では判定できない。
    """
    findings: list[dict[str, Any]] = []
    aw = snapshot.artboard.width_px
    ah = snapshot.artboard.height_px
    for obj in snapshot.objects:
        if not obj.visible:
            continue
        x, y, w, h = bx.rotated_aabb(obj.box, obj.rotation)
        if offscreen and (x + w < 0.0 or y + h < 0.0 or x > aw or y > ah):
            findings.append(
                _finding(
                    "offscreen",
                    obj,
                    f"オブジェクト {obj.id} ({obj.type}) は bbox "
                    f"{[x, y, w, h]} でアートボードの外にあり、描画されません",
                    bbox=[x, y, w, h],
                )
            )
        elif degenerate and obj.areal and (obj.box[2] <= 0.0 or obj.box[3] <= 0.0):
            findings.append(
                _finding(
                    "degenerate",
                    obj,
                    f"オブジェクト {obj.id} ({obj.type}) は "
                    f"width={obj.box[2]} height={obj.box[3]} のため不可視です",
                    width=obj.box[2],
                    height=obj.box[3],
                )
            )
    return findings


def check_overlap(snapshot: DocumentSnapshot, *, overlap: bool, occluded: bool) -> list[dict]:
    """オブジェクト同士の重なりと遮蔽。

    黙る条件（誤警告を避けるため意図的に狭くしてある）:

    - 線・矢印・フリーハンド・コネクタ: bbox が斜めの包絡でしかないので対象外。
    - text / math: ラベルは重なるのが仕事なので `overlap` の対象外
      （`occluded` には参加する — 完全に覆われたら本当に読めない）。
    - 同じ `group_id`: 意図的な合成。
    - 完全内包: 「箱の中のラベル」は正常。`occluded` 側で判断する。
    """
    if not (overlap or occluded):
        return []
    candidates = [o for o in snapshot.objects if o.visible and o.areal]
    findings: list[dict[str, Any]] = []
    for i, a in enumerate(candidates):
        for b in candidates[i + 1 :]:
            if a.group_id is not None and a.group_id == b.group_id:
                continue
            if not bx.obb_overlap(a.box, a.rotation, b.box, b.rotation):
                continue
            # z が後ろ（配列の後ろ）のほうが手前。
            front, back = (b, a) if b.z_index > a.z_index else (a, b)
            if (
                occluded
                and front.opaque
                and bx.covers(front.type, front.box, front.rotation, back.box, back.rotation)
            ):
                findings.append(
                    _finding(
                        "occluded",
                        back,
                        f"オブジェクト {back.id} ({back.type}) は"
                        f" {front.id} ({front.type}) に完全に覆われて見えません",
                        by=front.id,
                    )
                )
                continue
            if not overlap:
                continue
            if a.type in _TEXTUAL_TYPES or b.type in _TEXTUAL_TYPES:
                continue
            if bx.obb_contains(front.box, front.rotation, back.box, back.rotation) or (
                bx.obb_contains(back.box, back.rotation, front.box, front.rotation)
            ):
                continue  # 内包は重なり警告にしない（`occluded` の担当）
            inter_area = bx.obb_overlap_area(a.box, a.rotation, b.box, b.rotation)
            smaller = min(bx.area(a.box), bx.area(b.box))
            if smaller <= 0.0:
                continue
            ratio = inter_area / smaller
            if ratio < OVERLAP_MIN_RATIO:
                continue
            findings.append(
                _finding(
                    "overlap",
                    a,
                    f"オブジェクト {a.id} ({a.type}) と {b.id} ({b.type}) が"
                    f" 面積比 {ratio:.0%} で重なっています",
                    other_id=b.id,
                    ratio=round(ratio, 4),
                    area=round(inter_area, 2),
                )
            )
    return findings


def background_behind(snapshot: DocumentSnapshot, obj: ObjectSnapshot) -> tuple[str | None, str]:
    """`obj` の背後の色と、その出どころ。

    z が下で最も手前にある「obj の中心を含む」オブジェクトの塗りを採る。
    **画像が背後にある場合は色を返さない**（画素を推測しないため）。
    見つからなければアートボード背景。
    """
    center = bx.box_center(bx.normalized(obj.box))
    for other in sorted(snapshot.objects, key=lambda o: o.z_index, reverse=True):
        if other.z_index >= obj.z_index or not other.visible or other.id == obj.id:
            continue
        if not other.areal or not bx.point_in_obb(center, other.box, other.rotation):
            continue
        if other.type == "ellipse" and not bx.point_in_ellipse(center, other.box, other.rotation):
            continue
        if other.type == "image":
            return (None, f"object:{other.id}")
        if other.type in ("rect", "ellipse"):
            if other.fill is None:
                continue  # 透明な図形は背景にならない。さらに下を見る
            return (other.fill, f"object:{other.id}")
    return (snapshot.artboard.background, "artboard")


def find_host_shape(snapshot: DocumentSnapshot, obj: ObjectSnapshot) -> ObjectSnapshot | None:
    """`obj`（文字）がラベル付けしている図形＝ z が下で中心を含む最も手前のもの。"""
    center = bx.box_center(bx.normalized(obj.box))
    for other in sorted(snapshot.objects, key=lambda o: o.z_index, reverse=True):
        if other.z_index >= obj.z_index or not other.visible or other.id == obj.id:
            continue
        if other.type not in ("rect", "ellipse", "image"):
            continue
        if bx.point_in_obb(center, other.box, other.rotation):
            return other
    return None


def check_text(
    snapshot: DocumentSnapshot,
    *,
    text_overflow: bool,
    low_contrast: bool,
    small_text: bool,
) -> list[dict]:
    """文字のあふれ・コントラスト・実寸サイズ。"""
    if not (text_overflow or low_contrast or small_text):
        return []
    artboard = snapshot.artboard
    findings: list[dict[str, Any]] = []
    for obj in snapshot.objects:
        if obj.type not in _TEXTUAL_TYPES or not obj.visible:
            continue
        if obj.type == "text" and not obj.text:
            continue

        if text_overflow:
            findings.extend(_text_overflow_findings(snapshot, obj))
        if small_text and obj.font_size > 0.0:
            effective_pt = legibility.effective_point_size(
                obj.font_size, artboard.width_px, artboard.width_mm
            )
            if 0.0 < effective_pt < MIN_EFFECTIVE_PT:
                findings.append(
                    _finding(
                        "small_text",
                        obj,
                        f"オブジェクト {obj.id} ({obj.type}) は出力実寸で"
                        f" {effective_pt:.1f}pt になり、{MIN_EFFECTIVE_PT}pt を下回ります",
                        effective_pt=round(effective_pt, 2),
                        threshold_pt=MIN_EFFECTIVE_PT,
                        font_size_px=obj.font_size,
                        px_per_mm=round(
                            legibility.px_per_mm(artboard.width_px, artboard.width_mm), 4
                        ),
                        # math は箱に中心フィットするため、実際にはこれより小さくなりうる。
                        approximate=obj.type == "math",
                    )
                )
        if low_contrast and obj.color:
            findings.extend(_contrast_findings(snapshot, obj))
    return findings


def _text_overflow_findings(
    snapshot: DocumentSnapshot, obj: ObjectSnapshot
) -> list[dict[str, Any]]:
    findings: list[dict[str, Any]] = []
    _x, _y, w, h = bx.normalized(obj.box)
    # math は `MathItem` が箱に中心フィットさせるので自分の箱からはあふれない。
    if obj.type == "text" and obj.text_layout_size is not None:
        lw, lh = obj.text_layout_size
        dx = max(0.0, lw - w)
        dy = max(0.0, lh - h)
        if dx > OVERFLOW_TOLERANCE_PX or dy > OVERFLOW_TOLERANCE_PX:
            findings.append(
                _finding(
                    "text_overflow",
                    obj,
                    f"オブジェクト {obj.id} の文字が自分の箱から"
                    f" 右に {dx:.0f}px・下に {dy:.0f}px あふれています",
                    kind="own_box",
                    overflow=[round(dx, 2), round(dy, 2)],
                    text_bbox=[obj.box[0], obj.box[1], round(lw, 2), round(lh, 2)],
                )
            )
            return findings
    host = find_host_shape(snapshot, obj)
    if host is not None and not bx.covers(
        host.type, host.box, host.rotation, obj.box, obj.rotation
    ):
        findings.append(
            _finding(
                "text_overflow",
                obj,
                f"オブジェクト {obj.id} の文字がラベル先の"
                f" {host.id} ({host.type}) からはみ出しています",
                kind="host_shape",
                host_id=host.id,
                text_bbox=list(bx.normalized(obj.box)),
            )
        )
    return findings


def _contrast_findings(snapshot: DocumentSnapshot, obj: ObjectSnapshot) -> list[dict[str, Any]]:
    background, source = background_behind(snapshot, obj)
    if background is None:
        # 画像の上の文字。画素を推測しない（嘘の警告より沈黙を選ぶ）。
        return []
    assert obj.color is not None
    try:
        foreground = legibility.blend_over(obj.color, background, obj.opacity)
        ratio = legibility.contrast_ratio(foreground, background)
    except ValueError:
        return []  # 色が壊れている。値の検証は validate.py の仕事
    effective_pt = legibility.effective_point_size(
        obj.font_size, snapshot.artboard.width_px, snapshot.artboard.width_mm
    )
    required = legibility.required_ratio(effective_pt if effective_pt > 0.0 else obj.font_size)
    if ratio >= required:
        return []
    return [
        _finding(
            "low_contrast",
            obj,
            f"オブジェクト {obj.id} の文字色 {obj.color} は背景 {background} に対して"
            f" コントラスト比 {ratio:.2f}（必要 {required}）で読みにくい状態です",
            ratio=round(ratio, 2),
            required=required,
            foreground=obj.color,
            background=background,
            background_source=source,
        )
    ]


# --------------------------------------------------------------------------
# 入口
# --------------------------------------------------------------------------


def analyze(
    snapshot: DocumentSnapshot,
    checks: tuple[str, ...] | None = None,
    ids: tuple[int, ...] | None = None,
) -> list[dict[str, Any]]:
    """スナップショットを解析して所見の一覧を返す（純関数・スレッド安全）。

    `checks` に含まれない検査は**計算そのものを行わない**（遅延評価）。
    `ids` を渡すと、その id を参照する所見だけに絞る。
    """
    enabled = set(CHECK_NAMES if checks is None else checks)
    findings = check_bounds(
        snapshot,
        offscreen="offscreen" in enabled,
        degenerate="degenerate" in enabled,
    )
    findings += check_overlap(
        snapshot,
        overlap="overlap" in enabled,
        occluded="occluded" in enabled,
    )
    findings += check_text(
        snapshot,
        text_overflow="text_overflow" in enabled,
        low_contrast="low_contrast" in enabled,
        small_text="small_text" in enabled,
    )
    if ids is not None:
        wanted = set(ids)
        findings = [f for f in findings if wanted & _referenced_ids(f)]
    return findings


def _referenced_ids(finding: dict[str, Any]) -> set[int]:
    """所見が言及している全 id（`ids` フィルタ用）。"""
    return {
        int(finding[key])
        for key in ("id", "other_id", "by", "host_id")
        if isinstance(finding.get(key), int)
    }


def summarize(findings: list[dict[str, Any]]) -> dict[str, Any]:
    by_code: dict[str, int] = {}
    for finding in findings:
        by_code[finding["code"]] = by_code.get(finding["code"], 0) + 1
    return {"total": len(findings), "by_code": by_code}
