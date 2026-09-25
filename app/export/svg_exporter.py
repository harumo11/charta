"""自前 SVG シリアライザ（M4契約 §7、M5契約 §4、CLAUDE.md §8）。

Qt の `QSvgGenerator` 経由の `scene.render()` は `QGraphicsSvgItem`・`QPixmap` 画像・
フォントで劣化/欠落を起こすため使わない（§4 の検証済み制約）。代わりに `Document`
（モデル）を直接 z 順に走査し、種別ごとにネイティブ SVG 要素を組み立てる。

対応種別: rect / ellipse / line / arrow / freehand / text / image / math / curve。
math（数式）は matplotlib が生成した SVG をそのまま入れ子 `<svg>` として挿入する
ことでベクター保持する（§8「そのまま入れ子挿入」）。未対応種別は XML コメントとして
安全にスキップし将来の拡張を妨げない（`SVG_RENDERERS` への `@register_svg_renderer`
登録を追加するだけで足りる構造。Phase 3 契約 Stage 1C）。
"""

from __future__ import annotations

import os
import re
import sys
import warnings
from collections.abc import Callable
from xml.sax.saxutils import escape, quoteattr

from PySide6.QtCore import QRectF
from PySide6.QtGui import QFont, QFontInfo, QPainterPath

from app.export.text_outline import text_to_path, valign_offset, wrapped_lines
from app.graphics import curves
from app.graphics.arrows import (
    arrow_visible,
    circle_center_radius,
    open_segment_ends,
    shorten_amount,
    triangle_points,
    unit_vector,
)
from app.graphics.avoid import collect_obstacles
from app.graphics.image_pipeline import processed_png_base64
from app.graphics.routing import (
    build_routing,
    connector_endpoints_from_model,
    endpoint_direction,
    line_endpoints_from_model,
)
from app.graphics.strokes import is_stroked
from app.math.mathtext_render import MathRenderError, get_math_svg
from app.model.document import Document
from app.model.objects import BaseObject

_SVG_NS = "http://www.w3.org/2000/svg"
_XLINK_NS = "http://www.w3.org/1999/xlink"

_ALIGN_ANCHOR: dict[str, str] = {"left": "start", "center": "middle", "right": "end"}

# matplotlib 生成 SVG からルート <svg ...> 要素を抜き出すための正規表現
# （<?xml?> 宣言・DOCTYPE を除去した後の本体走査に使う。属性値は matplotlib の
# 出力が常にダブルクォートであることを前提にする）。
_SVG_ROOT_RE = re.compile(r"<svg\b[^>]*>", re.DOTALL)
_SVG_ATTR_RE = re.compile(r'([\w:.-]+)\s*=\s*"([^"]*)"')


def _fmt(value: float) -> str:
    """座標/寸法を SVG 属性用の文字列にする（指数表記を避ける固定小数）。"""
    return f"{float(value):.3f}"


def _fill_attr(fill: str | None) -> str:
    return fill if fill else "none"


def _dash_attr(dash: str, stroke_width: float) -> str:
    """dash 種別を `stroke-dasharray` 属性文字列にする（solid は空文字）。

    下限クランプ 0.1 は幅 0（線なし）を想定したものではない
    （`_stroke_attrs` が線なしなら早期リターンしここへ到達させない）。
    0 < stroke_width < 0.1 の極細線でもダッシュ間隔が 0 にならないようにするため
    のガードであり、線なし判定とは独立に残す。
    """
    sw = max(float(stroke_width), 0.1)
    if dash == "dash":
        return f' stroke-dasharray="{_fmt(sw * 4.0)},{_fmt(sw * 2.0)}"'
    if dash == "dot":
        return f' stroke-dasharray="{_fmt(sw)},{_fmt(sw * 2.0)}"'
    return ""


def _stroke_attrs(obj: BaseObject, *, dash: bool = True) -> str:
    """stroke / stroke-width / stroke-dasharray をまとめた属性文字列。

    線を描かない（stroke=None または幅 0）なら `stroke="none"` だけを返す
    — 幅 0 を素通しすると SVG では線が消えるのに画面は cosmetic 1px を描く、
    という画面/出力の食い違いになる（`app.graphics.strokes.is_stroked` が判定）。
    属性を省略せず明示するのは、将来 <g> に stroke を持たせても継承されないように
    するためと、テストで grep できるようにするため。

    `dash=False` は `dash` フィールドを持たないオブジェクト種別
    （freehand）向け。フィールドが無いのに `obj.dash` を読むと落ちる。
    """
    if not is_stroked(obj):
        return ' stroke="none"'
    attrs = f' stroke={quoteattr(obj.stroke)} stroke-width="{_fmt(obj.stroke_width)}"'
    if dash:
        attrs += _dash_attr(obj.dash, obj.stroke_width)
    return attrs


def _xml_comment(text: str) -> str:
    """XML コメントとして安全な文字列にする（`--` はコメント内で不正なため置換）。"""
    safe = text.replace("--", "- -")
    return f"<!-- {escape(safe)} -->"


def qpainterpath_to_svg_path_d(path: QPainterPath) -> str:
    """`QPainterPath` を SVG `<path d=...>` 用の文字列に変換する（M/L/C のみ）。

    `elementCount`/`elementAt` を走査する。要素数は対象パス（グリフ・矢じり等）
    に対して限定的であり、Python ループでも許容される（§13）。
    """
    parts: list[str] = []
    i = 0
    count = path.elementCount()
    while i < count:
        el = path.elementAt(i)
        el_type = el.type
        if el_type == QPainterPath.ElementType.MoveToElement:
            parts.append(f"M {_fmt(el.x)} {_fmt(el.y)}")
            i += 1
        elif el_type == QPainterPath.ElementType.LineToElement:
            parts.append(f"L {_fmt(el.x)} {_fmt(el.y)}")
            i += 1
        elif el_type == QPainterPath.ElementType.CurveToElement:
            c1 = el
            c2 = path.elementAt(i + 1)
            end = path.elementAt(i + 2)
            parts.append(
                f"C {_fmt(c1.x)} {_fmt(c1.y)} {_fmt(c2.x)} {_fmt(c2.y)} {_fmt(end.x)} {_fmt(end.y)}"
            )
            i += 3
        else:  # pragma: no cover - 想定外要素種別は無視して継続する
            i += 1
    return " ".join(parts)


# --------------------------------------------------------------------------
# box 幾何オブジェクト（rect/ellipse/text/image/freehand）の g ラップ
# --------------------------------------------------------------------------


def _group_open(x: float, y: float, w: float, h: float, rotation: float, opacity: float) -> str:
    transform = [f"translate({_fmt(x)},{_fmt(y)})"]
    if rotation:
        transform.append(f"rotate({_fmt(rotation)} {_fmt(w / 2.0)} {_fmt(h / 2.0)})")
    attrs = f' transform="{" ".join(transform)}"'
    if opacity != 1.0:
        attrs += f' opacity="{_fmt(opacity)}"'
    return f"<g{attrs}>"


def _wrap_box(inner: str, obj: BaseObject) -> str:
    return (
        _group_open(obj.x, obj.y, obj.width, obj.height, obj.rotation, obj.opacity) + inner + "</g>"
    )


def _wrap_opacity(inner: str, opacity: float) -> str:
    if opacity == 1.0:
        return inner
    return f'<g opacity="{_fmt(opacity)}">{inner}</g>'


# --------------------------------------------------------------------------
# 種別ごとの要素生成
# --------------------------------------------------------------------------


def _render_rect(obj: BaseObject) -> str:
    w, h = obj.width, obj.height
    attrs = f'x="0" y="0" width="{_fmt(w)}" height="{_fmt(h)}"'
    corner_radius = getattr(obj, "corner_radius", 0.0)
    if corner_radius > 0:
        attrs += f' rx="{_fmt(corner_radius)}"'
    attrs += f" fill={quoteattr(_fill_attr(obj.fill))}" + _stroke_attrs(obj)
    return f"<rect {attrs}/>"


def _render_ellipse(obj: BaseObject) -> str:
    w, h = obj.width, obj.height
    attrs = (
        f'cx="{_fmt(w / 2.0)}" cy="{_fmt(h / 2.0)}" rx="{_fmt(w / 2.0)}" ry="{_fmt(h / 2.0)}"'
        f" fill={quoteattr(_fill_attr(obj.fill))}"
    )
    attrs += _stroke_attrs(obj)
    return f"<ellipse {attrs}/>"


def _freehand_path_d(obj: BaseObject) -> str | None:
    """freehand の正規化点列 → ローカル座標(nx*w,ny*h)の path d。

    `FreehandItem._build_local_path` と同じ二次ベジェスムージングを用いる
    （見た目を画面表示と一致させるため）。点が1個以下なら None。
    """
    points: list[list[float]] = getattr(obj, "points", [])
    if len(points) < 2:
        return None
    w, h = obj.width, obj.height
    pts = [(nx * w, ny * h) for nx, ny in points]
    smoothing = float(getattr(obj, "smoothing", 0.0))
    d = [f"M {_fmt(pts[0][0])} {_fmt(pts[0][1])}"]
    if smoothing > 0.0 and len(pts) > 2:
        for i in range(1, len(pts) - 1):
            cx, cy = pts[i]
            nx, ny = pts[i + 1]
            mx, my = (cx + nx) / 2.0, (cy + ny) / 2.0
            d.append(f"Q {_fmt(cx)} {_fmt(cy)} {_fmt(mx)} {_fmt(my)}")
        lx, ly = pts[-1]
        d.append(f"L {_fmt(lx)} {_fmt(ly)}")
    else:
        for px, py in pts[1:]:
            d.append(f"L {_fmt(px)} {_fmt(py)}")
    return " ".join(d)


def _render_freehand(obj: BaseObject) -> str:
    d = _freehand_path_d(obj)
    if d is None:
        return _xml_comment("freehand: insufficient points, skipped")
    return (
        f'<path d="{d}" fill="none"{_stroke_attrs(obj, dash=False)}'
        ' stroke-linecap="round" stroke-linejoin="round"/>'
    )


def _curve_path_d(obj: BaseObject) -> str | None:
    """curve の正規化点列 → path d 属性文字列。

    `CurveItem._build_local_path`（`app/scene/items/curve_item.py`）と同一の
    `curves.curve_segments`/`curves.path_d` を通す（画面表示との一致のため。
    片方だけ直すと画面と SVG が食い違う）。点が2個未満の退化ケースは None。
    """
    points: list[list[float]] = getattr(obj, "points", [])
    closed = bool(getattr(obj, "closed", False))
    result = curves.curve_segments(
        points,
        obj.width,
        obj.height,
        closed=closed,
        tension=float(getattr(obj, "tension", curves.DEFAULT_TENSION)),
    )
    if result is None:
        return None
    start, segments = result
    return curves.path_d(start, segments, closed=closed, fmt=_fmt)


def _render_curve(obj: BaseObject) -> str:
    d = _curve_path_d(obj)
    if d is None:
        return _xml_comment("curve: insufficient points, skipped")
    # fill-rule="evenodd" は必須（Qt の QPainterPath 既定 OddEvenFill と一致させる。
    # SVG の既定は nonzero のため、明示しないと自己交差する曲線で塗りが食い違う）。
    attrs = (
        f'd="{d}" fill={quoteattr(_fill_attr(obj.fill))} fill-rule="evenodd"'
        f"{_stroke_attrs(obj)}"
        f' stroke-linecap="round" stroke-linejoin="round"'
    )
    return f"<path {attrs}/>"


def _render_arrowhead(
    tip_x: float,
    tip_y: float,
    dx: float,
    dy: float,
    shape: str,
    size: float,
    stroke: str,
    stroke_width: float,
) -> str:
    """`tip` に、線が `(dx, dy)` 向きに向かう前提での矢じりを描く（LineItem と同じ形状）。

    幾何計算（線端短縮量・triangle/circle/open の点列）は `app.graphics.arrows`
    に委譲する（Phase 1 契約 §1C/Stage2 §3）。SVG 文字列組み立てのみここで行う。
    """
    tip = (tip_x, tip_y)
    direction = (dx, dy)
    if shape == "triangle":
        tip_pt, b1, b2 = triangle_points(tip, direction, size)
        d = (
            f"M {_fmt(tip_pt[0])} {_fmt(tip_pt[1])} L {_fmt(b1[0])} {_fmt(b1[1])} "
            f"L {_fmt(b2[0])} {_fmt(b2[1])} Z"
        )
        return f'<path d="{d}" fill={quoteattr(stroke)}/>'
    if shape == "circle":
        center, r = circle_center_radius(tip, size)
        return (
            f'<circle cx="{_fmt(center[0])}" cy="{_fmt(center[1])}" r="{_fmt(r)}"'
            f" fill={quoteattr(stroke)}/>"
        )
    if shape == "open":
        end_plus, end_minus = open_segment_ends(tip, direction, size)
        segs = [
            f"M {_fmt(tip_x)} {_fmt(tip_y)} L {_fmt(end_plus[0])} {_fmt(end_plus[1])}",
            f"M {_fmt(tip_x)} {_fmt(tip_y)} L {_fmt(end_minus[0])} {_fmt(end_minus[1])}",
        ]
        d = " ".join(segs)
        return (
            f'<path d="{d}" fill="none" stroke={quoteattr(stroke)}'
            f' stroke-width="{_fmt(stroke_width)}"/>'
        )
    return ""  # pragma: no cover - 未知形状は描かない（"none" 相当）


def _render_line(document: Document, obj: BaseObject) -> str:
    """line/arrow をモデルから描画する。接着端は `line_endpoints_from_model`
    （Qt 非依存の共有経路、項目8）で実効座標に解決してから描く。
    """
    (x1, y1), (x2, y2) = line_endpoints_from_model(document, obj)
    direction = unit_vector((x1, y1), (x2, y2))
    arrow_size = max(float(obj.arrow_size), 0.0)
    lx1, ly1, lx2, ly2 = x1, y1, x2, y2
    if direction is not None:
        dx, dy = direction
        if arrow_visible(obj.arrow_end, arrow_size):
            shorten = shorten_amount(obj.arrow_end, arrow_size)
            lx2, ly2 = x2 - dx * shorten, y2 - dy * shorten
        if arrow_visible(obj.arrow_start, arrow_size):
            shorten = shorten_amount(obj.arrow_start, arrow_size)
            lx1, ly1 = x1 + dx * shorten, y1 + dy * shorten

    body = (
        f'<path d="M {_fmt(lx1)} {_fmt(ly1)} L {_fmt(lx2)} {_fmt(ly2)}" fill="none"'
        f'{_stroke_attrs(obj)} stroke-linecap="round"/>'
    )
    parts = [body]
    if direction is not None:
        dx, dy = direction
        if arrow_visible(obj.arrow_end, arrow_size):
            parts.append(
                _render_arrowhead(
                    x2, y2, dx, dy, obj.arrow_end, arrow_size, obj.stroke, obj.stroke_width
                )
            )
        if arrow_visible(obj.arrow_start, arrow_size):
            parts.append(
                _render_arrowhead(
                    x1, y1, -dx, -dy, obj.arrow_start, arrow_size, obj.stroke, obj.stroke_width
                )
            )
    return "".join(parts)


def _render_connector(document: Document, obj: BaseObject) -> str:
    """connector を `app.graphics.routing` でモデルから端点/ルーティングを解いて描画する。

    端点解決は `connector_endpoints_from_model`（Qt 非依存の共有経路）に委譲する。
    エージェント向けレンダリングの注釈オーバーレイも同じ関数を使うので、
    SVG と画面注釈で座標がずれない。
    """
    src_pt, tgt_pt = connector_endpoints_from_model(document, obj)
    # orthogonal は間にある図形を避ける。障害物の収集は `collect_obstacles` を
    # キャンバス側（connector_item）と共有する — 別々に集めると同じ図で
    # 画面と SVG の経路が食い違う（唯一の真実源を分岐させない、の一部）。
    obstacles = collect_obstacles(document, obj) if obj.routing == "orthogonal" else []
    points = build_routing(src_pt, tgt_pt, obj.routing, obstacles)

    line_points = list(points)
    arrow_size = max(float(obj.arrow_size), 0.0)
    if arrow_visible(obj.arrow_end, arrow_size):
        dx, dy = endpoint_direction(points)
        shorten = shorten_amount(obj.arrow_end, arrow_size)
        if shorten > 0.0:
            last_x, last_y = points[-1]
            line_points = points[:-1] + [(last_x - dx * shorten, last_y - dy * shorten)]

    d = " ".join(
        f"{'M' if i == 0 else 'L'} {_fmt(x)} {_fmt(y)}" for i, (x, y) in enumerate(line_points)
    )
    body = f'<path d="{d}" fill="none"{_stroke_attrs(obj)} stroke-linecap="round"/>'
    parts = [body]
    if arrow_visible(obj.arrow_end, arrow_size):
        dx, dy = endpoint_direction(points)
        tip_x, tip_y = points[-1]
        parts.append(
            _render_arrowhead(
                tip_x, tip_y, dx, dy, obj.arrow_end, arrow_size, obj.stroke, obj.stroke_width
            )
        )
    return _wrap_opacity("".join(parts), obj.opacity)


def _build_text_font(obj: BaseObject) -> QFont:
    """SVG 出力用の `QFont`（`text_item.font_for` と同じくピクセル実寸に固定する）。

    ポイントサイズのままだと描画デバイスの DPI で解決されてしまうため、
    デバイス非依存の解決結果を焼き込む（理由は `text_item.font_for` の docstring）。
    """
    font = QFont(obj.font_family)
    font.setPointSizeF(max(float(obj.font_size), 1.0))
    font.setBold(bool(obj.bold))
    font.setItalic(bool(obj.italic))
    font.setPixelSize(QFontInfo(font).pixelSize())
    return font


def _render_text_background(obj: BaseObject, element_opacity: float | None = None) -> str:
    """text の背景色（要望4）: 箱全体 `(0,0,w,h)` を塗る `<rect>`。無ければ空文字列。

    背景が無いときは `_wrap_box` の `<g>`（回転・不透明度を持つ）の内側先頭に
    置くだけで、`<g>` の不透明度をそのまま継承させれば正しい（塗る要素がこの
    1 個しか無いため、グループ合成も要素合成も同じ結果になる）。**背景がある
    ときは `element_opacity` が非 None で渡され、`<g>` 側は opacity=1.0 になる**
    （`_render_text_entry` 参照）。要素自身に `opacity` 属性を出すのはその場合
    だけで、理由は `_render_text` の docstring を参照。outline / `<text>` の
    両分岐・空テキストでも共通に出す（`TextItem.paint` が編集モードの早期
    return・アウトライン分岐より前で箱全体を塗るのと対称。B-2）。
    """
    background = getattr(obj, "background", None)
    if not background:
        return ""
    attrs = f'x="0" y="0" width="{_fmt(obj.width)}" height="{_fmt(obj.height)}"'
    attrs += f" fill={quoteattr(background)}"
    if element_opacity is not None:
        attrs += f' opacity="{_fmt(element_opacity)}"'
    return f"<rect {attrs}/>"


def _render_text(obj: BaseObject, outline_text: bool, element_opacity: float | None = None) -> str:
    """text 本体（背景＋グリフ）を SVG 要素にする。

    `element_opacity`（round-1 #8 / X #2）: 背景ありで `opacity < 1` のとき
    `_render_text_entry` から渡される。Qt は `QGraphicsItem` の opacity を
    **プリミティブごと**に適用する（`TextItem.paint` は背景 fillRect →
    グリフの順に別々に描く）ため、画面・PNG・PDF では「不透明な背景の上に
    半透明のグリフを重ねる」結果になり、グリフ色は背景色と混ざって淡くなる。
    一方 SVG の `<g opacity=...>` はグループを一度だけ合成するため、そのまま
    背景 `<rect>` とグリフを 1 つの `<g>` に入れると「グリフ本来の色のまま
    半透明の板を重ねる」結果になり、グリフがくっきり出すぎて他の出力と
    色が食い違う（round-1 #8 で報告・round-2 で修正）。これを避けるため、
    背景があるときは `<g>` 側の opacity を 1.0 にし、背景 `<rect>` と
    グリフ（`<path>`/`<text>`）のそれぞれに `opacity` 属性を個別に出す
    （Qt の per-primitive 合成を再現する）。Qt 側を「グループ合成」に
    寄せる案（`QGraphicsOpacityEffect`）は PDF 出力がラスタライズされて
    しまうため採らない。
    """
    font = _build_text_font(obj)
    rect = QRectF(0.0, 0.0, obj.width, obj.height)
    background = _render_text_background(obj, element_opacity)

    if outline_text:
        path = text_to_path(obj.text, font, rect, obj.align, bool(obj.underline), obj.valign)
        d = qpainterpath_to_svg_path_d(path)
        if not d:
            return background + _xml_comment("text: empty, skipped")
        path_attrs = f'd="{d}" fill={quoteattr(obj.color)}'
        if element_opacity is not None:
            path_attrs += f' opacity="{_fmt(element_opacity)}"'
        return background + f"<path {path_attrs}/>"

    # 非アウトライン: <text> はフォント依存（環境に同名フォントが無いと再現不可）。
    # outline_text=True と同じ QFont から QFontMetricsF で px 実寸を導出することで、
    # モデルの point-size をそのまま SVG の user-unit(px) に流用していた旧実装の
    # ずれ（outline/画面/PDF の見た目と不一致）を解消する。
    # <text> と outline の valign は同じ font・同じ valign_offset() で揃える
    # （text_to_path と別式を書かないこと。CLAUDE.md 参照）。
    # SVG の font-size は **em サイズ**（= 解決後のピクセルサイズ）。
    # `ascent + descent` は行ボックスの高さであって em ではない（Noto Sans CJK では
    # 約 1.45em）。ここを取り違えると `<text>` だけ 1.45 倍で描かれ、
    # outline 版・画面・PDF と食い違う。
    font_size_px = float(QFontInfo(font).pixelSize())
    offset = valign_offset(obj.text, font, rect, obj.valign)

    # 行分割は画面/アウトラインと同一エンジン（text_outline.wrapped_lines）を通す。
    # 以前は "\n" 分割のみで折返しを再現しておらず、折り返されたテキストは
    # <text> 出力だけ 1 行に伸びていた（2026-08-15 の折返し統一で修正）。
    # 各行の y は行スロット位置（top）＋**その行の実 ascent**（フォールバック
    # フォント込み。metrics.ascent() 固定だと欧文フォント指定＋日本語で数 px ずれる）。
    lines = wrapped_lines(obj.text, font, max(obj.width, 1.0))
    anchor = _ALIGN_ANCHOR.get(obj.align, "start")
    x = {"left": 0.0, "center": obj.width / 2.0, "right": obj.width}.get(obj.align, 0.0)
    weight = ' font-weight="bold"' if obj.bold else ""
    style = ' font-style="italic"' if obj.italic else ""
    decoration = ' text-decoration="underline"' if obj.underline else ""
    opacity_attr = f' opacity="{_fmt(element_opacity)}"' if element_opacity is not None else ""
    # 行末スペースは rstrip する: naturalTextWidth（= text-anchor の middle/end が
    # 基準にすべき幅）は行末スペースを含まないため、preserve のまま残すと
    # center/right 揃えがスペース分ずれる。行頭・行中の空白は xml:space="preserve"
    # で保持する（折返し行の字下げ・連続スペースが SVG の既定空白処理で潰れないように）。
    tspans = "".join(
        f'<tspan x="{_fmt(x)}" y="{_fmt(offset + top + ascent)}">{escape(line_text.rstrip())}'
        "</tspan>"
        for line_text, _natural_width, top, ascent in lines
    )
    text_el = (
        f'<text xml:space="preserve" font-family={quoteattr(obj.font_family)}'
        f' font-size="{_fmt(font_size_px)}"'
        f" fill={quoteattr(obj.color)}"
        f' text-anchor="{anchor}"{weight}{style}{decoration}{opacity_attr}>{tspans}</text>'
    )
    warning = _xml_comment(
        "text rendered as <text> (outline_text=False): requires matching font on viewer"
    )
    return background + warning + text_el


def _render_image(document: Document, obj: BaseObject) -> str:
    data = processed_png_base64(document, obj)
    if data is None:
        return _xml_comment(f"image: failed to load src={obj.src}")
    w, h = obj.width, obj.height
    href = f"data:image/png;base64,{data}"
    return (
        f'<image x="0" y="0" width="{_fmt(w)}" height="{_fmt(h)}"'
        f' preserveAspectRatio="none" xlink:href="{href}"/>'
    )


def _split_matplotlib_svg(svg_text: str) -> tuple[dict[str, str], str]:
    """matplotlib 生成 SVG からルート `<svg>` の属性と中身(inner)を取り出す。

    `<?xml ...?>` 宣言・`<!DOCTYPE ...>` は入れ子 SVG の中では不正のため、ここで
    ルート `<svg ...>...</svg>` の中身のみを切り出すことで自然に除去する。
    ルート要素が見つからない場合は `ValueError`。
    """
    match = _SVG_ROOT_RE.search(svg_text)
    if match is None:
        raise ValueError("matplotlib output has no <svg> root element")
    attrs = dict(_SVG_ATTR_RE.findall(match.group(0)))
    start = match.end()
    end = svg_text.rfind("</svg>")
    if end == -1 or end < start:
        raise ValueError("matplotlib output has no closing </svg>")
    return attrs, svg_text[start:end]


def _render_math(obj: BaseObject) -> str:
    """math を、matplotlib 生成 SVG の入れ子 `<svg>` としてそのまま挿入する（§8）。

    SVG 生成は `get_math_svg`（キー付き lru_cache）に一本化する。モデル側に古い
    キャッシュを持たせない（層違反・鮮度バグの回避）ため、`obj.latex` が変わった
    直後にエクスポートしても常に最新の latex に対応する SVG が使われる。
    生成/解析に失敗した場合は例外を握りつぶさず warn した上で、要素を省略し XML
    コメントで警告を残す（§9.4: 直前の有効表示維持はビュー側の責務、エクスポートは
    そのフレームでの最善描画を返せばよい）。
    """
    try:
        svg_text = get_math_svg(obj.latex, obj.font_size, obj.color)
    except MathRenderError as exc:
        message = f"charta: 数式のレンダリングに失敗しました latex={obj.latex!r}: {exc}"
        warnings.warn(message, stacklevel=2)
        print(message, file=sys.stderr)
        return _xml_comment(f"math: render failed for latex={obj.latex!r}: {exc}")
    try:
        attrs, inner = _split_matplotlib_svg(svg_text)
    except ValueError as exc:
        message = f"charta: 数式 SVG の解析に失敗しました latex={obj.latex!r}: {exc}"
        warnings.warn(message, stacklevel=2)
        print(message, file=sys.stderr)
        return _xml_comment(f"math: invalid svg for latex={obj.latex!r}: {exc}")

    view_box = attrs.get("viewBox") or f"0 0 {_fmt(obj.width)} {_fmt(obj.height)}"
    # レビュー所見対応: 画面（`MathItem._natural_fit_rect`）は box アスペクトと
    # matplotlib が生成した自然アスペクトがずれても歪ませず、box 内センターフィット
    # で描画する。ここを "none"（引き伸ばし）のままにすると、box アスペクトと自然
    # アスペクトがずれた場合（fontset 変更後の再採寸前や、プロパティパネルで
    # width/height を個別編集した場合）に画面と SVG 出力が食い違う。
    # "xMidYMid meet" は SVG 標準のアスペクト保持センターフィットで、画面側の
    # `_natural_fit_rect` と同じ挙動になる（box アスペクト == 自然アスペクトの
    # 通常ケースでは見た目も出力バイトも変わらない）。
    return (
        f'<svg x="0" y="0" width="{_fmt(obj.width)}" height="{_fmt(obj.height)}"'
        f' viewBox={quoteattr(view_box)} preserveAspectRatio="xMidYMid meet"'
        f" xmlns={quoteattr(_SVG_NS)}>{inner}</svg>"
    )


# --------------------------------------------------------------------------
# 型 → 描画関数のレジストリ（加法的登録、Phase 3 契約 Stage 1C）
# --------------------------------------------------------------------------

_SvgRenderFn = Callable[[Document, BaseObject, bool], str]

SVG_RENDERERS: dict[str, _SvgRenderFn] = {}


def register_svg_renderer(*type_names: str) -> Callable[[_SvgRenderFn], _SvgRenderFn]:
    """`type_names` それぞれに対して `SVG_RENDERERS` へ描画関数を登録するデコレータ。

    新しいオブジェクト型を SVG 出力に対応させるには、この形で登録関数を追加する
    だけでよい（`_render_object` 側の分岐を編集する必要はない）。
    """

    def decorator(fn: _SvgRenderFn) -> _SvgRenderFn:
        for name in type_names:
            SVG_RENDERERS[name] = fn
        return fn

    return decorator


@register_svg_renderer("rect")
def _render_rect_entry(document: Document, obj: BaseObject, outline_text: bool) -> str:
    return _wrap_box(_render_rect(obj), obj)


@register_svg_renderer("ellipse")
def _render_ellipse_entry(document: Document, obj: BaseObject, outline_text: bool) -> str:
    return _wrap_box(_render_ellipse(obj), obj)


@register_svg_renderer("line", "arrow")
def _render_line_entry(document: Document, obj: BaseObject, outline_text: bool) -> str:
    return _wrap_opacity(_render_line(document, obj), obj.opacity)


@register_svg_renderer("freehand")
def _render_freehand_entry(document: Document, obj: BaseObject, outline_text: bool) -> str:
    return _wrap_box(_render_freehand(obj), obj)


@register_svg_renderer("text")
def _render_text_entry(document: Document, obj: BaseObject, outline_text: bool) -> str:
    # round-1 #8 / round-2 #1・#6・#9: 背景あり＋opacity<1 のときだけ、グループの
    # opacity を 1.0 にして背景/グリフそれぞれへ個別に opacity を出す
    # （per-primitive 合成。理由は `_render_text` の docstring 参照）。背景が
    # 無ければ塗る要素が 1 個（グリフ）だけなのでグループ opacity と要素 opacity は
    # 同じ結果になり、既存の SVG バイト列は変わらない。
    if getattr(obj, "background", None) and obj.opacity != 1.0:
        inner = _render_text(obj, outline_text, element_opacity=obj.opacity)
        return _group_open(obj.x, obj.y, obj.width, obj.height, obj.rotation, 1.0) + inner + "</g>"
    return _wrap_box(_render_text(obj, outline_text), obj)


@register_svg_renderer("image")
def _render_image_entry(document: Document, obj: BaseObject, outline_text: bool) -> str:
    return _wrap_box(_render_image(document, obj), obj)


@register_svg_renderer("math")
def _render_math_entry(document: Document, obj: BaseObject, outline_text: bool) -> str:
    return _wrap_box(_render_math(obj), obj)


@register_svg_renderer("connector")
def _render_connector_entry(document: Document, obj: BaseObject, outline_text: bool) -> str:
    return _render_connector(document, obj)


@register_svg_renderer("curve")
def _render_curve_entry(document: Document, obj: BaseObject, outline_text: bool) -> str:
    return _wrap_box(_render_curve(obj), obj)


def _render_object(document: Document, obj: BaseObject, outline_text: bool) -> str:
    renderer = SVG_RENDERERS.get(obj.type)
    if renderer is None:
        return _xml_comment(f"unsupported object type for SVG export: {obj.type}")
    return renderer(document, obj, outline_text)


# --------------------------------------------------------------------------
# 公開 API
# --------------------------------------------------------------------------


def document_to_svg(document: Document, outline_text: bool = False) -> str:
    """`Document` を自前シリアライズして SVG 文字列を返す（`scene.render()` は使わない）。

    `outline_text` の既定は False（編集可能な `<text>` のまま出力。投稿規定対応、
    CLAUDE.md §8。2026-08-02 に ON→OFF へ反転）。
    """
    artboard = document.artboard
    width_px = artboard.width_px
    height_px = artboard.height_px
    width_mm = float(artboard.physical.width_mm)
    height_mm = width_mm * (height_px / width_px) if width_px else 0.0

    lines = [
        '<?xml version="1.0" encoding="UTF-8"?>',
        f'<svg xmlns="{_SVG_NS}" xmlns:xlink="{_XLINK_NS}" '
        f'width="{_fmt(width_mm)}mm" height="{_fmt(height_mm)}mm" '
        f'viewBox="0 0 {width_px} {height_px}">',
    ]
    if artboard.background:
        lines.append(
            f'<rect x="0" y="0" width="{width_px}" height="{height_px}"'
            f" fill={quoteattr(artboard.background)}/>"
        )
    for obj in document.objects:
        if not obj.visible:
            continue
        lines.append(_render_object(document, obj, outline_text))
    lines.append("</svg>")
    return "\n".join(lines)


def export_svg(document: Document, path: str, outline_text: bool = False) -> None:
    """SVG をファイルに書き出す（UTF-8）。書込失敗は例外を投げる（握りつぶさない）。"""
    svg = document_to_svg(document, outline_text=outline_text)
    directory = os.path.dirname(path)
    if directory:
        os.makedirs(directory, exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        f.write(svg)
