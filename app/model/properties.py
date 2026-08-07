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


COMMON_PROPS: list[PropSpec] = [
    PropSpec(key="name", label="名前", kind="text"),
    PropSpec(key="x", label="X", kind="number"),
    PropSpec(key="y", label="Y", kind="number"),
    PropSpec(key="width", label="幅", kind="number", minimum=0.0),
    PropSpec(key="height", label="高さ", kind="number", minimum=0.0),
    PropSpec(key="rotation", label="回転", kind="number"),
    PropSpec(
        key="opacity",
        label="不透明度",
        kind="number",
        minimum=0.0,
        maximum=1.0,
        step=0.05,
        decimals=2,
    ),
    PropSpec(key="locked", label="ロック", kind="bool"),
    PropSpec(key="visible", label="表示", kind="bool"),
]

# line は COMMON_PROPS の x/y/width/height/rotation を出さない専用リスト。
_LINE_COMMON_PROPS: list[PropSpec] = [
    PropSpec(key="name", label="名前", kind="text"),
    PropSpec(
        key="opacity",
        label="不透明度",
        kind="number",
        minimum=0.0,
        maximum=1.0,
        step=0.05,
        decimals=2,
    ),
    PropSpec(key="locked", label="ロック", kind="bool"),
    PropSpec(key="visible", label="表示", kind="bool"),
]

_DASH_OPTIONS: tuple[str, ...] = ("solid", "dash", "dot")
_ARROW_OPTIONS: tuple[str, ...] = ("none", "triangle", "open", "circle")

_RECT_PROPS: list[PropSpec] = [
    PropSpec(key="fill", label="塗り", kind="color_opt"),
    PropSpec(key="stroke", label="線色", kind="color"),
    PropSpec(key="stroke_width", label="線幅", kind="number", minimum=0.0),
    PropSpec(key="dash", label="線種", kind="enum", options=_DASH_OPTIONS),
    PropSpec(key="corner_radius", label="角丸半径", kind="number", minimum=0.0),
]

_ELLIPSE_PROPS: list[PropSpec] = [
    PropSpec(key="fill", label="塗り", kind="color_opt"),
    PropSpec(key="stroke", label="線色", kind="color"),
    PropSpec(key="stroke_width", label="線幅", kind="number", minimum=0.0),
    PropSpec(key="dash", label="線種", kind="enum", options=_DASH_OPTIONS),
]

_LINE_PROPS: list[PropSpec] = [
    PropSpec(key="p1", label="始点", kind="point"),
    PropSpec(key="p2", label="終点", kind="point"),
    PropSpec(key="stroke", label="線色", kind="color"),
    PropSpec(key="stroke_width", label="線幅", kind="number", minimum=0.0),
    PropSpec(key="dash", label="線種", kind="enum", options=_DASH_OPTIONS),
    PropSpec(key="arrow_start", label="始端矢じり", kind="enum", options=_ARROW_OPTIONS),
    PropSpec(key="arrow_end", label="終端矢じり", kind="enum", options=_ARROW_OPTIONS),
    PropSpec(key="arrow_size", label="矢じりサイズ", kind="number", minimum=0.0),
]

_IMAGE_PROPS: list[PropSpec] = [
    PropSpec(
        key="brightness",
        label="明るさ",
        kind="number",
        minimum=-1.0,
        maximum=1.0,
        step=0.05,
        decimals=2,
    ),
    PropSpec(
        key="contrast",
        label="コントラスト",
        kind="number",
        minimum=-1.0,
        maximum=1.0,
        step=0.05,
        decimals=2,
    ),
    # SAM3 選択的マスキング（§9.5）。mask_src が付与されているときのみ表示する。
    PropSpec(key="mask_color", label="マスク覆い色", kind="color_opt", requires="mask_src"),
    PropSpec(
        key="mask_opacity",
        label="マスク不透明度",
        kind="number",
        minimum=0.0,
        maximum=1.0,
        step=0.05,
        decimals=2,
        requires="mask_src",
    ),
    PropSpec(key="mask_enabled", label="マスク有効", kind="bool", requires="mask_src"),
]

_ALIGN_OPTIONS: tuple[str, ...] = ("left", "center", "right")
_VALIGN_OPTIONS: tuple[str, ...] = ("top", "middle", "bottom")

_FREEHAND_PROPS: list[PropSpec] = [
    PropSpec(key="stroke", label="線色", kind="color"),
    PropSpec(key="stroke_width", label="線幅", kind="number", minimum=0.0),
    PropSpec(
        key="smoothing",
        label="スムージング",
        kind="number",
        minimum=0.0,
        maximum=1.0,
        step=0.05,
        decimals=2,
    ),
]

_TEXT_PROPS: list[PropSpec] = [
    PropSpec(key="text", label="テキスト", kind="text"),
    PropSpec(key="font_family", label="フォント", kind="text"),
    PropSpec(key="font_size", label="サイズ", kind="number", minimum=0.01),
    PropSpec(key="bold", label="太字", kind="bool"),
    PropSpec(key="italic", label="斜体", kind="bool"),
    PropSpec(key="underline", label="下線", kind="bool"),
    PropSpec(key="color", label="文字色", kind="color"),
    PropSpec(key="align", label="整列", kind="enum", options=_ALIGN_OPTIONS),
    PropSpec(key="valign", label="縦位置", kind="enum", options=_VALIGN_OPTIONS),
]

_MATH_PROPS: list[PropSpec] = [
    PropSpec(key="latex", label="LaTeX", kind="text"),
    PropSpec(key="font_size", label="サイズ", kind="number", minimum=0.01),
    PropSpec(key="color", label="色", kind="color"),
]

_ROUTING_OPTIONS: tuple[str, ...] = ("straight", "orthogonal")

# connector も line 同様、x/y/width/height/rotation を出さない
# （source_id/target_id + アンカーが幾何の真実源、§9.3）。
# アンカー(source_anchor/target_anchor)はパネルの enum ウィジェットではなく
# オンキャンバスのアンカードットで選択する（種類別アンカー契約 §4）。
_CONNECTOR_PROPS: list[PropSpec] = [
    PropSpec(key="routing", label="ルーティング", kind="enum", options=_ROUTING_OPTIONS),
    PropSpec(key="stroke", label="線色", kind="color"),
    PropSpec(key="stroke_width", label="線幅", kind="number", minimum=0.0),
    PropSpec(key="dash", label="線種", kind="enum", options=_DASH_OPTIONS),
    PropSpec(key="arrow_end", label="終端矢じり", kind="enum", options=_ARROW_OPTIONS),
    PropSpec(key="arrow_size", label="矢じりサイズ", kind="number", minimum=0.0),
]

PROPERTIES: dict[str, list[PropSpec]] = {
    "rect": COMMON_PROPS + _RECT_PROPS,
    "ellipse": COMMON_PROPS + _ELLIPSE_PROPS,
    "line": _LINE_COMMON_PROPS + _LINE_PROPS,
    # arrow は create_item で LineItem になり選択可能なため、line と同じ編集項目を共有する。
    "arrow": _LINE_COMMON_PROPS + _LINE_PROPS,
    # crop は crop モード（対話ハンドル）で編集するのでパネルには出さない。
    "image": COMMON_PROPS + _IMAGE_PROPS,
    "freehand": COMMON_PROPS + _FREEHAND_PROPS,
    "text": COMMON_PROPS + _TEXT_PROPS,
    # latex 編集はパネル1行でも可（本格編集はダブルクリックの edit_latex ダイアログ）。
    "math": COMMON_PROPS + _MATH_PROPS,
    # connector は line 同様に専用の共通プロパティリストを使う（§9.3/M6契約 §6）。
    "connector": _LINE_COMMON_PROPS + _CONNECTOR_PROPS,
}
