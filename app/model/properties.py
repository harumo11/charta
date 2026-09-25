"""プロパティパネル駆動データ定義（Qt 非依存）。

`PropSpec` はプロパティパネルが動的にフォームを構築するための仕様。
`PROPERTIES[type]` を辿ってオブジェクト種別ごとの編集項目を得る。
"""

from __future__ import annotations

from dataclasses import dataclass, field


@dataclass(frozen=True)
class PropSpec:
    """1 プロパティの UI 仕様。

    key: オブジェクトの属性名（getattr/setattr で使う）。"p1"/"p2" は point。
    kind: "number"|"int"|"color"|"color_opt"|"enum"|"bool"|"text"|"point"
    color_opt = null 許容色（rect/ellipse の fill）。

    `kind` は**値の型**であり、パネル以外にエージェントのスキーマ生成
    （`app/agent/schema.py`）と値検証（`app/agent/validate.py::coerce`）が読む。
    未知の kind は検証を素通りするため、見た目の違い（フォントのドロップダウン・
    アイコンのトグル）は kind を増やさず `widget` で表す。同じ理由で、
    dataclass のフィールドではない合成キーを `PROPERTIES` に入れてはいけない
    （`schema.properties_for` がそのままエージェントへ書き込み可能キーとして公開する）。
    """

    key: str
    label: str
    kind: str
    minimum: float | None = None
    maximum: float | None = None
    step: float | None = None
    options: tuple[str, ...] = field(default_factory=tuple)
    # None ならパネル側の既定値（小数第1位）を使う。step が 0.1 未満で
    # 既定の丸めだと刻み幅が実質失われる項目（opacity/brightness/contrast/
    # smoothing の step=0.05 等）だけ、ここで明示的に上書きする。
    decimals: int | None = None
    # オブジェクトの当該属性が truthy のときのみパネルに行を表示する
    # （SAM3 選択的マスキング §9.5: mask_src が無い間は色/不透明度/有効行を隠す）。
    requires: str | None = None
    # null 値の表示ラベル（kind == "color_opt" のみ）。mask_color の null は
    # 「なし」ではなく「透明＝切り取り」という操作の意味を持つため（CLAUDE.md §9.5）。
    null_label: str = "なし"
    # 行グループ id（GROUP_* 定数）。パネルは**隣接する可視行の group が変わる位置**
    # に区切り線を入れる（先頭・末尾には入れない）。型をまたいで同じ定数を使うので、
    # 複数選択の共通行（積集合）でも区切りが一貫する。
    group: str = ""
    # 表示ウィジェットの上書き（kind は変えない）:
    #   "font_family" … kind="text" をインストール済みフォントのドロップダウンにする
    #   "toggle"      … kind="bool" をアイコンのトグルボタンにする（icon 必須）
    widget: str | None = None
    # widget="toggle" のアイコン名（qtawesome。例 "mdi6.format-bold"）。
    icon: str | None = None
    # 同じ row id を持つ**連続した** spec を 1 つのフォーム行にまとめる（B/I/U）。
    # 行ラベルは row の先頭 spec の `row_label`。各 spec の `label` はボタンの
    # ツールチップ兼エージェント向けラベルとして残る。
    row: str | None = None
    row_label: str | None = None


# ----------------------------------------------------------------------
# 行グループ（区切り線の単位）。並びは「意味が近いものは近く」（2026-09-25
# ユーザー要望）: 名前 | 位置と形 | 内容（文字・数式） | 見た目（色・線・不透明度）
# | 矢じり | マスク | 状態（表示・ロック）。不透明度は見た目グループの末尾＝色の行の下。
# ----------------------------------------------------------------------
GROUP_NAME = "name"
GROUP_GEOMETRY = "geometry"
GROUP_CONTENT = "content"
GROUP_APPEARANCE = "appearance"
GROUP_ARROWHEADS = "arrowheads"
GROUP_MASK = "mask"
GROUP_STATE = "state"

_DASH_OPTIONS: tuple[str, ...] = ("solid", "dash", "dot")
_ARROW_OPTIONS: tuple[str, ...] = ("none", "triangle", "open", "circle")
_ALIGN_OPTIONS: tuple[str, ...] = ("left", "center", "right")
_VALIGN_OPTIONS: tuple[str, ...] = ("top", "middle", "bottom")
_ROUTING_OPTIONS: tuple[str, ...] = ("straight", "orthogonal")

_NAME: list[PropSpec] = [PropSpec(key="name", label="名前", kind="text", group=GROUP_NAME)]

_BOX_GEOMETRY: list[PropSpec] = [
    PropSpec(key="x", label="X", kind="number", group=GROUP_GEOMETRY),
    PropSpec(key="y", label="Y", kind="number", group=GROUP_GEOMETRY),
    PropSpec(key="width", label="幅", kind="number", minimum=0.0, group=GROUP_GEOMETRY),
    PropSpec(key="height", label="高さ", kind="number", minimum=0.0, group=GROUP_GEOMETRY),
    PropSpec(key="rotation", label="回転", kind="number", group=GROUP_GEOMETRY),
]

_OPACITY: list[PropSpec] = [
    PropSpec(
        key="opacity",
        label="不透明度",
        kind="number",
        minimum=0.0,
        maximum=1.0,
        step=0.05,
        decimals=2,
        group=GROUP_APPEARANCE,
    ),
]

_STATE: list[PropSpec] = [
    PropSpec(key="visible", label="表示", kind="bool", group=GROUP_STATE),
    PropSpec(key="locked", label="ロック", kind="bool", group=GROUP_STATE),
]

# 後方互換の公開名（外部から参照されうる）。box 型の共通部分 = 名前 + 位置と形
# + 不透明度 + 状態。型ごとのリストはこれを直接連結せず、下の _compose で
# 「不透明度を見た目グループの末尾に置く」並びに組み立てる。
COMMON_PROPS: list[PropSpec] = _NAME + _BOX_GEOMETRY + _OPACITY + _STATE


def _compose(
    geometry: list[PropSpec],
    content: list[PropSpec],
    appearance: list[PropSpec],
    tail: list[PropSpec] | None = None,
) -> list[PropSpec]:
    """名前 | 位置と形 | 内容 | 見た目 + 不透明度 | (矢じり/マスク) | 状態 の順に組む。"""
    return _NAME + geometry + content + appearance + _OPACITY + (tail or []) + _STATE


def _stroke_specs(*, nullable: bool, dash: bool = True) -> list[PropSpec]:
    specs = [
        # rect/ellipse/curve の stroke は None（線なし）を許容する（項目12）。
        PropSpec(
            key="stroke",
            label="線色",
            kind="color_opt" if nullable else "color",
            group=GROUP_APPEARANCE,
        ),
        PropSpec(
            key="stroke_width", label="線幅", kind="number", minimum=0.0, group=GROUP_APPEARANCE
        ),
    ]
    if dash:
        specs.append(
            PropSpec(
                key="dash",
                label="線種",
                kind="enum",
                options=_DASH_OPTIONS,
                group=GROUP_APPEARANCE,
            )
        )
    return specs


_FILL: list[PropSpec] = [
    PropSpec(key="fill", label="塗り", kind="color_opt", group=GROUP_APPEARANCE),
]

_ARROW_SIZE = PropSpec(
    key="arrow_size", label="矢じりサイズ", kind="number", minimum=0.0, group=GROUP_ARROWHEADS
)

_RECT_PROPS = _compose(
    geometry=_BOX_GEOMETRY
    + [
        PropSpec(
            key="corner_radius", label="角丸半径", kind="number", minimum=0.0, group=GROUP_GEOMETRY
        )
    ],
    content=[],
    appearance=_FILL + _stroke_specs(nullable=True),
)

_ELLIPSE_PROPS = _compose(
    geometry=_BOX_GEOMETRY,
    content=[],
    appearance=_FILL + _stroke_specs(nullable=True),
)

# line/arrow は x/y/width/height/rotation を出さない（p1/p2 が幾何の真実源）。
_LINE_PROPS = _compose(
    geometry=[
        PropSpec(key="p1", label="始点", kind="point", group=GROUP_GEOMETRY),
        PropSpec(key="p2", label="終点", kind="point", group=GROUP_GEOMETRY),
    ],
    content=[],
    appearance=_stroke_specs(nullable=False),
    tail=[
        PropSpec(
            key="arrow_start",
            label="始端矢じり",
            kind="enum",
            options=_ARROW_OPTIONS,
            group=GROUP_ARROWHEADS,
        ),
        PropSpec(
            key="arrow_end",
            label="終端矢じり",
            kind="enum",
            options=_ARROW_OPTIONS,
            group=GROUP_ARROWHEADS,
        ),
        _ARROW_SIZE,
    ],
)

_IMAGE_PROPS = _compose(
    geometry=_BOX_GEOMETRY,
    content=[],
    appearance=[
        PropSpec(
            key="brightness",
            label="明るさ",
            kind="number",
            minimum=-1.0,
            maximum=1.0,
            step=0.05,
            decimals=2,
            group=GROUP_APPEARANCE,
        ),
        PropSpec(
            key="contrast",
            label="コントラスト",
            kind="number",
            minimum=-1.0,
            maximum=1.0,
            step=0.05,
            decimals=2,
            group=GROUP_APPEARANCE,
        ),
    ],
    # SAM3 選択的マスキング（§9.5）。mask_src が付与されているときのみ表示する。
    tail=[
        PropSpec(
            key="mask_color",
            label="マスク覆い色",
            kind="color_opt",
            requires="mask_src",
            null_label="透明（切り取り）",
            group=GROUP_MASK,
        ),
        PropSpec(
            key="mask_opacity",
            label="マスク不透明度",
            kind="number",
            minimum=0.0,
            maximum=1.0,
            step=0.05,
            decimals=2,
            requires="mask_src",
            group=GROUP_MASK,
        ),
        PropSpec(
            key="mask_enabled",
            label="マスク有効",
            kind="bool",
            requires="mask_src",
            group=GROUP_MASK,
        ),
    ],
)
# crop は crop モード（対話ハンドル）で編集するのでパネルには出さない。

_FREEHAND_PROPS = _compose(
    geometry=_BOX_GEOMETRY
    + [
        PropSpec(
            key="smoothing",
            label="スムージング",
            kind="number",
            minimum=0.0,
            maximum=1.0,
            step=0.05,
            decimals=2,
            group=GROUP_GEOMETRY,
        )
    ],
    content=[],
    appearance=_stroke_specs(nullable=False, dash=False),
)

_TEXT_STYLE_ROW = "text_style"

_TEXT_PROPS = _compose(
    geometry=_BOX_GEOMETRY,
    content=[
        PropSpec(key="text", label="テキスト", kind="text", group=GROUP_CONTENT),
        PropSpec(
            key="font_family",
            label="フォント",
            kind="text",
            widget="font_family",
            group=GROUP_CONTENT,
        ),
        PropSpec(key="font_size", label="サイズ", kind="number", minimum=0.01, group=GROUP_CONTENT),
        PropSpec(
            key="bold",
            label="太字",
            kind="bool",
            widget="toggle",
            icon="mdi6.format-bold",
            row=_TEXT_STYLE_ROW,
            row_label="書式",
            group=GROUP_CONTENT,
        ),
        PropSpec(
            key="italic",
            label="斜体",
            kind="bool",
            widget="toggle",
            icon="mdi6.format-italic",
            row=_TEXT_STYLE_ROW,
            group=GROUP_CONTENT,
        ),
        PropSpec(
            key="underline",
            label="下線",
            kind="bool",
            widget="toggle",
            icon="mdi6.format-underline",
            row=_TEXT_STYLE_ROW,
            group=GROUP_CONTENT,
        ),
        PropSpec(
            key="align", label="整列", kind="enum", options=_ALIGN_OPTIONS, group=GROUP_CONTENT
        ),
        PropSpec(
            key="valign",
            label="縦位置",
            kind="enum",
            options=_VALIGN_OPTIONS,
            group=GROUP_CONTENT,
        ),
    ],
    appearance=[
        PropSpec(key="color", label="文字色", kind="color", group=GROUP_APPEARANCE),
        PropSpec(key="background", label="背景色", kind="color_opt", group=GROUP_APPEARANCE),
    ],
)

_MATH_PROPS = _compose(
    geometry=_BOX_GEOMETRY,
    # latex 編集はパネル1行でも可（本格編集はダブルクリックの edit_latex ダイアログ）。
    content=[
        PropSpec(key="latex", label="LaTeX", kind="text", group=GROUP_CONTENT),
        PropSpec(key="font_size", label="サイズ", kind="number", minimum=0.01, group=GROUP_CONTENT),
    ],
    appearance=[PropSpec(key="color", label="色", kind="color", group=GROUP_APPEARANCE)],
)

# connector も line 同様、x/y/width/height/rotation を出さない
# （source_id/target_id + アンカーが幾何の真実源、§9.3）。
# アンカー(source_anchor/target_anchor)はパネルの enum ウィジェットではなく
# オンキャンバスのアンカードットで選択する（種類別アンカー契約 §4）。
_CONNECTOR_PROPS = _compose(
    geometry=[
        PropSpec(
            key="routing",
            label="ルーティング",
            kind="enum",
            options=_ROUTING_OPTIONS,
            group=GROUP_GEOMETRY,
        ),
    ],
    content=[],
    appearance=_stroke_specs(nullable=False),
    tail=[
        PropSpec(
            key="arrow_end",
            label="終端矢じり",
            kind="enum",
            options=_ARROW_OPTIONS,
            group=GROUP_ARROWHEADS,
        ),
        _ARROW_SIZE,
    ],
)

# points はパネルに出さない（オンキャンバス編集のため。crop と同じ扱い）。
_CURVE_PROPS = _compose(
    geometry=_BOX_GEOMETRY
    + [
        PropSpec(key="closed", label="閉じる", kind="bool", group=GROUP_GEOMETRY),
        PropSpec(
            key="tension",
            label="曲がり",
            kind="number",
            minimum=0.0,
            maximum=1.0,
            step=0.05,
            decimals=2,
            group=GROUP_GEOMETRY,
        ),
    ],
    content=[],
    appearance=_FILL + _stroke_specs(nullable=True),
)

PROPERTIES: dict[str, list[PropSpec]] = {
    "rect": _RECT_PROPS,
    "ellipse": _ELLIPSE_PROPS,
    "line": _LINE_PROPS,
    # arrow は create_item で LineItem になり選択可能なため、line と同じ編集項目を共有する。
    "arrow": _LINE_PROPS,
    "image": _IMAGE_PROPS,
    "freehand": _FREEHAND_PROPS,
    "text": _TEXT_PROPS,
    "math": _MATH_PROPS,
    "connector": _CONNECTOR_PROPS,
    "curve": _CURVE_PROPS,
}
