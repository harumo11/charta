"""選択中オブジェクトのプロパティを編集するパネル（§9.1、P2/P5契約でモード拡張）。

`scene.selectionChanged` を購読し、選択状態に応じて3つのモードでフォームを
動的生成する:

* **object**（単一選択）: `PROPERTIES[obj.type]` から従来どおりのフォームを
  構築する。先頭に整列/分布ボタン行を持つ。
* **artboard**（未選択）: 「未選択」ラベルは廃止し、代わりにアートボード設定
  フォーム（プリセット/幅mm/dpi/幅px/高さpx/背景色）を表示する。
* **multi**（複数選択）: 「N 個選択中」ラベル + 整列/分布ボタン行 +
  選択オブジェクト全型に共通するプロパティの混在表示フォーム。選択がちょうど
  1 つのグループの（ロックされていない）全メンバーなら、X/Y はグループ全体の
  外接矩形の左上を示し、編集するとメンバー全員を同じ差分だけ平行移動する
  （要望10の同じ穴、2026-09-25）。

セクション見出し（「変形」「スタイル」等）は 2026-09-25 のユーザー要望で全廃し、
`PropSpec.group` の変わり目に 1px の区切り線を挿む方式へ移行した（要望6）。
行の生成は `_add_row`/`_register_key` という 1 組のヘルパに一本化されており、
object/multi/artboard の 3 モードが同じ関数を通る。

値変更はすべて `QUndoCommand`（`SetGeometryCommand`/`SetPropertyCommand`/
`SetArtboardCommand`/`SetPropertyWithFollowCommand`/`TranslateGroupCommand`、
いずれも `app/commands/commands.py`）経由でモデルに反映する。色は
`ColorSwatchButton`、フォントは `FontFamilyCombo`（いずれも `app/ui/widgets/`）
に統一されている（要望1/8/11/12）。math/text の box 追従（フォント/latex/本文
変更に伴う寸法変化）は `app/scene/items/box_follow.py::box_follow_geometry`
が唯一の dispatch（エージェント `update_objects`/`apply_style` と共有。
2026-09-25 レビュー2巡目 finding「box-follow logic duplication」対応）。
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import replace
from typing import TYPE_CHECKING, Any

import shiboken6
from PySide6.QtCore import QSize, Qt, QTimer
from PySide6.QtWidgets import (
    QCheckBox,
    QComboBox,
    QDoubleSpinBox,
    QFormLayout,
    QFrame,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QScrollArea,
    QSpinBox,
    QToolButton,
    QVBoxLayout,
    QWidget,
)

from app.commands.commands import (
    SetArtboardCommand,
    SetGeometryCommand,
    SetMultiPropertyCommand,
    SetPropertyCommand,
    SetPropertyWithFollowCommand,
    TranslateGroupCommand,
)
from app.model.document import Artboard, Physical, artboard_with_pixel_size
from app.model.geometry import translate_geom
from app.model.objects import BaseObject
from app.model.palettes import Palette, palette_by_id
from app.model.properties import GROUP_GEOMETRY, PROPERTIES, PropSpec
from app.prefs import Preferences
from app.scene.items.box_follow import box_follow_geometry, followable_keys
from app.ui.artboard_presets import ARTBOARD_CUSTOM_LABEL, ARTBOARD_PRESETS, preset_px_size
from app.ui.theme import current_theme, icons
from app.ui.widgets import ColorSwatchButton, FontFamilyCombo, ScrubLabel, install_wheel_guard

if TYPE_CHECKING:
    from app.scene.canvas_scene import CanvasScene
    from app.ui.controllers.edit_controller import EditController

# x/y/width/height/rotation は SetGeometryCommand、それ以外は SetPropertyCommand（§9.1）。
_GEOMETRY_KEYS = {"x", "y", "width", "height", "rotation"}

_SPIN_RANGE = 1_000_000.0
_MIXED_TEXT = "混在"
# 要件: 数値スピナーの表示・入力は小数第1位まで（int の QSpinBox は対象外）。
# ただし step が 0.1 未満の項目（opacity/brightness/contrast/smoothing、
# properties.py の step=0.05 指定）はこの丸めだと刻み幅が実質失われる
# （例: setValue(0.85)->0.8, stepBy(-1) が 1.0->0.9 になり singleStep と
# 表示が食い違う）ため、PropSpec.decimals で個別に上書きできるようにする。
_NUMBER_DECIMALS = 1

# パネル固定幅（選択対象に依らず一定にする要件への対応）。
#
# 実測手順（必須）: QT_QPA_PLATFORM=offscreen かつ **apply_theme() 適用下**で各種別を選択し
# `panel._form_widget.sizeHint().width()`（フォームの自然な＝圧縮前の幅）の最大値を採る。
# `panel.minimumSizeHint().width()` は使わないこと——フォームを `QScrollArea` で包んで
# 以降、`panel` 自身の minimumSizeHint はスクロール領域の小さな既定値を返すだけになり、
# フォームの実際の幅要求を反映しない。テーマ未適用で測ると QSS の padding とアプリ
# フォント（Noto Sans 10pt）が乗らず 40px 以上小さい値が出る。
_PANEL_FIXED_WIDTH = 340

# QFormLayout の行間隔。Fusion 既定と同値を明示することで、将来スタイルが変わっても
# ここが唯一の真実源になる（Fusion 既定に暗黙に委ねない）。
_FORM_V_SPACING = 6
_FORM_H_SPACING = 12

# 整列/分布ボタン行: (アイコン名, ツールチップ, align_selected/distribute_selected へ
# 渡すモード文字列)。
_ALIGN_ACTIONS: tuple[tuple[str, str, str], ...] = (
    ("mdi6.align-horizontal-left", "左揃え", "left"),
    ("mdi6.align-horizontal-center", "水平方向中央揃え", "center_h"),
    ("mdi6.align-horizontal-right", "右揃え", "right"),
    ("mdi6.align-vertical-top", "上揃え", "top"),
    ("mdi6.align-vertical-center", "垂直方向中央揃え", "center_v"),
    ("mdi6.align-vertical-bottom", "下揃え", "bottom"),
)
_DISTRIBUTE_ACTIONS: tuple[tuple[str, str, str], ...] = (
    ("mdi6.distribute-horizontal-center", "水平方向に分布", "h"),
    ("mdi6.distribute-vertical-center", "垂直方向に分布", "v"),
)
_ALIGN_BUTTON_SIZE = 24
_ALIGN_ICON_SIZE = 16

# multi モードで対象にする kind（point は対象外）。text は widget=="font_family"
# のときだけ例外的に許可する（`_multi_common_specs` 参照）。
_MULTI_KINDS = frozenset({"number", "int", "color", "color_opt", "enum", "bool"})

#: color と color_opt は「色を選ぶ」という操作としては互換（複数選択で
#: rect+line の「線色」行を消さないための互換扱い）。
_COLOR_KINDS = frozenset({"color", "color_opt"})

# artboard モードの行グループ（見出し廃止に伴う区切り線の単位、要望6）。
_GROUP_ARTBOARD_SIZE = "artboard_size"
_GROUP_ARTBOARD_BG = "artboard_bg"

# アートボードの背景色スウォッチの null_label（背景は常に非 null なので実際には
# メニューに現れないが、ColorSwatchButton の共通引数として渡す）。
_ARTBOARD_BG_NULL_LABEL = "なし"

_TOGGLE_ICON_SIZE = 18


def _pin_control_height(widget: QWidget) -> None:
    """スピン/チェックボックスの高さを `control_h` に固定する。

    Fusion + QSS の `min-height` は、実測では「その時点でウィジェットに
    割り当てられている幅」によって最終的な計算結果が変わることが確認できた
    （point 行のように幅が圧縮されたり、縦スクロールバーの出現/消失で
    ビューポート幅が数px 増減したりするたびに、外形高さがばらついた）。
    QSS の係数だけでは「幅が変わるたびに再び崩れる」問題を構造的に解決できない
    ため、レイアウト API（`setFixedHeight`）で高さそのものを幅に依存しない値へ
    固定する。ウィジェット単位の `setStyleSheet` ではないため「原則禁止」規約
    には抵触しない。QSS 側の min-height はこれと矛盾しない下限として残してある。
    """
    widget.setFixedHeight(current_theme().control_h)


def _color_eq(a: str | None, b: str | None) -> bool:
    """色の同値判定（大文字小文字を無視する）。

    `ColorSwatchButton` は常に大文字 hex を発火するが、モデルの既存値
    （palette/initial_color/apply_style 由来）は小文字のこともあるため、
    同じ色の再選択で無駄な undo エントリが積まれないようにする。
    """
    an = a.upper() if a else None
    bn = b.upper() if b else None
    return an == bn


def _binding_closure(document: Any, obj: BaseObject) -> set[int]:
    """`obj` から接着チェーンを辿って到達できる id の集合（`obj.id` 自身は
    含めない。到達不能なら空集合）。

    `app.graphics.routing.binding_reaches` は「特定の1点へ到達できるか」を
    bool で返す判定専用で、「チェーンがどこまで広がるか」を集合として返さない。
    `_add_group_xy_rows.group_origin` の「グループ外接着」判定は一段先だけでは
    不十分（メンバー同士が接着し、その先がグループ外、という多段の鎖で誤判定
    する。レビュー2巡目 finding #2）ため、`binding_reaches` と同じ
    `binding_slots` だけを辿る同型の閉包版をここに置く（`app/graphics/routing.py`
    はこのレビュー対応の担当割り当てで編集権限が無い。model 層のみに依存し、
    Qt には触れない）。
    """
    from app.model.objects import binding_slots

    visited: set[int] = set()
    reachable: set[int] = set()
    stack = [obj.id]
    while stack:
        current = stack.pop()
        if current in visited:
            continue
        visited.add(current)
        cur_obj = document.object_by_id(current)
        if cur_obj is None:
            continue
        for id_key, _anchor_key, _point_key in binding_slots(cur_obj.type):
            next_id = getattr(cur_obj, id_key)
            if next_id is not None and next_id != cur_obj.id:
                reachable.add(next_id)
                stack.append(next_id)
    return reachable


class _MixedDoubleSpinBox(QDoubleSpinBox):
    """混在表示に対応した `QDoubleSpinBox`（multi モード・number 用）。

    `spin.lineEdit().setText("混在")` は仕様書どおりの手法だが、実機で確認した
    ところ `QAbstractSpinBox` は show/polish イベントの際に内部 `value()` から
    表示テキストを再構築するため、パネルが実際に画面へ出た瞬間（＝複数選択の
    実運用そのもの）にフォーマット済み数値（例: "0.00"）へ巻き戻ってしまう
    （processEvents 1 回で再現するバグとして実測確認済み）。`textFromValue()`
    をオーバーライドして Qt 自身に「混在」を返させることで、再フォーマットの
    発生タイミングに依らず安定させる。
    """

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.mixed = False

    def textFromValue(self, value: float) -> str:  # noqa: N802 (Qt override)
        if self.mixed:
            return _MIXED_TEXT
        return super().textFromValue(value)


class _MixedSpinBox(QSpinBox):
    """int 版の `_MixedDoubleSpinBox`（同上）。"""

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.mixed = False

    def textFromValue(self, value: int) -> str:  # noqa: N802 (Qt override)
        if self.mixed:
            return _MIXED_TEXT
        return super().textFromValue(value)


class PropertyPanel(QWidget):
    """選択状態（object/artboard/multi）に応じてフォームを切り替えるパネル。"""

    def __init__(
        self,
        scene: CanvasScene,
        edit: EditController | None = None,
        parent: QWidget | None = None,
        prefs: Preferences | None = None,
    ) -> None:
        super().__init__(parent)
        self.scene = scene
        # 整列/分布ボタンの実行先。None の場合は整列行を常に非表示にする
        # （既存テストが `PropertyPanel(scene)` で構築するための後方互換）。
        self._edit = edit
        self._rebuilding = False

        # 色スウォッチのメニュー・ダイアログに使うパレット（環境設定、要望8/11/12）。
        # `prefs` を渡さない構築（既存テスト互換）では基本色のみになる。
        self._prefs = prefs
        self._palette: Palette | None = (
            palette_by_id(prefs.palette_id) if prefs is not None else None
        )
        # 現在のフォームに存在する全 ColorSwatchButton（`refresh_palette` が
        # 環境設定変更後にパレットを配り直すための台帳）。
        self._color_swatches: list[ColorSwatchButton] = []

        # 現在フォーム表示中の対象種別（in-place 更新の可否判定用）。
        # "object"/"artboard"/"multi" のいずれか（初期状態は None）。
        self._current_mode: str | None = None
        self._current_obj_id: int | None = None
        self._current_type: str | None = None
        # PropSpec.requires を持つ行の表示可否状態のスナップショット
        # （mask_src の付与/解除等で requires 対象が truthy/falsy を跨いだ
        # 場合にフォームを再構築するための判定に使う）。
        self._current_requires_state: tuple[bool, ...] | None = None
        # multi モードの対象識別（選択 id 集合。id順にソートして比較する）。
        self._current_multi_ids: tuple[int, ...] | None = None
        # multi モードのとき「選択がちょうど1つのグループの全メンバー」かどうかの
        # 判定結果（`_whole_group_selection` の戻り値。None なら通常の複数選択）。
        # グループ化（Ctrl+G）/解除/その undo/redo・エージェント order_objects の
        # group/ungroup は選択 id 集合そのものを変えないため、`_current_multi_ids`
        # だけを識別キーにすると「同じ選択のまま」判定でフォームが古いまま残り、
        # グループ化直後に X を書くとメンバーが同じ座標へ収束してグループが崩れる
        # （findings #1/#10）。これも識別に含めて変化を検出する。
        self._current_multi_group_id: int | None = None
        # 各ウィジェット生成時に登録する「モデル値→ウィジェットへ反映する」updater。
        # undo/redo 等で対象が変わらないまま値だけ変わった場合、フォームを
        # 破棄せずこれらを呼ぶだけで再同期する（§M8クラッシュ修正、下記参照）。
        self._updaters: list[Callable[[], None]] = []
        # このパネル自身が push() 中かどうかの深さ（ネスト対応の安全のため int）。
        # locked/visible の編集は sync_item() 内で item の
        # ItemIsSelectable/visible フラグを変えるため、Qt が当該 item を
        # 自動的に選択解除し、redo() の途中で scene.selectionChanged が
        # 同期的に（この push() 呼び出しの中で）発火し得る。そのタイミングで
        # 破壊的な _clear_form/_build_form を行うと「いま toggled を発火中の
        # チェックボックス自身」を破棄してしまう（実測でクラッシュ確認済み）。
        # push_depth > 0 の間は破壊的パスを _schedule_deferred_resync() に
        # 委譲し、push() が完全に戻ってから安全に再評価する。
        self._push_depth = 0
        self._resync_pending = False

        # 行台帳（公開ヘルパの裏付け。見出し廃止・row まとめ・グループ区切りの
        # 導入により「PROPERTIES[type] の並び順 == QFormLayout の行番号」という
        # 前提が崩れたため、key → 実際の行/ウィジェットを明示的に記録する）。
        self._row_keys: dict[str, int] = {}
        self._field_widgets: dict[str, QWidget] = {}
        self._label_widgets: dict[str, QWidget] = {}
        self._key_group: dict[str, str] = {}
        self._ordered_keys: list[str] = []
        self._separator_rows: list[int] = []
        self._last_group: str | None = None
        self._has_rows = False

        # QSS の `#propertyPanel` スコープ選択子（app/ui/theme/qss.py）がこのパネル
        # 自体を識別する鍵（将来パネル全体の見た目を扱う規則のため）。
        self.setObjectName("propertyPanel")

        self._align_row = self._build_align_row()
        self._info_label = QLabel("")
        self._form = QFormLayout()
        self._form_widget = QWidget()
        # 行高・スウォッチ幅の QSS 規則（`#propertyPanelForm ...`）は `self`
        # ではなくこちらをスコープ鍵にする（ダイアログ等の子孫まで拾わないため）。
        self._form_widget.setObjectName("propertyPanelForm")
        self._form_widget.setLayout(self._form)
        self._configure_form()

        # フォームをスクロール領域に包む（ドックがフォームの sizeHint 未満の
        # 高さしか与えないとき——1366×768/1200×900 のような常用ウィンドウで
        # text/rect フォームは容易にこれを超える——QVBoxLayout は `_form_widget`
        # を QSS の min-height 未満まで圧縮してしまい、行高統一・ラベル/入力欄の
        # 垂直中心一致が崩れる。`setWidgetResizable(True)` で「ビューポートより
        # 小さければビューポートまで広げ、`_form_widget.minimumSizeHint()` より
        # 小さくはしない」という Qt 標準の挙動に任せることで、縦方向は自然な
        # sizeHint を尊重してスクロールに逃がす（どんなドック高でも行は常に
        # control_h を保つ）。横方向はスピンに `setMinimumWidth` の下限を
        # 設けてあるため、フォーム全体の最小幅がパネル固定幅(340px)を上回らず、
        # 横スクロールバー（AlwaysOff）を出さなくても見切れない。
        self._form_scroll = QScrollArea()
        self._form_scroll.setObjectName("propertyPanelFormScroll")
        self._form_scroll.setWidget(self._form_widget)
        self._form_scroll.setWidgetResizable(True)
        self._form_scroll.setFrameShape(QFrame.Shape.NoFrame)
        self._form_scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        self._form_scroll.setVerticalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAsNeeded)

        layout = QVBoxLayout(self)
        layout.addWidget(self._align_row)
        layout.addWidget(self._info_label)
        layout.addWidget(self._form_scroll, 1)

        # 選択オブジェクト種別ごとにフォーム内容の幅が変わっても QDockWidget が
        # 追従リサイズしないよう、パネル自体を固定幅にする（要件1）。
        self.setFixedWidth(_PANEL_FIXED_WIDTH)

        self.scene.selectionChanged.connect(self.on_selection_changed)
        self.on_selection_changed()

    def minimumSizeHint(self) -> QSize:  # noqa: N802 (Qt override)
        """縦方向の強制最小サイズは小さく保つ（幅は `setFixedWidth` で固定済み）。

        artboard モード（プリセット/mm/dpi/px×2/背景色）や object モードの
        整列行を足すと自然な `sizeHint` の高さは大きくなる。これをそのまま
        `minimumSizeHint` として右ドックの `QSplitter` に渡すと、ウィンドウを
        縮小できる下限がその分だけ底上げされ、極端に小さい画面（オフスクリーンの
        テスト環境等）で `MainWindow._resize_window_to_fit` が想定するチロム
        （window - viewport）と実際の値がずれてしまう。通常サイズのウィンドウ
        では十分な余裕があるため見た目には影響しない（Qt はレイアウトに空きが
        ある限り `sizeHint`＝プリファードサイズで描画し、`minimumSizeHint` は
        本当に空間が足りないときの床にすぎない）。
        """
        hint = super().minimumSizeHint()
        return QSize(hint.width(), 1)

    def sizeHint(self) -> QSize:  # noqa: N802 (Qt override)
        """フォームの自然な幅を反映する（`_form_scroll` の幅上乗せを打ち消す）。

        `QScrollArea.sizeHint()` は縦スクロールバー用の余白を常に見込むため、
        埋め込みウィジェット（`_form_widget`）自身の sizeHint 幅より広い値を
        返す（実測: 内容 316px に対し 329px）。これが `panel.sizeHint()`
        （`panel.adjustSize()` が使う）を押し上げ、line/arrow のような
        幅広フォームで `_PANEL_FIXED_WIDTH` を余分に超えてしまう。

        `_FormScrollArea` のようなサブクラスで `sizeHint()` 自体を上書きする
        案も試したが、`QLayout` が `QWidgetItem` 経由で C++ 側から呼ぶ経路
        では反映されないことを実測で確認した（Python から直接呼べば上書きは
        効くが、レイアウトの `totalSizeHint()` 計算はこの経路を通らない）。
        そのため、最終的な合計を返す**この関数**で埋め込みウィジェットの
        sizeHint を直接読み、差分だけ補正する（Python から直接呼ぶので
        上記の制約を受けない）。
        """
        hint = super().sizeHint()
        overshoot = self._form_scroll.sizeHint().width() - self._form_widget.sizeHint().width()
        if overshoot > 0:
            return QSize(hint.width() - overshoot, hint.height())
        return hint

    def _configure_form(self) -> None:
        """行の整列規約を明示する（Fusion 既定に委ねない）。

        既定では labelAlignment に垂直フラグが無く、行がフィールドより高いとき
        フィールドが上寄せされる。AlignVCenter を明示し、行の高さは QSS の
        min-height（Theme.control_h）で一定にすることで、どの kind の行でも
        ラベル文字と入力欄の垂直中心が一致する。
        """
        self._form.setLabelAlignment(Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter)
        self._form.setFormAlignment(Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignTop)
        self._form.setFieldGrowthPolicy(QFormLayout.FieldGrowthPolicy.AllNonFixedFieldsGrow)
        self._form.setRowWrapPolicy(QFormLayout.RowWrapPolicy.DontWrapRows)
        self._form.setVerticalSpacing(_FORM_V_SPACING)
        self._form.setHorizontalSpacing(_FORM_H_SPACING)

    # ------------------------------------------------------------------
    # パレット（要望8/11/12）
    # ------------------------------------------------------------------

    def refresh_palette(self) -> None:
        """`self._prefs.palette_id` を読み直し、表示中の全スウォッチへ配り直す。

        環境設定ダイアログの確定後に `MainWindow` から呼ばれる（新しいパレットの
        色が既存フォームの色メニューへ即座に反映されるようにするため）。
        """
        self._palette = palette_by_id(self._prefs.palette_id) if self._prefs is not None else None
        for button in self._color_swatches:
            if shiboken6.isValid(button):
                button.set_palette(self._palette)

    # ------------------------------------------------------------------
    # 整列/分布ボタン行
    # ------------------------------------------------------------------

    def _build_align_row(self) -> QWidget:
        container = QWidget()
        hlayout = QHBoxLayout(container)
        hlayout.setContentsMargins(0, 4, 0, 4)
        hlayout.setSpacing(2)

        self._align_buttons: list[QToolButton] = []
        for icon_name, tooltip, mode in _ALIGN_ACTIONS:
            button = self._make_toolbutton(icon_name, tooltip)
            button.clicked.connect(lambda _checked=False, m=mode: self._on_align_clicked(m))
            hlayout.addWidget(button)
            self._align_buttons.append(button)

        hlayout.addSpacing(6)

        self._distribute_buttons: list[QToolButton] = []
        for icon_name, tooltip, axis in _DISTRIBUTE_ACTIONS:
            button = self._make_toolbutton(icon_name, tooltip)
            button.clicked.connect(lambda _checked=False, a=axis: self._on_distribute_clicked(a))
            hlayout.addWidget(button)
            self._distribute_buttons.append(button)

        hlayout.addStretch(1)
        return container

    @staticmethod
    def _make_toolbutton(icon_name: str, tooltip: str) -> QToolButton:
        button = QToolButton()
        button.setIcon(icons.icon(icon_name))
        button.setIconSize(QSize(_ALIGN_ICON_SIZE, _ALIGN_ICON_SIZE))
        button.setFixedSize(_ALIGN_BUTTON_SIZE, _ALIGN_BUTTON_SIZE)
        button.setToolTip(tooltip)
        return button

    def _on_align_clicked(self, mode: str) -> None:
        if self._edit is not None:
            self._edit.align_selected(mode)

    def _on_distribute_clicked(self, axis: str) -> None:
        if self._edit is not None:
            self._edit.distribute_selected(axis)

    def _set_align_row_visible(self, visible: bool) -> None:
        self._align_row.setVisible(visible and self._edit is not None)

    def _update_align_enabled(self, count: int) -> None:
        """整列=選択2個以上、分布=3個以上で有効化する。"""
        for button in self._align_buttons:
            button.setEnabled(count >= 2)
        for button in self._distribute_buttons:
            button.setEnabled(count >= 3)

    # ------------------------------------------------------------------
    # 選択変更・モード判定
    # ------------------------------------------------------------------

    def on_selection_changed(self) -> None:
        """選択に応じてフォームを更新する。

        `undo_stack.indexChanged` はパネル自身が push した編集の redo でも
        発火するため、この slot は「フォーム編集の真っ最中に自分自身を
        呼び出す」再入を起こし得る。対象（モード・id/type や選択集合）が
        変わっていなければ `_refresh_values()` で値だけを signals ブロック
        しつつ再設定し、フォームは破棄しない（`_clear_form`/`_build_form` は
        ウィジェットを removeRow で破棄するため、いま valueChanged 等を発火中
        のウィジェット自身を破棄すると use-after-free でクラッシュする —
        実際に発生していたバグ）。

        対象が変わった場合（モード遷移・別オブジェクト選択・選択集合変化）は
        フォーム再構築が必須だが、`self._push_depth > 0`（このパネル自身の
        push() 実行中の再入）であれば `_schedule_deferred_resync()` に委譲し、
        push() が完全に戻ってから安全に再構築する。
        """
        if not shiboken6.isValid(self) or not shiboken6.isValid(self.scene):
            return
        selected = self.scene.selected_objects()

        if len(selected) == 1:
            obj = selected[0]
            if (
                self._current_mode == "object"
                and obj.id == self._current_obj_id
                and obj.type == self._current_type
                and self._requires_state(obj) == self._current_requires_state
            ):
                self._update_align_enabled(1)
                self._refresh_values()
                return
        elif len(selected) >= 2:
            ids = tuple(sorted(o.id for o in selected))
            group_id = self._whole_group_selection(selected)
            if (
                self._current_mode == "multi"
                and ids == self._current_multi_ids
                and group_id == self._current_multi_group_id
            ):
                self._update_align_enabled(len(selected))
                self._refresh_values()
                return
        else:
            if self._current_mode == "artboard":
                self._refresh_values()
                return

        if self._push_depth > 0:
            self._schedule_deferred_resync()
            return

        self._apply_selection_state(selected)

    def _apply_selection_state(self, selected: list[BaseObject]) -> None:
        """フォームを（必要なら）破棄して選択状態どおりに再構築する。"""
        if len(selected) == 1:
            obj = selected[0]
            self._current_mode = "object"
            self._current_obj_id = obj.id
            self._current_type = obj.type
            self._current_requires_state = self._requires_state(obj)
            self._current_multi_ids = None
            self._current_multi_group_id = None
            self._clear_form()
            self._build_form(obj)
            self._info_label.hide()
            self._form_widget.show()
            self._set_align_row_visible(True)
            self._update_align_enabled(1)
            return

        if len(selected) >= 2:
            self._current_mode = "multi"
            self._current_obj_id = None
            self._current_type = None
            self._current_requires_state = None
            self._current_multi_ids = tuple(sorted(o.id for o in selected))
            self._current_multi_group_id = self._whole_group_selection(selected)
            self._clear_form()
            self._build_multi_form(selected, group_id=self._current_multi_group_id)
            self._info_label.setText(f"{len(selected)} 個選択中")
            self._info_label.show()
            self._form_widget.show()
            self._set_align_row_visible(True)
            self._update_align_enabled(len(selected))
            return

        # 未選択 = artboard モード（表示上は「未選択」ラベルをアートボード設定
        # フォームに置き換えて廃止する）。ラベル自体のテキストは互換のため
        # "未選択" のまま保持しつつ非表示にする（`tests/test_panel_edit_m8.py`
        # が `_info_label.text() == "未選択"` を検証している）。
        self._current_mode = "artboard"
        self._current_obj_id = None
        self._current_type = None
        self._current_requires_state = None
        self._current_multi_ids = None
        self._current_multi_group_id = None
        self._clear_form()
        self._build_artboard_form()
        self._info_label.setText("未選択")
        self._info_label.hide()
        self._form_widget.show()
        self._set_align_row_visible(False)

    def _schedule_deferred_resync(self) -> None:
        """push() 完了後（次のイベントループ）に一度だけ再評価を予約する。"""
        if self._resync_pending:
            return
        self._resync_pending = True
        QTimer.singleShot(0, self._run_deferred_resync)

    def _run_deferred_resync(self) -> None:
        self._resync_pending = False
        if not shiboken6.isValid(self) or not shiboken6.isValid(self.scene):
            return
        self.on_selection_changed()

    @staticmethod
    def _requires_state(obj: BaseObject) -> tuple[bool, ...]:
        """`PropSpec.requires` を持つ行それぞれの現在の表示可否を並べたタプル。

        これが変化した（例: mask_src の付与/解除）場合はフォームの行構成
        自体が変わるため、`on_selection_changed` は in-place 更新ではなく
        フォーム再構築を行う判定に使う。
        """
        specs = PROPERTIES.get(obj.type, [])
        return tuple(bool(getattr(obj, s.requires, None)) for s in specs if s.requires is not None)

    def _refresh_values(self) -> None:
        """フォームを再構築せず、登録済み updater で現在値を再設定する。"""
        for updater in list(self._updaters):
            updater()

    def _clear_form(self) -> None:
        while self._form.rowCount() > 0:
            self._form.removeRow(0)
        self._row_keys = {}
        self._field_widgets = {}
        self._label_widgets = {}
        self._key_group = {}
        self._ordered_keys = []
        self._separator_rows = []
        self._last_group = None
        self._has_rows = False
        self._color_swatches = []

    # ------------------------------------------------------------------
    # 行生成の共有ヘルパ（object/multi/artboard の3モードが通る単一の経路。要望6）
    # ------------------------------------------------------------------

    def _make_separator(self) -> QWidget:
        """グループの変わり目に挿む 1px の区切り線（要望6）。

        `#headerSep`（HeaderBar の縦区切り）と同じ流儀: ウィジェット単位の
        `setStyleSheet` ではなく QSS（`#propertyPanelForm #propertyPanelSeparator`）
        で色を与える。
        """
        sep = QFrame()
        sep.setObjectName("propertyPanelSeparator")
        sep.setFrameShape(QFrame.Shape.NoFrame)
        sep.setFixedHeight(1)
        sep.setAttribute(Qt.WidgetAttribute.WA_StyledBackground, True)
        return sep

    def _add_row(self, group: str, label_widget: QWidget | None, field_widget: QWidget) -> int:
        """1 行を追加し、必要なら直前にグループ区切りを挿む。行番号(FieldRole)を返す。

        区切りは「直前に emit した行の group」と `group` が異なるときだけ挿む
        （先頭には挿まない＝`self._has_rows` が False の間は挿まない）。
        `requires` で非表示の行は呼び出し側がそもそも渡さないため、判定は
        自然に「隣接する可視行」基準になる。
        """
        if self._has_rows and group != self._last_group:
            self._form.addRow(self._make_separator())
            self._separator_rows.append(self._form.rowCount() - 1)
        row = self._form.rowCount()
        if label_widget is None:
            self._form.addRow(field_widget)
        else:
            self._form.addRow(label_widget, field_widget)
        self._last_group = group
        self._has_rows = True
        return row

    def _register_key(
        self,
        key: str,
        row: int,
        field_widget: QWidget,
        label_widget: QWidget,
        group: str,
    ) -> None:
        self._row_keys[key] = row
        self._field_widgets[key] = field_widget
        self._label_widgets[key] = label_widget
        self._key_group[key] = group
        self._ordered_keys.append(key)

    def _track_color_swatch(self, button: ColorSwatchButton) -> ColorSwatchButton:
        button.set_palette(self._palette)
        self._color_swatches.append(button)
        return button

    # ------------------------------------------------------------------
    # 公開ヘルパ（key から実際の行/ウィジェットを引く経路。テストと外部から
    # はこの経路を使うこと）
    # ------------------------------------------------------------------

    def row_for_key(self, key: str) -> int:
        """`key` に対応する `QFormLayout` の行番号（FieldRole 側）を返す。"""
        try:
            return self._row_keys[key]
        except KeyError:
            available = ", ".join(sorted(self._row_keys)) or "(none)"
            raise KeyError(f"row not found for key {key!r}; available keys: {available}") from None

    def field_widget_for(self, key: str) -> QWidget:
        """`key` のフィールド側ウィジェットを返す（区切り線を返すことはない）。

        B/I/U のように複数 key が 1 行を共有する場合でも、各 key は自分専用の
        ウィジェット（個別の `QToolButton`）を返す。
        """
        try:
            return self._field_widgets[key]
        except KeyError:
            available = ", ".join(sorted(self._field_widgets)) or "(none)"
            raise RuntimeError(
                f"field widget not found for key {key!r}; available keys: {available}"
            ) from None

    def label_widget_for(self, key: str) -> QWidget:
        """`key` のラベル側ウィジェットを返す。"""
        try:
            return self._label_widgets[key]
        except KeyError:
            available = ", ".join(sorted(self._label_widgets)) or "(none)"
            raise RuntimeError(
                f"label widget not found for key {key!r}; available keys: {available}"
            ) from None

    def keys_in_form(self) -> tuple[str, ...]:
        """現在のフォームの表示順（`requires` で隠れた行は含まない）。"""
        return tuple(self._ordered_keys)

    def separator_rows(self) -> tuple[int, ...]:
        """区切り線行の `QFormLayout` 行番号を表示順に返す（要望6）。"""
        return tuple(self._separator_rows)

    def groups_in_form(self) -> tuple[tuple[str, tuple[str, ...]], ...]:
        """(group id, その group に属する key の列) を表示順に返す（要望6）。"""
        result: list[tuple[str, tuple[str, ...]]] = []
        current_group: str | None = None
        current_keys: list[str] = []
        for key in self._ordered_keys:
            group = self._key_group[key]
            if group != current_group:
                if current_group is not None:
                    result.append((current_group, tuple(current_keys)))
                current_group = group
                current_keys = []
            current_keys.append(key)
        if current_group is not None:
            result.append((current_group, tuple(current_keys)))
        return tuple(result)

    # ------------------------------------------------------------------
    # object モード: フォーム構築（PROPERTIES 駆動）
    # ------------------------------------------------------------------

    def _make_label_widget(self, spec: PropSpec, field_widget: QWidget) -> QWidget:
        """number/int/point 行のラベルは ScrubLabel にする。

        point 行（field_widget は x/y 2 個の QDoubleSpinBox を持つ横並び
        コンテナ）は、先頭（x側）のスピンボックスをスクラブ対象にする。
        """
        if spec.kind in ("number", "int"):
            return ScrubLabel(spec.label, field_widget)
        if spec.kind == "point":
            spin_x = field_widget.findChildren(QDoubleSpinBox)[0]
            return ScrubLabel(spec.label, spin_x)
        return QLabel(spec.label)

    def _build_form(self, obj: BaseObject) -> None:
        self._rebuilding = True
        self._updaters = []
        try:
            specs = [
                s
                for s in PROPERTIES.get(obj.type, [])
                if s.requires is None or getattr(obj, s.requires, None)
            ]
            i = 0
            while i < len(specs):
                spec = specs[i]
                if spec.row is not None:
                    row_specs = [spec]
                    j = i + 1
                    while j < len(specs) and specs[j].row == spec.row:
                        row_specs.append(specs[j])
                        j += 1
                    i = j
                    label_widget: QWidget = QLabel(spec.row_label or spec.label)
                    field_widget, per_key = self._make_toggle_row_widget(obj, row_specs)
                    row = self._add_row(spec.group, label_widget, field_widget)
                    for row_spec in row_specs:
                        self._register_key(
                            row_spec.key, row, per_key[row_spec.key], label_widget, row_spec.group
                        )
                    continue
                widget = self._make_widget(obj, spec)
                label_widget = self._make_label_widget(spec, widget)
                row = self._add_row(spec.group, label_widget, widget)
                self._register_key(spec.key, row, widget, label_widget, spec.group)
                i += 1
        finally:
            self._rebuilding = False

    # ------------------------------------------------------------------
    # ウィジェット生成（kind ごと、object モード用）
    # ------------------------------------------------------------------

    def _make_widget(self, obj: BaseObject, spec: PropSpec) -> QWidget:
        if spec.kind == "number":
            return self._make_number_widget(obj, spec)
        if spec.kind == "int":
            return self._make_int_widget(obj, spec)
        if spec.kind == "point":
            return self._make_point_widget(obj, spec)
        if spec.kind in ("color", "color_opt"):
            return self._make_color_widget(obj, spec)
        if spec.kind == "enum":
            return self._make_enum_widget(obj, spec)
        if spec.kind == "bool":
            return self._make_bool_widget(obj, spec)
        if spec.kind == "text":
            if spec.widget == "font_family":
                return self._make_font_family_widget(obj, spec)
            return self._make_text_widget(obj, spec)
        raise NotImplementedError(f"unknown PropSpec.kind: {spec.kind!r}")

    def _make_number_widget(self, obj: BaseObject, spec: PropSpec) -> QDoubleSpinBox:
        spin = QDoubleSpinBox()
        _pin_control_height(spin)
        install_wheel_guard(spin)
        spin.setKeyboardTracking(False)
        spin.setDecimals(spec.decimals if spec.decimals is not None else _NUMBER_DECIMALS)
        spin.setMinimum(spec.minimum if spec.minimum is not None else -_SPIN_RANGE)
        spin.setMaximum(spec.maximum if spec.maximum is not None else _SPIN_RANGE)
        spin.setSingleStep(spec.step if spec.step is not None else 1.0)
        spin.blockSignals(True)
        spin.setValue(float(getattr(obj, spec.key)))
        spin.blockSignals(False)

        def on_changed(value: float, obj: BaseObject = obj, spec: PropSpec = spec) -> None:
            if self._rebuilding:
                return
            old_value = getattr(obj, spec.key)
            if value == old_value:
                return
            self._commit_scalar(obj, spec.key, value, old_value)

        spin.valueChanged.connect(on_changed)

        def update_widget(
            spin: QDoubleSpinBox = spin, obj: BaseObject = obj, spec: PropSpec = spec
        ) -> None:
            if not shiboken6.isValid(spin):
                return
            spin.blockSignals(True)
            try:
                spin.setValue(float(getattr(obj, spec.key)))
            finally:
                spin.blockSignals(False)

        self._updaters.append(update_widget)
        return spin

    def _make_int_widget(self, obj: BaseObject, spec: PropSpec) -> QSpinBox:
        spin = QSpinBox()
        _pin_control_height(spin)
        install_wheel_guard(spin)
        spin.setKeyboardTracking(False)
        spin.setMinimum(int(spec.minimum) if spec.minimum is not None else int(-_SPIN_RANGE))
        spin.setMaximum(int(spec.maximum) if spec.maximum is not None else int(_SPIN_RANGE))
        spin.setSingleStep(int(spec.step) if spec.step is not None else 1)
        spin.blockSignals(True)
        spin.setValue(int(getattr(obj, spec.key)))
        spin.blockSignals(False)

        def on_changed(value: int, obj: BaseObject = obj, spec: PropSpec = spec) -> None:
            if self._rebuilding:
                return
            old_value = getattr(obj, spec.key)
            if value == old_value:
                return
            self._commit_scalar(obj, spec.key, value, old_value)

        spin.valueChanged.connect(on_changed)

        def update_widget(
            spin: QSpinBox = spin, obj: BaseObject = obj, spec: PropSpec = spec
        ) -> None:
            if not shiboken6.isValid(spin):
                return
            spin.blockSignals(True)
            try:
                spin.setValue(int(getattr(obj, spec.key)))
            finally:
                spin.blockSignals(False)

        self._updaters.append(update_widget)
        return spin

    def _make_point_widget(self, obj: BaseObject, spec: PropSpec) -> QWidget:
        container = QWidget()
        # 行高統一の対象は `field_widget_for("p1")` が返すこのコンテナ自身
        # （内側のスピン2個は個別に `_pin_control_height` 済み）。
        _pin_control_height(container)
        hlayout = QHBoxLayout(container)
        hlayout.setContentsMargins(0, 0, 0, 0)
        # ラベル("x"/"y")とスピンの間隔を明示する（Fusion 既定の詰まり方に
        # 委ねず、外側フォームの _FORM_H_SPACING より詰めた値にして「内側の対」
        # であることを視覚的に示す）。
        hlayout.setSpacing(6)

        point: list[float] = list(getattr(obj, spec.key))
        spin_x = QDoubleSpinBox()
        spin_y = QDoubleSpinBox()
        for spin in (spin_x, spin_y):
            _pin_control_height(spin)
            install_wheel_guard(spin)
            spin.setKeyboardTracking(False)
            spin.setDecimals(_NUMBER_DECIMALS)
            spin.setMinimum(-_SPIN_RANGE)
            spin.setMaximum(_SPIN_RANGE)
            # line/arrow の始点/終点行はスピン2個を横に並べるため、自然な
            # sizeHint（107px前後/個）のままだとフォーム全体の最小幅が
            # `_PANEL_FIXED_WIDTH` を超える。フォームを包む QScrollArea が
            # `setWidgetResizable(True)` で「ビューポートより小さければ
            # minimumSizeHint まで広げる」ため、この最小幅がパネル幅を
            # 超えると横スクロールバー無しでは右側が見切れる。ここで最小幅の
            # 床を下げておくことで、フォームの最小幅が常にパネル幅に収まり、
            # QFormLayout の AllNonFixedFieldsGrow が従来どおりスピンを圧縮して
            # フィットさせる（点行が圧縮されてでも全部見える、という既存の
            # トレードオフを維持）。80px は小数第1位までの数値
            # （例 "-1234.5"）が入る実用上の下限。
            spin.setMinimumWidth(80)
        spin_x.blockSignals(True)
        spin_x.setValue(float(point[0]))
        spin_x.blockSignals(False)
        spin_y.blockSignals(True)
        spin_y.setValue(float(point[1]))
        spin_y.blockSignals(False)

        # 行高統一後は外側ラベル（ScrubLabel）と同じ AlignVCenter を明示することで、
        # 内側の "x"/"y" ラベルもスピンと中心が一致する（QSpinBox.setPrefix 化は
        # スピンの sizeHint を +11px/個 押し上げて幅制約を圧迫するので採らない）。
        label_x = QLabel("x")
        label_x.setAlignment(Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter)
        label_y = QLabel("y")
        label_y.setAlignment(Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter)

        hlayout.addWidget(label_x)
        hlayout.addWidget(spin_x)
        hlayout.addWidget(label_y)
        hlayout.addWidget(spin_y)

        def on_changed(
            _value: float = 0.0,
            obj: BaseObject = obj,
            spec: PropSpec = spec,
            spin_x: QDoubleSpinBox = spin_x,
            spin_y: QDoubleSpinBox = spin_y,
        ) -> None:
            if self._rebuilding:
                return
            old_point = list(getattr(obj, spec.key))
            new_point = [spin_x.value(), spin_y.value()]
            if new_point == old_point:
                return
            cmd = SetGeometryCommand(
                self.scene.document,
                obj,
                {spec.key: new_point},
                {spec.key: old_point},
                mergeable=True,
            )
            self._push(cmd)

        spin_x.valueChanged.connect(on_changed)
        spin_y.valueChanged.connect(on_changed)

        def update_widget(
            obj: BaseObject = obj,
            spec: PropSpec = spec,
            spin_x: QDoubleSpinBox = spin_x,
            spin_y: QDoubleSpinBox = spin_y,
        ) -> None:
            if not shiboken6.isValid(spin_x) or not shiboken6.isValid(spin_y):
                return
            point = list(getattr(obj, spec.key))
            spin_x.blockSignals(True)
            try:
                spin_x.setValue(float(point[0]))
            finally:
                spin_x.blockSignals(False)
            spin_y.blockSignals(True)
            try:
                spin_y.setValue(float(point[1]))
            finally:
                spin_y.blockSignals(False)

        self._updaters.append(update_widget)
        return container

    def _make_color_widget(self, obj: BaseObject, spec: PropSpec) -> ColorSwatchButton:
        """color / color_opt 共通の色ボタンを作る（要望8/11/12: `ColorSwatchButton`
        に統一。パレット8色＋黒白＋（nullable なら）null_label＋「色を選択…」を
        持つメニューは `ColorSwatchButton` 自身が構築する）。
        """
        button = ColorSwatchButton(nullable=(spec.kind == "color_opt"), null_label=spec.null_label)
        self._track_color_swatch(button)
        button.set_value(getattr(obj, spec.key))

        def on_chosen(new_value: str | None, obj: BaseObject = obj, spec: PropSpec = spec) -> None:
            if self._rebuilding:
                return
            old_value = getattr(obj, spec.key)
            if _color_eq(new_value, old_value):
                return
            # `color_chosen` はメニュー項目クリック/ダイアログOKごとに1回だけ
            # 発火する離散コミット（B/I/U・フォントと同じ理由で
            # `mergeable=False`。レビュー3巡目 finding #9）: 既定のまま
            # （mergeable=True）だと「赤→青」の2回の選択が1エントリへ潰れ、
            # Ctrl+Z が赤を飛ばして初期色へ戻ってしまう。
            self._commit_scalar(
                obj, spec.key, new_value, old_value, mergeable=False, label=f"{spec.label}を変更"
            )

        button.color_chosen.connect(on_chosen)

        def update_widget(
            button: ColorSwatchButton = button, obj: BaseObject = obj, spec: PropSpec = spec
        ) -> None:
            if not shiboken6.isValid(button):
                return
            button.set_value(getattr(obj, spec.key))

        self._updaters.append(update_widget)
        return button

    def _make_enum_widget(self, obj: BaseObject, spec: PropSpec) -> QComboBox:
        combo = QComboBox()
        _pin_control_height(combo)
        install_wheel_guard(combo)
        combo.addItems(list(spec.options))
        combo.blockSignals(True)
        idx = combo.findText(str(getattr(obj, spec.key)))
        combo.setCurrentIndex(idx if idx >= 0 else 0)
        combo.blockSignals(False)

        def on_changed(
            _index: int, obj: BaseObject = obj, spec: PropSpec = spec, combo: QComboBox = combo
        ) -> None:
            if self._rebuilding:
                return
            old_value = getattr(obj, spec.key)
            new_value = combo.currentText()
            if new_value == old_value:
                return
            # enum コンボの選択は1回きりの離散コミット（色/B・I・U/フォントと
            # 同じ理由で mergeable=False。レビュー3巡目 finding #9）。
            self._push(
                SetPropertyCommand(
                    self.scene.document, obj, spec.key, new_value, old_value, mergeable=False
                )
            )

        combo.currentIndexChanged.connect(on_changed)

        def update_widget(
            combo: QComboBox = combo, obj: BaseObject = obj, spec: PropSpec = spec
        ) -> None:
            if not shiboken6.isValid(combo):
                return
            combo.blockSignals(True)
            try:
                idx = combo.findText(str(getattr(obj, spec.key)))
                combo.setCurrentIndex(idx if idx >= 0 else 0)
            finally:
                combo.blockSignals(False)

        self._updaters.append(update_widget)
        return combo

    def _make_bool_widget(self, obj: BaseObject, spec: PropSpec) -> QCheckBox:
        """トグル指定の無い bool（locked/visible/closed 等）は従来どおり QCheckBox。"""
        checkbox = QCheckBox()
        _pin_control_height(checkbox)
        checkbox.blockSignals(True)
        checkbox.setChecked(bool(getattr(obj, spec.key)))
        checkbox.blockSignals(False)

        def on_toggled(checked: bool, obj: BaseObject = obj, spec: PropSpec = spec) -> None:
            if self._rebuilding:
                return
            old_value = getattr(obj, spec.key)
            if checked == old_value:
                return
            # チェックボックスのクリックも1回きりの離散コミット（レビュー3巡目
            # finding #9）。既定のまま（mergeable=True）だと2回クリックが
            # old=False/new=False の no-op 1エントリへ潰れ、Ctrl+Z が見た目
            # 何もしなくなる。
            self._push(
                SetPropertyCommand(
                    self.scene.document, obj, spec.key, checked, old_value, mergeable=False
                )
            )

        checkbox.toggled.connect(on_toggled)

        def update_widget(
            checkbox: QCheckBox = checkbox, obj: BaseObject = obj, spec: PropSpec = spec
        ) -> None:
            if not shiboken6.isValid(checkbox):
                return
            checkbox.blockSignals(True)
            try:
                checkbox.setChecked(bool(getattr(obj, spec.key)))
            finally:
                checkbox.blockSignals(False)

        self._updaters.append(update_widget)
        return checkbox

    def _make_toggle_row_widget(
        self, obj: BaseObject, specs: list[PropSpec]
    ) -> tuple[QWidget, dict[str, QToolButton]]:
        """`widget=="toggle"` の連続 spec を1行にまとめる（要望14、B/I/U）。

        `control_h` 高さのコンテナに `control_h`×`control_h` の checkable
        `QToolButton` を並べる。アイコンは `icons.icon_checkable`（オフ=灰色/
        オン=アクセント色。qtawesome に off 版グリフが無いため色のみで表現）。
        """
        container = QWidget()
        _pin_control_height(container)
        hlayout = QHBoxLayout(container)
        hlayout.setContentsMargins(0, 0, 0, 0)
        hlayout.setSpacing(4)

        per_key: dict[str, QToolButton] = {}
        control_h = current_theme().control_h
        for spec in specs:
            button = QToolButton()
            button.setCheckable(True)
            button.setFixedSize(control_h, control_h)
            button.setIconSize(QSize(_TOGGLE_ICON_SIZE, _TOGGLE_ICON_SIZE))
            button.setIcon(icons.icon_checkable(spec.icon or ""))
            button.setToolTip(spec.label)
            button.blockSignals(True)
            button.setChecked(bool(getattr(obj, spec.key)))
            button.blockSignals(False)

            def on_toggled(checked: bool, obj: BaseObject = obj, spec: PropSpec = spec) -> None:
                if self._rebuilding:
                    return
                old_value = getattr(obj, spec.key)
                if checked == old_value:
                    return
                # 離散的な1回きりのクリック。連続クリックが1エントリへ潰れると
                # クリック1回=undo1回の要件が壊れる（レビュー2巡目 finding #1）。
                self._commit_scalar(obj, spec.key, checked, old_value, mergeable=False)

            button.toggled.connect(on_toggled)
            hlayout.addWidget(button)
            per_key[spec.key] = button

        hlayout.addStretch(1)

        def update_widget(
            per_key: dict[str, QToolButton] = per_key,
            obj: BaseObject = obj,
            specs: list[PropSpec] = specs,
        ) -> None:
            for spec in specs:
                button = per_key[spec.key]
                if not shiboken6.isValid(button):
                    continue
                button.blockSignals(True)
                try:
                    button.setChecked(bool(getattr(obj, spec.key)))
                finally:
                    button.blockSignals(False)

        self._updaters.append(update_widget)
        return container, per_key

    def _make_font_family_widget(self, obj: BaseObject, spec: PropSpec) -> FontFamilyCombo:
        """`widget=="font_family"` の行（要望1）。インストール済みフォントの
        ドロップダウン。値は `activated` 経由でのみコミットする。
        """
        combo = FontFamilyCombo()
        _pin_control_height(combo)
        combo.set_family(getattr(obj, spec.key))

        def on_chosen(value: str, obj: BaseObject = obj, spec: PropSpec = spec) -> None:
            if self._rebuilding:
                return
            old_value = getattr(obj, spec.key)
            if value == old_value:
                return
            # `activated` は選択1回につき1回だけ発火する離散コミット
            # （レビュー2巡目 finding #1）。矢印キーでの連続選択もそれぞれが
            # コミットなので、それぞれ1エントリになる（意図どおり）。
            self._commit_scalar(obj, spec.key, value, old_value, mergeable=False)

        combo.family_chosen.connect(on_chosen)

        def update_widget(
            combo: FontFamilyCombo = combo, obj: BaseObject = obj, spec: PropSpec = spec
        ) -> None:
            if not shiboken6.isValid(combo):
                return
            combo.set_family(getattr(obj, spec.key))

        self._updaters.append(update_widget)
        return combo

    def _make_text_widget(self, obj: BaseObject, spec: PropSpec) -> QLineEdit:
        line_edit = QLineEdit()
        line_edit.blockSignals(True)
        line_edit.setText(str(getattr(obj, spec.key)))
        line_edit.blockSignals(False)

        def on_finished(
            obj: BaseObject = obj, spec: PropSpec = spec, le: QLineEdit = line_edit
        ) -> None:
            if self._rebuilding:
                return
            old_value = getattr(obj, spec.key)
            new_value = le.text()
            if new_value == old_value:
                return
            # _commit_scalar 経由にすることで math/text の box 追従が付く。
            # `editingFinished` は確定1回につき1回だけ発火する離散コミット
            # （text/latex/name いずれも該当。レビュー2巡目 finding #1）ので
            # 連続する別々の編集を1エントリへ潰さない。ラベルは spec.key で
            # 決める（obj.type ではない）——name は全型共通のキーなので、
            # 「math オブジェクトの名前欄」を obj.type だけで判定すると
            # 「数式を変更」という誤ったラベルになる（latex キーの math だけ
            # 「数式を変更」、それ以外は spec.label から「名前を変更」等）。
            label = "数式を変更" if spec.key == "latex" else f"{spec.label}を変更"
            self._commit_scalar(obj, spec.key, new_value, old_value, mergeable=False, label=label)

        line_edit.editingFinished.connect(on_finished)

        def update_widget(
            le: QLineEdit = line_edit, obj: BaseObject = obj, spec: PropSpec = spec
        ) -> None:
            if not shiboken6.isValid(le):
                return
            le.blockSignals(True)
            try:
                le.setText(str(getattr(obj, spec.key)))
            finally:
                le.blockSignals(False)

        self._updaters.append(update_widget)
        return line_edit

    # ------------------------------------------------------------------
    # multi モード: 複数選択の共通プロパティ・混在表示
    # ------------------------------------------------------------------

    def _whole_group_selection(self, objs: list[BaseObject]) -> int | None:
        """`objs` が「ちょうど1つのグループの、ロックされていない全メンバー」
        だけなら、そのグループ id を返す（要望10の同じ穴、項目8）。

        判定はモデルだけで行う（F 担当のグループ「入る」状態には依存しない）。
        """
        if not objs:
            return None
        group_ids = {o.group_id for o in objs}
        if len(group_ids) != 1:
            return None
        group_id = next(iter(group_ids))
        if group_id is None:
            return None
        # 「選択され得るメンバー」は not locked だけでなく visible も要る（review
        # finding #7）: Qt は非表示アイテムを選択できないため、非表示メンバーを持つ
        # グループは以前の「ロックのみ」判定では全体選択に到達できず、この関数が
        # 常に None を返し続けて絶対座標の複数選択フォームへ崩壊していた。
        members = self.scene.document.selectable_group_members(group_id)
        if not members:
            return None
        return group_id if {o.id for o in objs} == {m.id for m in members} else None

    def _multi_common_specs(self, objs: list[BaseObject]) -> list[PropSpec]:
        """選択オブジェクト全型に共通する (key, kind[, options]) を求める。

        表示順は先頭オブジェクトの型の spec 順（PROPERTIES[type] の交差）。
        point は対象外。`requires` 付き spec も対象外にする（mask_src 等
        インスタンス依存の可否を型交差だけでは判定できないため）。

        `widget=="font_family"` の text kind は例外的に許可する（要望1: 複数
        選択でもフォント欄を出す。混在は `FontFamilyCombo.set_family(None)`）。

        color/color_opt は互換扱いにする。rect（fill/stroke が color_opt）と
        line（stroke が color のまま）を同時選択すると、単純な
        `other.kind != spec.kind` 判定では「線色」行そのものが消えてしまう
        （color_opt vs color の不一致として弾かれるため。今できていることの
        回帰になる）。実効 kind は「全員 color_opt のときだけ color_opt、
        1つでも color が混ざれば color」に寄せる（狭い方＝null を許容しない方へ
        寄せることで、実際に None を送れない対象を含む行にメニューを出さない）。
        """
        if not objs:
            return []
        types = [o.type for o in objs]
        base_specs = PROPERTIES.get(types[0], [])
        other_spec_maps = [{s.key: s for s in PROPERTIES.get(t, [])} for t in types[1:]]
        result: list[PropSpec] = []
        for spec in base_specs:
            is_font_family = spec.kind == "text" and spec.widget == "font_family"
            if (spec.kind not in _MULTI_KINDS and not is_font_family) or spec.requires is not None:
                continue
            ok = True
            kinds = {spec.kind}
            for spec_map in other_spec_maps:
                other = spec_map.get(spec.key)
                if other is None or other.requires is not None:
                    ok = False
                    break
                if other.kind == spec.kind and other.widget == spec.widget:
                    if other.options != spec.options:
                        ok = False
                        break
                    continue
                if {spec.kind, other.kind} <= _COLOR_KINDS:
                    kinds.add(other.kind)
                    continue
                ok = False
                break
            if not ok:
                continue
            if kinds <= _COLOR_KINDS and len(kinds) > 1:
                spec = replace(spec, kind="color")
            result.append(spec)
        return result

    def _build_multi_form(self, objs: list[BaseObject], group_id: int | None = None) -> None:
        """`group_id` は `_apply_selection_state` が計算済みの
        `_whole_group_selection(objs)` の結果を渡す（省略時はここで計算し直す。
        値は `objs` に対して常に一意に決まるので、その場合も結果は変わらない）。
        `_current_multi_group_id` と実際に構築するフォームの食い違いを構造的に
        防ぐため、呼び出し側で計算した値をそのまま使わせる（findings #1/#10）。
        """
        self._rebuilding = True
        self._updaters = []
        try:
            if group_id is None:
                group_id = self._whole_group_selection(objs)
            if group_id is not None:
                self._add_group_xy_rows(objs, group_id)

            specs = self._multi_common_specs(objs)
            if group_id is not None:
                # 幅/高さ/回転(と生の x/y)の行はグループ全体では出さない（項目8）。
                specs = [s for s in specs if s.key not in _GEOMETRY_KEYS]

            i = 0
            while i < len(specs):
                spec = specs[i]
                if spec.row is not None:
                    row_specs = [spec]
                    j = i + 1
                    while j < len(specs) and specs[j].row == spec.row:
                        row_specs.append(specs[j])
                        j += 1
                    i = j
                    label_widget: QWidget = QLabel(spec.row_label or spec.label)
                    field_widget, per_key = self._make_multi_toggle_row_widget(objs, row_specs)
                    row = self._add_row(spec.group, label_widget, field_widget)
                    for row_spec in row_specs:
                        self._register_key(
                            row_spec.key, row, per_key[row_spec.key], label_widget, row_spec.group
                        )
                    continue
                widget = self._make_multi_widget(objs, spec)
                label_widget = QLabel(spec.label)
                row = self._add_row(spec.group, label_widget, widget)
                self._register_key(spec.key, row, widget, label_widget, spec.group)
                i += 1
        finally:
            self._rebuilding = False

    def _add_group_xy_rows(self, objs: list[BaseObject], group_id: int) -> None:
        """選択がちょうど1つのグループ全体のとき、外接矩形の左上を X/Y として
        出す（項目8）。編集するとメンバー全員を同じ差分だけ平行移動する。

        `objs` は選択（可視メンバーのみ、`_whole_group_selection` が判定済み）
        で、表示する外接矩形（`group_origin`）はこちらを基準にする——非表示の
        メンバーが混ざると、画面に見えているものと違う原点が表示されてしまう。
        一方、実際に平行移動する対象は `group_id` から求めた
        `Document.movable_group_members`（要望10 追加決定 Option A: ロック
        されていなければ非表示でも剛体で動く）を使う——`objs` のままだと
        非表示メンバーだけその場に取り残され、グループが崩れる。

        line/arrow の端点・接着・コネクタの扱いは自作せず、`resolved_bounding_box`
        （接着済み端点をアンカーから解き直す）と `translate_geom`（幾何種別ごとの
        平行移動）という既存の共有ロジックをそのまま使う。

        ただし `resolved_bounding_box` をそのまま原点計算に使うと、グループ外の
        オブジェクトに接着された line/connector を含む選択で収束しない
        （finding #2）: 表示する原点はアンカーから解決した座標（外側の接着先が
        動けば一緒に動く）なのに、実際の移動は `translate_geom` が生の
        p1/p2・source_point/target_point しか動かさない——外側の接着先はその場に
        残るので、原点も動かない。書いた値と表示され続ける値が食い違い、
        再入力するたびにグループがさらにずれていく。
        `binding_slots` で「グループ外へ接着されたスロットを持つメンバー」を
        見分け、そのメンバーからは（`translate_geom` が実際に動かす）自由な
        端点の生座標だけを原点候補に加える。グループ内で完結するメンバー
        （接着なし・グループ内メンバー同士の接着）は従来どおり解決済み矩形を使う
        （そちらは接着先も一緒に動くので収束する）。

        「グループ外へ接着」の判定は一段だけでは不十分だった（レビュー2巡目
        finding #2）: line L1 がメンバー L2 に接着し、L2 がさらにグループ外へ
        接着している場合、L1 自身の接着スロットだけを見ると「メンバーにしか
        接着していない」ため誤って解決済み矩形の枝に入るが、その解決済み座標は
        L2 の外側の端が動かないぶんだけ delta の一部しか反映せず、収束しない
        （再入力のたびにグループがさらにずれる）。`_binding_closure` で接着
        チェーンを辿った到達可能集合がメンバー集合に完全に収まるかどうかで
        判定する（直接の1段接着も到達可能集合に含まれるので、従来からの単純な
        ケースも変わらず検出される）。

        回転した box メンバーの原点は、model の x/y（回転前の矩形）ではなく
        見えている回転後の外接矩形（`boxes.rotated_aabb`）を使う（レビュー2巡目
        finding #4）。単一オブジェクトの X/Y が model x/y をそのまま見せるのとは
        意図的に違う——グループ行が示すのは「グループ全体の外接矩形の左上」
        （契約 D2-8）であり、これはスナップ側（`CanvasScene.snap_rect_for_item`）
        や「入っているグループ」の破線枠（`sceneBoundingRect`）が使っているのと
        同じ意味の矩形。endpoints/connector メンバーは回転を持たないので対象外。

        `move_members`/`member_ids`（実際に平行移動する対象）は**フォーム構築時
        に1回だけ計算してクロージャに閉じ込めない**——`group_origin`/
        `on_changed` の呼び出しのたびに `document.movable_group_members(group_id)`
        を都度引き直す（レビュー3巡目 finding #3）。以前はここで1回だけ計算して
        `on_changed`/`update_widget` が使い回していたため、非表示メンバーの
        ロック状態をレイヤーパネルから切り替えても（選択もグループも変わらない
        ためフォームは再構築されない）古いメンバー集合のまま平行移動してしまい、
        ロック済みメンバーを動かす／解除済みメンバーを置き去りにする、という
        「ドラッグは毎回引き直すのに、この行だけ古い集合を使う」不整合が起きて
        いた。削除で消えたメンバーがこの行だけ残留する副作用も同時に直る。
        `objs`（表示原点の基準）は選択なので、選択が変われば必ずフォームごと
        再構築される——閉じ込めたままで安全。
        """
        from app.graphics.boxes import rotated_aabb
        from app.graphics.routing import resolved_bounding_box
        from app.model.objects import binding_slots

        document = self.scene.document

        def member_box(o: BaseObject) -> tuple[float, float, float, float]:
            box = resolved_bounding_box(document, o)
            if getattr(o, "GEOMETRY", "box") == "box":
                return rotated_aabb(box, float(getattr(o, "rotation", 0.0)))
            return box

        def move_members() -> list[BaseObject]:
            """平行移動する対象（呼び出しのたびに引き直す。理由は docstring 末尾）。

            グループに「入っている」間は、選択中のメンバーだけを動かす——ドラッグ
            （`CanvasScene.rigid_group_targets` は入っているグループを展開しない）と
            同じ意味にそろえる。入った状態で可視メンバーを全部選び直した場合に、
            ドラッグは非表示メンバーを置いていくのにこの行だけ連れて行く、という
            食い違いを防ぐ（レビュー4巡目 minor）。入っているかどうかは選択を変えずに
            Esc で切り替わりうるので、フォーム構築時ではなく毎回判定する。
            """
            entered = getattr(self.scene, "entered_group_id", None)
            if callable(entered) and entered() == group_id:
                return list(objs)
            return document.movable_group_members(group_id)

        def group_origin() -> tuple[float, float]:
            member_ids = {o.id for o in move_members()}
            points: list[tuple[float, float]] = []
            for o in objs:
                slots = binding_slots(o.type)
                has_outside_binding = not _binding_closure(document, o) <= member_ids
                if not has_outside_binding:
                    box = member_box(o)
                    points.append((box[0], box[1]))
                    continue
                for id_key, _anchor_key, point_key in slots:
                    if getattr(o, id_key) is None:
                        px, py = getattr(o, point_key)
                        points.append((float(px), float(py)))
            if not points:
                # 全メンバーが（line/connector で）両端ともグループ外へ接着されて
                # いるなど、動く自由な点が1つも無い縮退ケース。従来どおり解決済み
                # 矩形の最小値にフォールバックする（表示はできるが動かしても
                # 収束はしない——動く点そのものが無いのでこれ以上できることはない）。
                boxes = [member_box(o) for o in objs]
                return (min(b[0] for b in boxes), min(b[1] for b in boxes))
            return (min(p[0] for p in points), min(p[1] for p in points))

        for axis_index, key, label in ((0, "x", "X"), (1, "y", "Y")):
            spin = QDoubleSpinBox()
            _pin_control_height(spin)
            install_wheel_guard(spin)
            spin.setKeyboardTracking(False)
            spin.setDecimals(_NUMBER_DECIMALS)
            spin.setMinimum(-_SPIN_RANGE)
            spin.setMaximum(_SPIN_RANGE)
            spin.blockSignals(True)
            spin.setValue(group_origin()[axis_index])
            spin.blockSignals(False)

            def on_changed(
                value: float, spin: QDoubleSpinBox = spin, axis_index: int = axis_index
            ) -> None:
                if self._rebuilding:
                    return
                old_origin = group_origin()
                delta = value - old_origin[axis_index]
                if abs(delta) < 1e-9:
                    return
                dx = delta if axis_index == 0 else 0.0
                dy = delta if axis_index == 1 else 0.0
                entries: list[tuple[BaseObject, dict[str, Any], dict[str, Any]]] = []
                for o in move_members():
                    old_geom, new_geom = translate_geom(o, dx, dy)
                    entries.append((o, new_geom, old_geom))
                # 単一コマンド + mergeWith にすることでスピンの連打が1エントリへ
                # 統合される（`_push_macro` はマクロ単位で、Qt はマクロ同士を
                # マージしないため毎ティック別エントリになっていた。
                # レビュー2巡目 finding #3）。`_push` を使うことで再入安全性の
                # `_push_depth` ガードは維持する。
                self._push(TranslateGroupCommand(document, entries, "グループを移動"))

            spin.valueChanged.connect(on_changed)

            def update_widget(spin: QDoubleSpinBox = spin, axis_index: int = axis_index) -> None:
                if not shiboken6.isValid(spin):
                    return
                spin.blockSignals(True)
                try:
                    spin.setValue(group_origin()[axis_index])
                finally:
                    spin.blockSignals(False)

            label_widget = QLabel(label)
            row = self._add_row(GROUP_GEOMETRY, label_widget, spin)
            self._register_key(key, row, spin, label_widget, GROUP_GEOMETRY)
            self._updaters.append(update_widget)

    def _make_multi_widget(self, objs: list[BaseObject], spec: PropSpec) -> QWidget:
        if spec.kind == "number":
            return self._make_multi_number_widget(objs, spec)
        if spec.kind == "int":
            return self._make_multi_int_widget(objs, spec)
        if spec.kind in ("color", "color_opt"):
            return self._make_multi_color_widget(objs, spec)
        if spec.kind == "enum":
            return self._make_multi_enum_widget(objs, spec)
        if spec.kind == "bool":
            return self._make_multi_bool_widget(objs, spec)
        if spec.kind == "text" and spec.widget == "font_family":
            return self._make_multi_font_widget(objs, spec)
        raise NotImplementedError(f"multi モード未対応の kind: {spec.kind!r}")

    def _make_multi_number_widget(self, objs: list[BaseObject], spec: PropSpec) -> QDoubleSpinBox:
        spin = _MixedDoubleSpinBox()
        _pin_control_height(spin)
        install_wheel_guard(spin)
        spin.setKeyboardTracking(False)
        spin.setDecimals(spec.decimals if spec.decimals is not None else _NUMBER_DECIMALS)
        spin.setMinimum(spec.minimum if spec.minimum is not None else -_SPIN_RANGE)
        spin.setMaximum(spec.maximum if spec.maximum is not None else _SPIN_RANGE)
        spin.setSingleStep(spec.step if spec.step is not None else 1.0)

        def sync_widget(spin: _MixedDoubleSpinBox = spin) -> None:
            if not shiboken6.isValid(spin):
                return
            values = {float(getattr(o, spec.key)) for o in objs}
            spin.blockSignals(True)
            try:
                if len(values) == 1:
                    spin.mixed = False
                    spin.setValue(next(iter(values)))
                else:
                    spin.mixed = True
                    spin.lineEdit().setText(_MIXED_TEXT)
            finally:
                spin.blockSignals(False)

        sync_widget()

        def on_changed(value: float, spin: QDoubleSpinBox = spin) -> None:
            if self._rebuilding:
                return
            # 1確定 = 1エントリ（レビュー3巡目 finding #8）。以前は
            # `_push_macro`（beginMacro/endMacro）を毎ティック使っていたが、
            # `QUndoStack` はマクロ同士を絶対にマージしないため、矢印の連打が
            # そのまま連打回数ぶんの undo エントリになっていた（単一選択の
            # `SetPropertyWithFollowCommand`/`TranslateGroupCommand` と挙動が
            # 食い違う）。`SetMultiPropertyCommand` に一本化し、メンバー集合が
            # 同じ連続ティックを `mergeWith` で1エントリへ統合する。
            # メンバー集合を安定させるため、値が既に一致している対象も
            # （`continue` で飛ばさず）new==old のエントリとして含める——
            # 混在選択から始まった最初のティックでメンバー集合が変わると、
            # 以後のティックとマージできなくなる。
            entries: list[tuple[BaseObject, dict[str, Any], dict[str, Any]]] = []
            for o in objs:
                new_values: dict[str, Any] = {spec.key: value}
                if spec.key not in _GEOMETRY_KEYS:
                    follow = box_follow_geometry(o, {spec.key: value})
                    if follow:
                        new_values.update(follow)
                old_values = {k: getattr(o, k) for k in new_values}
                entries.append((o, new_values, old_values))
            if all(new == old for _, new, old in entries):
                # 誰にとっても正味の変化が無い（例: 混在選択に対してたまたま
                # 既存値と同じ値を打ち込んだ）ときは、マージ相手の無い空エントリを
                # 新規に積まない。
                return
            merge_key = "geom" if spec.key in _GEOMETRY_KEYS else spec.key
            self._push(
                SetMultiPropertyCommand(
                    self.scene.document, merge_key, entries, f"{spec.label}を変更"
                )
            )

        spin.valueChanged.connect(on_changed)
        self._updaters.append(sync_widget)
        return spin

    def _make_multi_int_widget(self, objs: list[BaseObject], spec: PropSpec) -> QSpinBox:
        spin = _MixedSpinBox()
        _pin_control_height(spin)
        install_wheel_guard(spin)
        spin.setKeyboardTracking(False)
        spin.setMinimum(int(spec.minimum) if spec.minimum is not None else int(-_SPIN_RANGE))
        spin.setMaximum(int(spec.maximum) if spec.maximum is not None else int(_SPIN_RANGE))
        spin.setSingleStep(int(spec.step) if spec.step is not None else 1)

        def sync_widget(spin: _MixedSpinBox = spin) -> None:
            if not shiboken6.isValid(spin):
                return
            values = {int(getattr(o, spec.key)) for o in objs}
            spin.blockSignals(True)
            try:
                if len(values) == 1:
                    spin.mixed = False
                    spin.setValue(next(iter(values)))
                else:
                    spin.mixed = True
                    spin.lineEdit().setText(_MIXED_TEXT)
            finally:
                spin.blockSignals(False)

        sync_widget()

        def on_changed(value: int, spin: QSpinBox = spin) -> None:
            if self._rebuilding:
                return
            # `_make_multi_number_widget` と同じ理由・同じ規則
            # （レビュー3巡目 finding #8。`kind=="int"` を使う PropSpec は現状
            # 無いため実質未到達だが、number 側と対称に保つ）。
            entries: list[tuple[BaseObject, dict[str, Any], dict[str, Any]]] = []
            for o in objs:
                new_values: dict[str, Any] = {spec.key: value}
                old_values = {k: getattr(o, k) for k in new_values}
                entries.append((o, new_values, old_values))
            if all(new == old for _, new, old in entries):
                return
            merge_key = "geom" if spec.key in _GEOMETRY_KEYS else spec.key
            self._push(
                SetMultiPropertyCommand(
                    self.scene.document, merge_key, entries, f"{spec.label}を変更"
                )
            )

        spin.valueChanged.connect(on_changed)
        self._updaters.append(sync_widget)
        return spin

    def _make_multi_color_widget(self, objs: list[BaseObject], spec: PropSpec) -> ColorSwatchButton:
        """color / color_opt 共通の複数選択用色ボタン（単一選択と同じ理由で統一）。

        `effective kind`（`_multi_common_specs` が既に決めている）が "color_opt"
        のとき（＝選択全員が color_opt）だけ null 選択肢を出す。rect+line の
        ような混在は effective kind が "color" に寄せられているため、ここでは
        null 選択肢を出さない（多数派が None を許容しない対象を含むので、
        一括で None を送る操作を UI 上提示しない）。
        """
        nullable = spec.kind == "color_opt"
        button = ColorSwatchButton(nullable=nullable, null_label=spec.null_label)
        self._track_color_swatch(button)

        def sync_widget(button: ColorSwatchButton = button) -> None:
            if not shiboken6.isValid(button):
                return
            values = {getattr(o, spec.key) for o in objs}
            normalized = {(v.upper() if v else None) for v in values}
            if len(normalized) == 1:
                button.set_mixed(False)
                button.set_value(next(iter(values)))
            else:
                button.set_mixed(True)

        sync_widget()

        def on_chosen(new_value: str | None, button: ColorSwatchButton = button) -> None:
            cmds: list[Any] = []
            for o in objs:
                old_value = getattr(o, spec.key)
                if _color_eq(new_value, old_value):
                    continue
                cmds.append(
                    SetPropertyCommand(self.scene.document, o, spec.key, new_value, old_value)
                )
            self._push_macro(f"{spec.label}を変更", cmds)

        button.color_chosen.connect(on_chosen)
        self._updaters.append(sync_widget)
        return button

    def _make_multi_enum_widget(self, objs: list[BaseObject], spec: PropSpec) -> QComboBox:
        combo = QComboBox()
        _pin_control_height(combo)
        install_wheel_guard(combo)
        combo.addItems(list(spec.options))

        def sync_widget(combo: QComboBox = combo) -> None:
            if not shiboken6.isValid(combo):
                return
            values = {getattr(o, spec.key) for o in objs}
            combo.blockSignals(True)
            try:
                if len(values) == 1:
                    idx = combo.findText(str(next(iter(values))))
                    combo.setCurrentIndex(idx if idx >= 0 else -1)
                else:
                    combo.setCurrentIndex(-1)
            finally:
                combo.blockSignals(False)

        sync_widget()

        def on_changed(index: int, combo: QComboBox = combo) -> None:
            if self._rebuilding or index < 0:
                return
            new_value = combo.currentText()
            cmds: list[Any] = []
            for o in objs:
                old_value = getattr(o, spec.key)
                if old_value == new_value:
                    continue
                cmds.append(
                    SetPropertyCommand(self.scene.document, o, spec.key, new_value, old_value)
                )
            self._push_macro(f"{spec.label}を変更", cmds)

        combo.currentIndexChanged.connect(on_changed)
        self._updaters.append(sync_widget)
        return combo

    def _make_multi_bool_widget(self, objs: list[BaseObject], spec: PropSpec) -> QCheckBox:
        checkbox = QCheckBox()
        _pin_control_height(checkbox)

        def sync_widget(checkbox: QCheckBox = checkbox) -> None:
            if not shiboken6.isValid(checkbox):
                return
            values = {bool(getattr(o, spec.key)) for o in objs}
            checkbox.blockSignals(True)
            try:
                if len(values) == 1:
                    checkbox.setTristate(False)
                    checkbox.setChecked(next(iter(values)))
                else:
                    checkbox.setTristate(True)
                    checkbox.setCheckState(Qt.CheckState.PartiallyChecked)
            finally:
                checkbox.blockSignals(False)

        sync_widget()

        def on_state_changed(_state: int, checkbox: QCheckBox = checkbox) -> None:
            if self._rebuilding:
                return
            if checkbox.checkState() == Qt.CheckState.PartiallyChecked:
                return  # プログラム的な混在表示セット（本来 blockSignals 済みだが念のため）。
            # ユーザー操作後は2状態に戻す。
            checkbox.setTristate(False)
            new_value = checkbox.isChecked()
            cmds: list[Any] = []
            for o in objs:
                old_value = bool(getattr(o, spec.key))
                if old_value == new_value:
                    continue
                cmds.append(
                    SetPropertyCommand(self.scene.document, o, spec.key, new_value, old_value)
                )
            self._push_macro(f"{spec.label}を変更", cmds)

        checkbox.stateChanged.connect(on_state_changed)
        self._updaters.append(sync_widget)
        return checkbox

    def _make_multi_toggle_row_widget(
        self, objs: list[BaseObject], specs: list[PropSpec]
    ) -> tuple[QWidget, dict[str, QToolButton]]:
        """複数選択版の B/I/U 行（要望14）。

        Word 方式: 混在なら非押下＋tooltip に「（混在）」を添え、クリックで
        全選択オブジェクトを True にする（`toggled` がそのまま `checked=True` を
        運ぶため、通常のトグルと同じ経路で実現できる）。混在でなければ通常の
        トグル（クリックで全員を反転）と同じ挙動になる。
        """
        container = QWidget()
        _pin_control_height(container)
        hlayout = QHBoxLayout(container)
        hlayout.setContentsMargins(0, 0, 0, 0)
        hlayout.setSpacing(4)

        per_key: dict[str, QToolButton] = {}
        control_h = current_theme().control_h
        for spec in specs:
            button = QToolButton()
            button.setCheckable(True)
            button.setFixedSize(control_h, control_h)
            button.setIconSize(QSize(_TOGGLE_ICON_SIZE, _TOGGLE_ICON_SIZE))
            button.setIcon(icons.icon_checkable(spec.icon or ""))

            def on_toggled(checked: bool, spec: PropSpec = spec) -> None:
                if self._rebuilding:
                    return
                cmds: list[Any] = []
                for o in objs:
                    old_value = bool(getattr(o, spec.key))
                    if old_value == checked:
                        continue
                    cmds.append(
                        SetPropertyCommand(self.scene.document, o, spec.key, checked, old_value)
                    )
                    follow = self._box_follow_command(o, spec.key, checked)
                    if follow is not None:
                        cmds.append(follow)
                self._push_macro(f"{spec.label}を変更", cmds)

            button.toggled.connect(on_toggled)
            hlayout.addWidget(button)
            per_key[spec.key] = button

        hlayout.addStretch(1)

        def sync_widget(per_key: dict[str, QToolButton] = per_key) -> None:
            for spec in specs:
                button = per_key[spec.key]
                if not shiboken6.isValid(button):
                    continue
                values = {bool(getattr(o, spec.key)) for o in objs}
                button.blockSignals(True)
                try:
                    if len(values) == 1:
                        button.setChecked(next(iter(values)))
                        button.setToolTip(spec.label)
                    else:
                        button.setChecked(False)
                        button.setToolTip(f"{spec.label}（混在）")
                finally:
                    button.blockSignals(False)

        sync_widget()
        self._updaters.append(sync_widget)
        return container, per_key

    def _make_multi_font_widget(self, objs: list[BaseObject], spec: PropSpec) -> FontFamilyCombo:
        combo = FontFamilyCombo()
        _pin_control_height(combo)

        def sync_widget(combo: FontFamilyCombo = combo) -> None:
            if not shiboken6.isValid(combo):
                return
            values = {getattr(o, spec.key) for o in objs}
            if len(values) == 1:
                combo.set_family(next(iter(values)))
            else:
                combo.set_family(None)

        sync_widget()

        def on_chosen(value: str, combo: FontFamilyCombo = combo) -> None:
            cmds: list[Any] = []
            for o in objs:
                old_value = getattr(o, spec.key)
                if old_value == value:
                    continue
                cmds.append(SetPropertyCommand(self.scene.document, o, spec.key, value, old_value))
                follow = self._box_follow_command(o, spec.key, value)
                if follow is not None:
                    cmds.append(follow)
            self._push_macro(f"{spec.label}を変更", cmds)

        combo.family_chosen.connect(on_chosen)
        self._updaters.append(sync_widget)
        return combo

    # ------------------------------------------------------------------
    # artboard モード: アートボード設定フォーム
    # ------------------------------------------------------------------

    def _build_artboard_form(self) -> None:
        self._rebuilding = True
        self._updaters = []
        try:
            preset_combo = QComboBox()
            _pin_control_height(preset_combo)
            install_wheel_guard(preset_combo)
            for label, *_rest in ARTBOARD_PRESETS:
                preset_combo.addItem(label)
            preset_combo.addItem(ARTBOARD_CUSTOM_LABEL)
            # 最長のプリセット名（例 "A4 (210×297mm)"）がそのまま
            # minimumSizeHint になり、ラベル分と合わせてフォームの最小幅が
            # `_PANEL_FIXED_WIDTH` を超えてしまう（line/arrow の point 行と
            # 同じ構造の問題）。フォームを包む QScrollArea が
            # `setWidgetResizable(True)` でこの最小幅まで広げるため、床を
            # 下げてパネル幅に収める（閉じた状態では省略記号で切れるが、
            # 開けば全文が見える）。
            preset_combo.setMinimumWidth(150)

            width_mm_spin = QDoubleSpinBox()
            _pin_control_height(width_mm_spin)
            install_wheel_guard(width_mm_spin)
            width_mm_spin.setKeyboardTracking(False)
            width_mm_spin.setRange(1.0, 2000.0)
            width_mm_spin.setDecimals(1)

            dpi_spin = QSpinBox()
            _pin_control_height(dpi_spin)
            install_wheel_guard(dpi_spin)
            dpi_spin.setKeyboardTracking(False)
            dpi_spin.setRange(1, 2400)

            width_px_spin = QSpinBox()
            _pin_control_height(width_px_spin)
            install_wheel_guard(width_px_spin)
            width_px_spin.setKeyboardTracking(False)
            width_px_spin.setRange(1, 20000)

            height_px_spin = QSpinBox()
            _pin_control_height(height_px_spin)
            install_wheel_guard(height_px_spin)
            height_px_spin.setKeyboardTracking(False)
            height_px_spin.setRange(1, 20000)

            bg_button = ColorSwatchButton(nullable=False, null_label=_ARTBOARD_BG_NULL_LABEL)
            self._track_color_swatch(bg_button)

            def sync_widget(
                preset_combo: QComboBox = preset_combo,
                width_mm_spin: QDoubleSpinBox = width_mm_spin,
                dpi_spin: QSpinBox = dpi_spin,
                width_px_spin: QSpinBox = width_px_spin,
                height_px_spin: QSpinBox = height_px_spin,
                bg_button: ColorSwatchButton = bg_button,
            ) -> None:
                if not shiboken6.isValid(preset_combo):
                    return
                artboard = self.scene.document.artboard
                for w in (preset_combo, width_mm_spin, dpi_spin, width_px_spin, height_px_spin):
                    w.blockSignals(True)
                try:
                    preset_combo.setCurrentIndex(self._matching_preset_index(artboard))
                    width_mm_spin.setValue(artboard.physical.width_mm)
                    dpi_spin.setValue(artboard.physical.target_dpi)
                    width_px_spin.setValue(artboard.width_px)
                    height_px_spin.setValue(artboard.height_px)
                finally:
                    for w in (
                        preset_combo,
                        width_mm_spin,
                        dpi_spin,
                        width_px_spin,
                        height_px_spin,
                    ):
                        w.blockSignals(False)
                bg_button.set_value(artboard.background)

            sync_widget()

            def push_artboard(new_artboard: Artboard, text: str = "アートボード") -> None:
                old_artboard = self.scene.document.artboard
                if new_artboard == old_artboard:
                    return
                self._push(
                    SetArtboardCommand(self.scene.document, new_artboard, old_artboard, text=text)
                )

            def on_preset_changed(
                index: int,
                width_mm_spin: QDoubleSpinBox = width_mm_spin,
                dpi_spin: QSpinBox = dpi_spin,
                width_px_spin: QSpinBox = width_px_spin,
                height_px_spin: QSpinBox = height_px_spin,
            ) -> None:
                if self._rebuilding or index < 0 or index >= len(ARTBOARD_PRESETS):
                    return  # カスタム選択時は何もしない（現在値を維持）。
                _label, width_mm, dpi, aspect_w, aspect_h = ARTBOARD_PRESETS[index]
                width_px, height_px = preset_px_size(width_mm, dpi, aspect_w, aspect_h)
                old_artboard = self.scene.document.artboard
                new_artboard = Artboard(
                    width_px=width_px,
                    height_px=height_px,
                    physical=Physical(width_mm=width_mm, target_dpi=dpi),
                    background=old_artboard.background,
                )
                # プリセット選択は4フィールドをまとめて1コマンド。
                for w, v in (
                    (width_mm_spin, width_mm),
                    (dpi_spin, dpi),
                    (width_px_spin, width_px),
                    (height_px_spin, height_px),
                ):
                    w.blockSignals(True)
                    w.setValue(v)
                    w.blockSignals(False)
                push_artboard(new_artboard, text="アートボードプリセット")

            def on_field_changed(
                _value: Any = None,
                *,
                source: str = "mm",
                preset_combo: QComboBox = preset_combo,
                width_mm_spin: QDoubleSpinBox = width_mm_spin,
                dpi_spin: QSpinBox = dpi_spin,
                width_px_spin: QSpinBox = width_px_spin,
                height_px_spin: QSpinBox = height_px_spin,
            ) -> None:
                """px↔mm の片方向連動（ユーザー決定）。

                `source="px"`（px スピンの変更）: dpi を維持したまま
                `artboard_with_pixel_size` で width_mm を再計算し、mm 欄へ
                反映する。これをしないと「グリップ/自動フィットで作った
                アートボード px = 書き出し px」の等式が、パネルで px を触った
                瞬間に崩れる（px は変わるのに mm/dpi が古いまま＝書き出し px が
                width_mm/25.4*dpi 経由で別の値になる）。

                `source="mm"`（mm/dpi スピンの変更）: px は変えない（現状維持）。
                """
                if self._rebuilding:
                    return
                old_artboard = self.scene.document.artboard
                if source == "px":
                    new_artboard = artboard_with_pixel_size(
                        old_artboard, float(width_px_spin.value()), float(height_px_spin.value())
                    )
                    width_mm_spin.blockSignals(True)
                    width_mm_spin.setValue(new_artboard.physical.width_mm)
                    width_mm_spin.blockSignals(False)
                else:
                    new_artboard = Artboard(
                        width_px=width_px_spin.value(),
                        height_px=height_px_spin.value(),
                        physical=Physical(
                            width_mm=width_mm_spin.value(), target_dpi=dpi_spin.value()
                        ),
                        background=old_artboard.background,
                    )
                if new_artboard == old_artboard:
                    return
                preset_combo.blockSignals(True)
                preset_combo.setCurrentIndex(self._matching_preset_index(new_artboard))
                preset_combo.blockSignals(False)
                push_artboard(new_artboard)

            def on_bg_chosen(
                new_value: str | None, bg_button: ColorSwatchButton = bg_button
            ) -> None:
                old_artboard = self.scene.document.artboard
                if _color_eq(new_value, old_artboard.background):
                    return
                new_artboard = Artboard(
                    width_px=old_artboard.width_px,
                    height_px=old_artboard.height_px,
                    physical=old_artboard.physical,
                    background=new_value,
                )
                push_artboard(new_artboard, text="背景色")

            preset_combo.currentIndexChanged.connect(on_preset_changed)
            width_mm_spin.valueChanged.connect(lambda _v: on_field_changed(source="mm"))
            dpi_spin.valueChanged.connect(lambda _v: on_field_changed(source="mm"))
            width_px_spin.valueChanged.connect(lambda _v: on_field_changed(source="px"))
            height_px_spin.valueChanged.connect(lambda _v: on_field_changed(source="px"))
            bg_button.color_chosen.connect(on_bg_chosen)

            for group, label, widget, key in (
                (_GROUP_ARTBOARD_SIZE, "プリセット", preset_combo, "preset"),
                (_GROUP_ARTBOARD_SIZE, "幅 (mm)", width_mm_spin, "width_mm"),
                (_GROUP_ARTBOARD_SIZE, "解像度 (dpi)", dpi_spin, "dpi"),
                (_GROUP_ARTBOARD_SIZE, "幅 (px)", width_px_spin, "width_px"),
                (_GROUP_ARTBOARD_SIZE, "高さ (px)", height_px_spin, "height_px"),
                (_GROUP_ARTBOARD_BG, "背景色", bg_button, "background"),
            ):
                label_widget = QLabel(label)
                row = self._add_row(group, label_widget, widget)
                self._register_key(key, row, widget, label_widget, group)

            self._updaters.append(sync_widget)
        finally:
            self._rebuilding = False

    @staticmethod
    def _matching_preset_index(artboard: Artboard) -> int:
        """現在の artboard が既知プリセットと一致するならその index、なければカスタム。"""
        phys = artboard.physical
        for i, (_label, width_mm, dpi, aspect_w, aspect_h) in enumerate(ARTBOARD_PRESETS):
            if phys.width_mm != width_mm or phys.target_dpi != dpi:
                continue
            width_px, height_px = preset_px_size(width_mm, dpi, aspect_w, aspect_h)
            if artboard.width_px == width_px and artboard.height_px == height_px:
                return i
        return len(ARTBOARD_PRESETS)

    # ------------------------------------------------------------------
    # コマンド発行ヘルパ
    # ------------------------------------------------------------------

    def _commit_scalar(
        self,
        obj: BaseObject,
        key: str,
        new_value: Any,
        old_value: Any,
        *,
        mergeable: bool = True,
        label: str | None = None,
    ) -> None:
        """`mergeable=False`（レビュー2巡目 finding #1）は、B/I/U トグル・
        フォント選択・LaTeX/テキスト/名前の確定編集のような離散的な1回きりの
        コミット専用。追従し得るキーかどうかに関わらず
        `SetPropertyWithFollowCommand(mergeable=False)` を使う——追従が無い
        キー（underline・name 等）は follow_new/follow_old が空 dict になる
        だけで、redo/undo は `SetPropertyCommand` と同じ結果になる
        （`app/commands/commands.py::SetPropertyWithFollowCommand` docstring 参照）。

        `key` が x/y で、`obj` が「非表示メンバーを含む1つのグループの唯一の
        選択可能メンバー」であるとき（単一選択の X/Y 行はこのケースを扱う唯一の
        場所——`_add_group_xy_rows` は選択がちょうどグループ全体のときだけ
        構築される）は、`obj` 単体ではなく `rigid_group_targets` が返す全員を
        同じ差分だけ平行移動する（レビュー3巡目 finding #4）。以前は
        `SetGeometryCommand(obj)` 単体しか push しなかったため、ドラッグ/コピー/
        複製/削除/グループ化がすべて非表示の兄弟を剛体として連れて行くのに
        パネルの数値入力だけが `obj` を置き去りにしてグループを崩していた
        （Option A の抜け穴）。幅/高さ/回転は非対象（rigid group はサイズ変形
        しない。複数選択フォームが `_GEOMETRY_KEYS` から x/y 以外を group モード
        で出さないのと同じ理由）。
        """
        if key in ("x", "y"):
            targets = self.scene.rigid_group_targets([obj])
            if len(targets) > 1:
                delta = float(new_value) - float(getattr(obj, key))
                if abs(delta) < 1e-9:
                    return
                dx, dy = (delta, 0.0) if key == "x" else (0.0, delta)
                entries: list[tuple[BaseObject, dict[str, Any], dict[str, Any]]] = []
                for o in targets:
                    old_geom, new_geom = translate_geom(o, dx, dy)
                    entries.append((o, new_geom, old_geom))
                self._push(TranslateGroupCommand(self.scene.document, entries, "グループを移動"))
                return
        if key in _GEOMETRY_KEYS:
            cmd = SetGeometryCommand(
                self.scene.document, obj, {key: new_value}, {key: old_value}, mergeable=True
            )
            self._push(cmd)
            return
        if key in followable_keys(obj.type) or not mergeable:
            # math/text の box は自然サイズ×表示倍率（または折返し採寸）の派生値
            # なので、寸法に効くキーの変更には追従を同一エントリで添える。
            # `key` が追従し得るキーである限り、実際にこのティックで追従が
            # 起きるか（follow_new が空か）に関わらず常に
            # `SetPropertyWithFollowCommand` を使う——ティックによって
            # コマンドの型が変わると、隣り合うティック同士でもマージできず、
            # 交互に別エントリへ分かれてしまう（findings #8/#12）。
            follow_new = box_follow_geometry(obj, {key: new_value}) or {}
            follow_old = {k: getattr(obj, k) for k in follow_new}
            if label is None:
                # obj.type が math/text のときだけ専用の文言にする（レビュー3巡目
                # finding #9）。以前は「追従し得るキーかどうか」で math/text 固定
                # 文言に倒していたため、rect の fill 変更が followable_keys の
                # 判定対象外でここへ来ないはずが、将来 followable_keys が広がると
                # 「塗りを変更」のつもりが「テキストを変更」とラベル表示される
                # 危険があった（防波堤として obj.type を明示的に見る）。
                label = (
                    "数式を変更"
                    if obj.type == "math"
                    else "テキストを変更" if obj.type == "text" else f"{key}を変更"
                )
            self._push(
                SetPropertyWithFollowCommand(
                    self.scene.document,
                    obj,
                    key,
                    new_value,
                    old_value,
                    follow_new,
                    follow_old,
                    label,
                    mergeable=mergeable,
                )
            )
            return
        cmd = SetPropertyCommand(
            self.scene.document, obj, key, new_value, old_value, mergeable=mergeable
        )
        self._push(cmd)

    def _box_follow_command(self, obj: BaseObject, key: str, new_value: Any) -> Any | None:
        """math/text の box 追従を `SetGeometryCommand` として返す（複数選択の
        各経路——`_make_multi_number_widget` 等——専用。単一選択の
        `_commit_scalar` はこちらではなく `SetPropertyWithFollowCommand` を
        使う。対象外（追従不要）なら None。`box_follow_geometry`
        （`app/scene/items/box_follow.py`）が dispatch の唯一の真実源
        （エージェント `update_objects`/`apply_style` と共有。2026-09-25
        レビュー2巡目 finding「box-follow logic duplication」対応）。
        """
        follow = box_follow_geometry(obj, {key: new_value})
        if follow is None:
            return None
        old_geom = {k: getattr(obj, k) for k in follow}
        return SetGeometryCommand(self.scene.document, obj, follow, old_geom)

    def _push(self, cmd: Any) -> None:
        if self.scene.undo_stack is None:
            return
        # push_depth の増減で囲むことで、この push() 中に再入した
        # on_selection_changed() が「破壊的パスは危険」と判定できるようにする
        # （上記 on_selection_changed のドキュメント参照）。
        self._push_depth += 1
        try:
            self.scene.undo_stack.push(cmd)
        finally:
            self._push_depth -= 1

    def _push_macro(self, text: str, cmds: list[Any]) -> None:
        """複数コマンドを1つの undo マクロにまとめて push する（multi モード用）。

        `_push()` と同様に `_push_depth` で再入安全性を保つ。`cmds` が空なら
        何もしない（値が全オブジェクトで既に一致している等で変更が無い場合に
        空マクロを作らないため）。1確定 = 1マクロ = undo 1回。
        """
        if self.scene.undo_stack is None or not cmds:
            return
        self._push_depth += 1
        try:
            self.scene.undo_stack.beginMacro(text)
            try:
                for cmd in cmds:
                    self.scene.undo_stack.push(cmd)
            finally:
                # push（の redo）が例外を投げてもマクロを必ず閉じる。閉じ忘れると
                # QUndoStack が「マクロ構築中」のまま固まり、以後の undo/redo が
                # 恒久的に効かなくなる（CLAUDE.md §15 不変条件 2 と同種のハザード）。
                self.scene.undo_stack.endMacro()
        finally:
            self._push_depth -= 1
