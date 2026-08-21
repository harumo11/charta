"""選択中オブジェクトのプロパティを編集するパネル（§9.1、P2契約でモード拡張）。

`scene.selectionChanged` を購読し、選択状態に応じて3つのモードでフォームを
動的生成する（P2契約 §3.2）:

* **object**（単一選択）: `PROPERTIES[obj.type]` から従来どおりのフォームを
  構築する。先頭に整列/分布ボタン行、フォーム内にセクション見出し（「変形」/
  「スタイル」）を持つ。
* **artboard**（未選択）: 「未選択」ラベルは廃止し、代わりにアートボード設定
  フォーム（プリセット/幅mm/dpi/幅px/高さpx/背景色）を表示する。
* **multi**（複数選択）: 「N 個選択中」ラベル + 整列/分布ボタン行 +
  選択オブジェクト全型に共通するプロパティの混在表示フォーム。

値変更はすべて `QUndoCommand`（`SetGeometryCommand`/`SetPropertyCommand`/
`SetArtboardCommand`）経由でモデルに反映する。
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import replace
from typing import TYPE_CHECKING, Any

import shiboken6
from PySide6.QtCore import QSize, Qt, QTimer
from PySide6.QtGui import QColor
from PySide6.QtWidgets import (
    QCheckBox,
    QColorDialog,
    QComboBox,
    QDoubleSpinBox,
    QFormLayout,
    QFrame,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QMenu,
    QPushButton,
    QScrollArea,
    QSpinBox,
    QToolButton,
    QVBoxLayout,
    QWidget,
)

from app.commands.commands import SetArtboardCommand, SetGeometryCommand, SetPropertyCommand
from app.model.document import Artboard, Physical
from app.model.objects import BaseObject
from app.model.properties import PROPERTIES, PropSpec
from app.ui.artboard_presets import ARTBOARD_CUSTOM_LABEL, ARTBOARD_PRESETS, preset_px_size
from app.ui.theme import current_theme, icons
from app.ui.widgets.scrub_label import ScrubLabel

if TYPE_CHECKING:
    from app.scene.canvas_scene import CanvasScene
    from app.ui.controllers.edit_controller import EditController

# x/y/width/height/rotation は SetGeometryCommand、それ以外は SetPropertyCommand（§9.1）。
_GEOMETRY_KEYS = {"x", "y", "width", "height", "rotation"}

_SPIN_RANGE = 1_000_000.0
_DEFAULT_COLOR = "#000000"
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
# 以降（レビュー所見対応、下記）、`panel` 自身の minimumSizeHint はスクロール領域の小さな
# 既定値を返すだけになり、フォームの実際の幅要求を反映しない。テーマ未適用で測ると QSS の
# padding とアプリフォント（Noto Sans 10pt）が乗らず 40px 以上小さい値が出る（conftest.py は
# apply_theme() を呼ばないため、tests/test_panel_layout.py の既存テストで測っている sizeHint
# はテーマ未適用の値であり、この docstring とは別の前提で有効なままである）。
# テーマ適用下の実測（2026-08-21、レビュー所見適用後: line/arrow の point 行スピンに
# `setMinimumWidth(80)`、artboard の preset コンボに `setMinimumWidth(150)` を追加）:
# `_form_widget.sizeHint().width()` は rect 205 / connector 228 / artboard 335 /
# **line・arrow 356**。すなわち line/arrow だけが固定幅 340 を超え、point 行
# （始点/終点）のスピン 1 個あたり自然幅 110px に対し実配置 88〜93px（ウィンドウにより
# 微差）へ圧縮される。この圧縮は `setMinimumWidth(80)` で意図的に許容している
# （自然幅のままだとフォームの `minimumSizeHint` が 340 を超え、フォームを包む
# `QScrollArea` の `setWidgetResizable(True)` がその最小値まで幅を広げてしまい、
# 横スクロールバー無しでは右側が見切れて操作不能になる——レビューで見つかった回帰。
# フロアを下げて再び「圧縮されるが全部見える」旧来のトレードオフに戻した）。
# 注意: artboard の `_form_widget.minimumSizeHint().width()`（`setMinimumWidth(150)`
# 適用後の**最小**幅、sizeHint ではない）は 245——`test_point_row_spins_are_squeezed_
# at_the_current_fixed_width` が line で確認しているのと同種の「最小幅がビューポート
# 幅を超えない」性質を、artboard 自身は sizeHint（335）が固定幅を超える側なので
# 個別テストでは確認していない（実機の 24 ケースのマトリクス確認では OK だった。
# `tests/test_panel_row_metrics.py` の `test_all_field_rows_share_one_height[...-
# artboard]` 系がこれをカバーする）。
# 値そのものの表示は破綻しないため 340 を維持している（広げるとキャンバスが狭くなる。
# ユーザーの要望は「高さが揃っていない」であって幅ではないため、要望されていない見た目の
# 変更を避ける）。この圧縮の事実は tests/test_panel_row_metrics.py::
# test_point_row_spins_are_squeezed_at_the_current_fixed_width が明示的に記録する
# （幅を 380 などに変えたらこのテストが落ちて気づける、という向きの安全網）。
_PANEL_FIXED_WIDTH = 340

# QFormLayout の行間隔（P2契約 担当A）。Fusion 既定と同値を明示することで、
# 将来スタイルが変わってもここが唯一の真実源になる（Fusion 既定に暗黙に委ねない）。
_FORM_V_SPACING = 6
_FORM_H_SPACING = 12

# 整列/分布ボタン行（P2契約 §3.3）: (アイコン名, ツールチップ, align_selected/
# distribute_selected へ渡すモード文字列)。
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

# multi モードで対象にする kind（point/text は対象外、P2契約 §3.6）。
# color_opt は P2 契約（担当C）で追加: rect/ellipse/curve の stroke が
# color_opt になったため、これを対象外にすると「rect を複数選択したときに
# 線色行が出ない」という新たな回帰になる。ただし rect+line のような
# color/color_opt 混在は `_multi_common_specs` が実効 kind を "color" へ
# 寄せるため、この集合には両方を含めておく。
_MULTI_KINDS = frozenset({"number", "int", "color", "color_opt", "enum", "bool"})

#: color と color_opt は「色を選ぶ」という操作としては互換（P2契約: 複数選択で
#: rect+line の「線色」行を消さないための互換扱い）。
_COLOR_KINDS = frozenset({"color", "color_opt"})


def _pin_control_height(widget: QWidget) -> None:
    """スピン/チェックボックスの高さを `control_h` に固定する（レビュー所見）。

    Fusion + QSS の `min-height` は、実測では「その時点でウィジェットに
    割り当てられている幅」によって最終的な計算結果が変わることが確認できた
    （`QDoubleSpinBox`/`QSpinBox`/`QCheckBox` は同じ QSS・同じテーマでも、
    line/arrow の point 行のように幅が圧縮されたり、フォームの縦スクロール
    バーの出現/消失でビューポート幅が数px 増減したりするたびに、外形高さが
    29/32/35/37px のようにばらついた）。QSS の係数（`control_h - N`）を
    チューニングするだけでは「幅が変わるたびに再び崩れる」問題を構造的に
    解決できないため、レイアウト API（`setFixedHeight`）で高さそのものを
    幅に依存しない値へ固定する。ウィジェット単位の `setStyleSheet` ではない
    ため「原則禁止」規約には抵触しない。QSS 側の min-height はこれと矛盾
    しない下限として残してある（フォールバックスタイル適用時の保険）。
    """
    widget.setFixedHeight(current_theme().control_h)


class _MixedDoubleSpinBox(QDoubleSpinBox):
    """混在表示に対応した `QDoubleSpinBox`（multi モード・number 用、P2契約 §3.6）。

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
    """int 版の `_MixedDoubleSpinBox`（同上、P2契約 §3.6）。"""

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
    ) -> None:
        super().__init__(parent)
        self.scene = scene
        # 整列/分布ボタンの実行先。None の場合は整列行を常に非表示にする
        # （既存テストが `PropertyPanel(scene)` で構築するための後方互換）。
        self._edit = edit
        self._rebuilding = False

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

        # 公開ヘルパ（row_for_key/field_widget_for/...）の裏付けとなる行台帳。
        # 見出し行を独立スパン行にした（_HeaderedLabel 廃止）ことで、
        # 「PROPERTIES[type] の並び順 == QFormLayout の行番号」という前提が
        # 崩れるため、key → 実際の行番号を明示的に記録する。
        self._row_keys: dict[str, int] = {}
        # keys_in_form() が返す表示順（requires で隠れた行は含まない）。
        self._ordered_keys: list[str] = []
        # (見出し行の行番号, 見出しテキスト) の列。line/arrow で「スタイル」が
        # p1（始点）行の直前に付くバグ（ユーザー報告）をテストで固定するために使う。
        self._section_rows: list[tuple[int, str]] = []

        # QSS の `#propertyPanel` スコープ選択子（app/ui/theme/qss.py）がこのパネル
        # 自体を識別する鍵（将来パネル全体の見た目を扱う規則のため）。
        self.setObjectName("propertyPanel")

        self._align_row = self._build_align_row()
        self._info_label = QLabel("")
        self._form = QFormLayout()
        self._form_widget = QWidget()
        # 行高・スウォッチ幅の QSS 規則（`#propertyPanelForm ...`）は `self`
        # ではなくこちらをスコープ鍵にする。`_make_color_widget` が開く
        # `QColorDialog` は `self`（PropertyPanel）を親に取るため、`#propertyPanel
        # QPushButton` のような子孫セレクタにすると、ダイアログ内部の OK/Cancel/
        # 「画面上の色を選択」ボタンまで拾って幅108pxへ強制的に狭められてしまう
        # （実測で確認済み。ラベル文字が入り切らずクリップされる）。フォーム内容の
        # コンテナだけを鍵にすることで、その配下にしかいないスウォッチ/入力欄だけに
        # 規則が効き、ダイアログには届かなくなる。
        self._form_widget.setObjectName("propertyPanelForm")
        self._form_widget.setLayout(self._form)
        self._configure_form()

        # フォームをスクロール領域に包む（レビュー所見: ドックがフォームの
        # sizeHint 未満の高さしか与えないとき——1366×768/1200×900 のような
        # 常用ウィンドウで text/rect フォームは容易にこれを超える——QVBoxLayout
        # は `_form_widget` を QSS の min-height 未満まで圧縮してしまい、行高
        # 統一・ラベル/入力欄の垂直中心一致（担当A の目的そのもの）が崩れる。
        # `setWidgetResizable(True)` で「ビューポートより小さければビューポート
        # まで広げ、`_form_widget.minimumSizeHint()` より小さくはしない」という
        # Qt 標準の挙動に任せることで、縦方向は自然な sizeHint を尊重して
        # スクロールに逃がす（どんなドック高でも行は常に control_h を保つ）。
        # 横方向は `_make_point_widget` 側でスピンに `setMinimumWidth` の下限を
        # 設けてあるため、フォーム全体の最小幅がパネル固定幅(340px)を上回らず、
        # 横スクロールバー（AlwaysOff）を出さなくても見切れない
        # （line/arrow の point 行が圧縮されて全部見える、という既存の
        # トレードオフを維持したまま実現している）。
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
        整列行・セクション見出しを足すと自然な `sizeHint` の高さは大きくなる。
        これをそのまま `minimumSizeHint` として右ドックの `QSplitter` に渡すと、
        ウィンドウを縮小できる下限がその分だけ底上げされ、極端に小さい画面
        （オフスクリーンのテスト環境等）で `MainWindow._resize_window_to_fit`
        が想定するチロム（window - viewport）と実際の値がずれてしまう。
        通常サイズのウィンドウでは十分な余裕があるため見た目には影響しない
        （Qt はレイアウトに空きがある限り `sizeHint`＝プリファードサイズで
        描画し、`minimumSizeHint` は本当に空間が足りないときの床にすぎない）。
        """
        hint = super().minimumSizeHint()
        return QSize(hint.width(), 1)

    def sizeHint(self) -> QSize:  # noqa: N802 (Qt override)
        """フォームの自然な幅を反映する（`_form_scroll` の幅上乗せを打ち消す）。

        `QScrollArea.sizeHint()` は縦スクロールバー用の余白を常に見込むため、
        埋め込みウィジェット（`_form_widget`）自身の sizeHint 幅より広い値を
        返す（実測: 内容 316px に対し 329px）。これが `panel.sizeHint()`
        （`panel.adjustSize()` が使う）を押し上げ、line/arrow のような
        幅広フォームで `_PANEL_FIXED_WIDTH` を余分に超えてしまう
        （既存テスト `test_panel_form_does_not_exceed_fixed_width_for_widest_type`
        の回帰、レビュー所見対応）。

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
        """行の整列規約を明示する（Fusion 既定に委ねない、P2契約 担当A）。

        既定では labelAlignment に垂直フラグが無く、行がフィールドより高いとき
        フィールドが上寄せされる（実測: 見出し埋め込み行でラベル文字とスピンの中心が
        13〜21px ずれていた）。AlignVCenter を明示し、行の高さは QSS の min-height
        （Theme.control_h）で一定にすることで、どの kind の行でもラベル文字と入力欄の
        垂直中心が一致する。
        """
        self._form.setLabelAlignment(Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter)
        self._form.setFormAlignment(Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignTop)
        self._form.setFieldGrowthPolicy(QFormLayout.FieldGrowthPolicy.AllNonFixedFieldsGrow)
        self._form.setRowWrapPolicy(QFormLayout.RowWrapPolicy.DontWrapRows)
        self._form.setVerticalSpacing(_FORM_V_SPACING)
        self._form.setHorizontalSpacing(_FORM_H_SPACING)

    # ------------------------------------------------------------------
    # 整列/分布ボタン行（P2契約 §3.3）
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
        """整列=選択2個以上、分布=3個以上で有効化する（P2契約 §3.3）。"""
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
            if self._current_mode == "multi" and ids == self._current_multi_ids:
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
            self._clear_form()
            self._build_multi_form(selected)
            self._info_label.setText(f"{len(selected)} 個選択中")
            self._info_label.show()
            self._form_widget.show()
            self._set_align_row_visible(True)
            self._update_align_enabled(len(selected))
            return

        # 未選択 = artboard モード（P2契約 §3.2: 表示上は「未選択」ラベルを
        # アートボード設定フォームに置き換えて廃止する）。ラベル自体の
        # テキストは互換のため "未選択" のまま保持しつつ非表示にする
        # （tests/test_panel_edit_m8.py が `_info_label.text() == "未選択"`
        # を検証している）。
        self._current_mode = "artboard"
        self._current_obj_id = None
        self._current_type = None
        self._current_requires_state = None
        self._current_multi_ids = None
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
        self._ordered_keys = []
        self._section_rows = []

    # ------------------------------------------------------------------
    # object モード: フォーム構築（PROPERTIES 駆動）
    # ------------------------------------------------------------------

    def _build_form(self, obj: BaseObject) -> None:
        self._rebuilding = True
        self._updaters = []
        try:
            specs = PROPERTIES.get(obj.type, [])
            for spec in specs:
                if spec.requires is not None and not getattr(obj, spec.requires, None):
                    continue
                # 見出しは PropSpec.section（データ）が持つ。「COMMON_PROPS に
                # 無い最初の key」という位置ベースの推論は、x/y を持たない
                # line/arrow で「スタイル」が p1（始点）行に付くバグを生んでいた
                # （ユーザー報告）。section を独立スパン行として先に addRow する
                # ことで、見出しの有無がフィールド行の高さ（QFormLayout の
                # 上寄せ）に影響しなくなる（項目3/項目4 の主因対策）。
                if spec.section is not None:
                    header_row = self._form.rowCount()
                    self._form.addRow(self._make_section_label(spec.section))
                    self._section_rows.append((header_row, spec.section))
                widget = self._make_widget(obj, spec)
                label_widget = self._make_label_widget(spec, widget)
                field_row = self._form.rowCount()
                self._form.addRow(label_widget, widget)
                self._row_keys[spec.key] = field_row
                self._ordered_keys.append(spec.key)
        finally:
            self._rebuilding = False

    @staticmethod
    def _make_section_label(text: str) -> QLabel:
        """セクション見出し用の独立スパン行ラベルを作る。

        以前は `_HeaderedLabel` で見出しをラベル欄に埋め込んでいたが、
        フィールド行がラベル欄の高さ（見出し2行分）に引っ張られて上寄せされ、
        実ラベル文字と入力欄の中心が最大21pxずれていた（項目3/項目4 の実測）。
        `QFormLayout.addRow(widget)`（スパン行）として完全に独立させることで、
        見出しの有無がフィールド行の高さに一切影響しなくなる。artboard モードの
        見出しもこれを使い、見出し生成を1箇所に統一する。
        """
        label = QLabel(text)
        label.setProperty("role", "section")
        return label

    def _make_label_widget(self, spec: PropSpec, field_widget: QWidget) -> QWidget:
        """number/int/point 行のラベルは ScrubLabel にする（P2契約 §3.4）。

        point 行（field_widget は x/y 2 個の QDoubleSpinBox を持つ横並び
        コンテナ）は、先頭（x側）のスピンボックスをスクラブ対象にする。
        """
        if spec.kind in ("number", "int"):
            return ScrubLabel(spec.label, field_widget)
        if spec.kind == "point":
            spin_x = field_widget.findChildren(QDoubleSpinBox)[0]
            return ScrubLabel(spec.label, spin_x)
        return QLabel(spec.label)

    # ------------------------------------------------------------------
    # 公開ヘルパ（契約 API）: セクション見出しの独立行化により
    # 「PROPERTIES[type] の並び順 == QFormLayout の行番号」という前提が
    # 崩れたため、key から実際の行/ウィジェットを引く経路をここに一本化する
    # （テストと外部からはこの経路を使うこと。itemAt の生の index 引きは
    # 見出し行があると見出しラベル自身を掴んでしまう）。
    # ------------------------------------------------------------------

    def row_for_key(self, key: str) -> int:
        """`key` に対応する `QFormLayout` の行番号（FieldRole 側）を返す。

        見出し行はどの key にも属さないため、この番号は常に実データ行を指す。
        """
        try:
            return self._row_keys[key]
        except KeyError:
            available = ", ".join(sorted(self._row_keys)) or "(none)"
            raise KeyError(f"row not found for key {key!r}; available keys: {available}") from None

    def field_widget_for(self, key: str) -> QWidget:
        """`key` のフィールド側ウィジェットを返す（セクション見出しを返すことはない）。"""
        row = self.row_for_key(key)
        item = self._form.itemAt(row, QFormLayout.ItemRole.FieldRole)
        if item is None:
            # `python -O` で assert が消えても不変条件違反を無言で通さない
            # （`row_for_key` の KeyError と同様、公開ヘルパは例外種別を
            # 安定させる。レビュー所見）。
            raise RuntimeError(f"field item not found for key {key!r} (row={row})")
        widget = item.widget()
        if widget is None:
            raise RuntimeError(f"field widget not found for key {key!r} (row={row})")
        return widget

    def label_widget_for(self, key: str) -> QWidget:
        """`key` のラベル側ウィジェットを返す。"""
        row = self.row_for_key(key)
        item = self._form.itemAt(row, QFormLayout.ItemRole.LabelRole)
        if item is None:
            raise RuntimeError(f"label item not found for key {key!r} (row={row})")
        widget = item.widget()
        if widget is None:
            raise RuntimeError(f"label widget not found for key {key!r} (row={row})")
        return widget

    def keys_in_form(self) -> tuple[str, ...]:
        """現在のフォームの表示順（`requires` で隠れた行は含まない）。"""
        return tuple(self._ordered_keys)

    def section_rows(self) -> tuple[tuple[int, str], ...]:
        """(見出し行の行番号, 見出しテキスト) の列を表示順に返す。"""
        return tuple(self._section_rows)

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
            return self._make_text_widget(obj, spec)
        raise NotImplementedError(f"unknown PropSpec.kind: {spec.kind!r}")

    def _make_number_widget(self, obj: BaseObject, spec: PropSpec) -> QDoubleSpinBox:
        spin = QDoubleSpinBox()
        _pin_control_height(spin)
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
        # ラベル("x"/"y")とスピンの間隔を明示する（P2契約 担当A。Fusion 既定の
        # 詰まり方に委ねず、外側フォームの _FORM_H_SPACING より詰めた値にして
        # 「内側の対」であることを視覚的に示す）。
        hlayout.setSpacing(6)

        point: list[float] = list(getattr(obj, spec.key))
        spin_x = QDoubleSpinBox()
        spin_y = QDoubleSpinBox()
        for spin in (spin_x, spin_y):
            _pin_control_height(spin)
            spin.setKeyboardTracking(False)
            spin.setDecimals(_NUMBER_DECIMALS)
            spin.setMinimum(-_SPIN_RANGE)
            spin.setMaximum(_SPIN_RANGE)
            # line/arrow の始点/終点行はスピン2個を横に並べるため、自然な
            # sizeHint（107px前後/個）のままだとフォーム全体の最小幅が
            # `_PANEL_FIXED_WIDTH` を超える。フォームを包む QScrollArea が
            # `setWidgetResizable(True)` で「ビューポートより小さければ
            # minimumSizeHint まで広げる」ため、この最小幅がパネル幅を
            # 超えると横スクロールバー無しでは右側が見切れる（レビュー所見の
            # 回帰）。ここで最小幅の床を下げておくことで、フォームの最小幅が
            # 常にパネル幅に収まり、QFormLayout の AllNonFixedFieldsGrow が
            # 従来どおりスピンを圧縮してフィットさせる（点行が圧縮されてでも
            # 全部見える、という既存のトレードオフを維持）。80px は小数第1位
            # までの数値（例 "-1234.5"）が入る実用上の下限。
            spin.setMinimumWidth(80)
        spin_x.blockSignals(True)
        spin_x.setValue(float(point[0]))
        spin_x.blockSignals(False)
        spin_y.blockSignals(True)
        spin_y.setValue(float(point[1]))
        spin_y.blockSignals(False)

        # 行高統一後は外側ラベル（ScrubLabel）と同じ AlignVCenter を明示することで、
        # 内側の "x"/"y" ラベルもスピンと中心が一致する（実測で確認済み。
        # QSpinBox.setPrefix 化はスピンの sizeHint を +11px/個 押し上げて幅制約を
        # 圧迫するので採らない）。
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

    def _make_color_widget(self, obj: BaseObject, spec: PropSpec) -> QPushButton:
        """color / color_opt 共通の色ボタンを作る（P2契約: 色ウィジェットの単一化）。

        以前は kind="color_opt" 専用に `_make_color_opt_widget`
        （QCheckBox「透明」+ QPushButton の2部品構成）を持っていたが、これを廃止し
        1個のボタンへ統合した。理由は3つ:
        (1) 1プロパティに2部品出すのはユーザーの「UI部品は最小限」方針に反する。
        (2) QCheckBox は QSS 上の行高が他コントロールと異なり、行高不揃い
            （項目3）の原因の一つだった。
        (3) 「透明」解除時に直前の色をボタンの動的プロパティへ退避・復元する
            隠し状態が、トグルの往復だけで元の色を失う回帰を過去に起こして
            いた。

        nullable（kind=="color_opt"）のときだけボタンに `QMenu` を付け、
        「色を選択…」と `spec.null_label` の2アクションから選ぶ（`exec()` を
        呼ばずに `menu().actions()[i].trigger()` でヘッドレスに駆動できる）。
        非 nullable（kind=="color"）はメニューを付けず `clicked` → 直接ダイアログ
        のまま（従来どおり）。
        """
        nullable = spec.kind == "color_opt"
        button = QPushButton()
        self._apply_button_color(button, getattr(obj, spec.key), spec.null_label)

        def pick_color(
            _checked: bool = False,
            obj: BaseObject = obj,
            spec: PropSpec = spec,
            button: QPushButton = button,
        ) -> None:
            old_value = getattr(obj, spec.key)
            initial = QColor(old_value) if old_value else QColor(_DEFAULT_COLOR)
            # DontUseNativeDialog: ネイティブ色ダイアログの環境では `setCustomColor`
            # で載せたパレットスウォッチが表示されない（所見 S1）。
            color = QColorDialog.getColor(
                initial,
                self,
                "色を選択",
                options=QColorDialog.ColorDialogOption.DontUseNativeDialog,
            )
            if not color.isValid():
                return
            new_value = color.name()
            if new_value == old_value:
                return
            self._apply_button_color(button, new_value, spec.null_label)
            self._push(SetPropertyCommand(self.scene.document, obj, spec.key, new_value, old_value))

        def set_none(
            _checked: bool = False,
            obj: BaseObject = obj,
            spec: PropSpec = spec,
            button: QPushButton = button,
        ) -> None:
            old_value = getattr(obj, spec.key)
            if old_value is None:
                return
            self._apply_button_color(button, None, spec.null_label)
            self._push(SetPropertyCommand(self.scene.document, obj, spec.key, None, old_value))

        if nullable:
            menu = QMenu(button)
            pick_action = menu.addAction("色を選択…")
            pick_action.triggered.connect(pick_color)
            none_action = menu.addAction(spec.null_label)
            none_action.triggered.connect(set_none)
            button.setMenu(menu)
        else:
            button.clicked.connect(pick_color)

        def update_widget(
            button: QPushButton = button, obj: BaseObject = obj, spec: PropSpec = spec
        ) -> None:
            if not shiboken6.isValid(button):
                return
            self._apply_button_color(button, getattr(obj, spec.key), spec.null_label)

        self._updaters.append(update_widget)
        return button

    def _make_enum_widget(self, obj: BaseObject, spec: PropSpec) -> QComboBox:
        combo = QComboBox()
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
            self._push(SetPropertyCommand(self.scene.document, obj, spec.key, new_value, old_value))

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
            self._push(SetPropertyCommand(self.scene.document, obj, spec.key, checked, old_value))

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
            # _commit_scalar 経由にすることで math の latex 編集に box 追従が付く。
            self._commit_scalar(obj, spec.key, new_value, old_value)

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
    # multi モード: 複数選択の共通プロパティ・混在表示（P2契約 §3.6）
    # ------------------------------------------------------------------

    def _multi_common_specs(self, objs: list[BaseObject]) -> list[PropSpec]:
        """選択オブジェクト全型に共通する (key, kind[, options]) を求める。

        表示順は先頭オブジェクトの型の spec 順（PROPERTIES[type] の交差）。
        text/point は対象外。`requires` 付き spec も対象外にする
        （mask_src 等インスタンス依存の可否を型交差だけでは判定できないため）。

        color/color_opt は互換扱いにする（P2契約: 担当C）。rect（fill/stroke が
        color_opt）と line（stroke が color のまま）を同時選択すると、単純な
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
            if spec.kind not in _MULTI_KINDS or spec.requires is not None:
                continue
            ok = True
            kinds = {spec.kind}
            for spec_map in other_spec_maps:
                other = spec_map.get(spec.key)
                if other is None or other.requires is not None:
                    ok = False
                    break
                if other.kind == spec.kind:
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

    def _build_multi_form(self, objs: list[BaseObject]) -> None:
        self._rebuilding = True
        self._updaters = []
        try:
            for spec in self._multi_common_specs(objs):
                widget = self._make_multi_widget(objs, spec)
                row = self._form.rowCount()
                self._form.addRow(QLabel(spec.label), widget)
                # 公開ヘルパ（row_for_key 等）が multi モードでも引けるようにする。
                self._row_keys[spec.key] = row
                self._ordered_keys.append(spec.key)
        finally:
            self._rebuilding = False

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
        raise NotImplementedError(f"multi モード未対応の kind: {spec.kind!r}")

    def _make_multi_number_widget(self, objs: list[BaseObject], spec: PropSpec) -> QDoubleSpinBox:
        spin = _MixedDoubleSpinBox()
        _pin_control_height(spin)
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
            cmds: list[Any] = []
            for o in objs:
                old_value = getattr(o, spec.key)
                if float(old_value) == value:
                    continue
                if spec.key in _GEOMETRY_KEYS:
                    cmds.append(
                        SetGeometryCommand(
                            self.scene.document,
                            o,
                            {spec.key: value},
                            {spec.key: old_value},
                            mergeable=False,
                        )
                    )
                else:
                    cmds.append(
                        SetPropertyCommand(self.scene.document, o, spec.key, value, old_value)
                    )
                    # math の font_size は box 追従を同一マクロに添える（_commit_scalar と対称）。
                    follow = self._math_follow_command(o, spec.key, value)
                    if follow is not None:
                        cmds.append(follow)
            self._push_macro(f"{spec.label}を変更", cmds)

        spin.valueChanged.connect(on_changed)
        self._updaters.append(sync_widget)
        return spin

    def _make_multi_int_widget(self, objs: list[BaseObject], spec: PropSpec) -> QSpinBox:
        spin = _MixedSpinBox()
        _pin_control_height(spin)
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
            cmds: list[Any] = []
            for o in objs:
                old_value = getattr(o, spec.key)
                if int(old_value) == value:
                    continue
                if spec.key in _GEOMETRY_KEYS:
                    cmds.append(
                        SetGeometryCommand(
                            self.scene.document,
                            o,
                            {spec.key: value},
                            {spec.key: old_value},
                            mergeable=False,
                        )
                    )
                else:
                    cmds.append(
                        SetPropertyCommand(self.scene.document, o, spec.key, value, old_value)
                    )
            self._push_macro(f"{spec.label}を変更", cmds)

        spin.valueChanged.connect(on_changed)
        self._updaters.append(sync_widget)
        return spin

    def _make_multi_color_widget(self, objs: list[BaseObject], spec: PropSpec) -> QPushButton:
        """color / color_opt 共通の複数選択用色ボタン（単一選択の `_make_color_widget`
        と同じ理由で統一。P2契約 §3.6）。

        `effective kind`（`_multi_common_specs` が既に決めている）が "color_opt"
        のとき（＝選択全員が color_opt）だけメニューを付ける。rect+line のような
        混在は effective kind が "color" に寄せられているため、ここではメニュー
        無し・直接ダイアログのまま（多数派が None を許容しない対象を含むので、
        一括で None を送る操作を UI 上提示しない）。
        """
        nullable = spec.kind == "color_opt"
        button = QPushButton()

        def sync_widget(button: QPushButton = button) -> None:
            if not shiboken6.isValid(button):
                return
            values = {getattr(o, spec.key) for o in objs}
            if len(values) == 1:
                self._apply_button_color(button, next(iter(values)), spec.null_label)
            else:
                button.setText(_MIXED_TEXT)
                button.setStyleSheet("")
                button.setToolTip(_MIXED_TEXT)

        sync_widget()

        def apply_color(new_value: str | None, button: QPushButton = button) -> None:
            cmds: list[Any] = []
            for o in objs:
                old_value = getattr(o, spec.key)
                if old_value == new_value:
                    continue
                cmds.append(
                    SetPropertyCommand(self.scene.document, o, spec.key, new_value, old_value)
                )
            if not cmds:
                return
            self._apply_button_color(button, new_value, spec.null_label)
            self._push_macro(f"{spec.label}を変更", cmds)

        def pick_color(_checked: bool = False, button: QPushButton = button) -> None:
            values = {getattr(o, spec.key) for o in objs}
            current = next(iter(values)) if len(values) == 1 else None
            initial = QColor(current) if current else QColor(_DEFAULT_COLOR)
            # DontUseNativeDialog: ネイティブ色ダイアログの環境では `setCustomColor`
            # で載せたパレットスウォッチが表示されない（所見 S1）。
            color = QColorDialog.getColor(
                initial,
                self,
                "色を選択",
                options=QColorDialog.ColorDialogOption.DontUseNativeDialog,
            )
            if not color.isValid():
                return
            apply_color(color.name())

        if nullable:
            menu = QMenu(button)
            pick_action = menu.addAction("色を選択…")
            pick_action.triggered.connect(pick_color)
            none_action = menu.addAction(spec.null_label)
            none_action.triggered.connect(lambda: apply_color(None))
            button.setMenu(menu)
        else:
            button.clicked.connect(pick_color)

        self._updaters.append(sync_widget)
        return button

    def _make_multi_enum_widget(self, objs: list[BaseObject], spec: PropSpec) -> QComboBox:
        combo = QComboBox()
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
            # ユーザー操作後は2状態に戻す（P2契約 §3.6）。
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

    # ------------------------------------------------------------------
    # artboard モード: アートボード設定フォーム（P2契約 §3.5）
    # ------------------------------------------------------------------

    def _build_artboard_form(self) -> None:
        self._rebuilding = True
        self._updaters = []
        try:
            header_row = self._form.rowCount()
            self._form.addRow(self._make_section_label("アートボード"))
            self._section_rows.append((header_row, "アートボード"))

            preset_combo = QComboBox()
            for label, *_rest in ARTBOARD_PRESETS:
                preset_combo.addItem(label)
            preset_combo.addItem(ARTBOARD_CUSTOM_LABEL)
            # 最長のプリセット名（例 "A4 (210×297mm)"）がそのまま
            # minimumSizeHint になり、ラベル分と合わせてフォームの最小幅が
            # `_PANEL_FIXED_WIDTH` を超えてしまう（line/arrow の point 行と
            # 同じ構造の問題、レビュー所見）。フォームを包む QScrollArea が
            # `setWidgetResizable(True)` でこの最小幅まで広げるため、床を
            # 下げてパネル幅に収める（閉じた状態では省略記号で切れるが、
            # 開けば全文が見える）。
            preset_combo.setMinimumWidth(150)

            width_mm_spin = QDoubleSpinBox()
            _pin_control_height(width_mm_spin)
            width_mm_spin.setKeyboardTracking(False)
            width_mm_spin.setRange(1.0, 2000.0)
            width_mm_spin.setDecimals(1)

            dpi_spin = QSpinBox()
            _pin_control_height(dpi_spin)
            dpi_spin.setKeyboardTracking(False)
            dpi_spin.setRange(1, 2400)

            width_px_spin = QSpinBox()
            _pin_control_height(width_px_spin)
            width_px_spin.setKeyboardTracking(False)
            width_px_spin.setRange(1, 20000)

            height_px_spin = QSpinBox()
            _pin_control_height(height_px_spin)
            height_px_spin.setKeyboardTracking(False)
            height_px_spin.setRange(1, 20000)

            bg_button = QPushButton()

            def sync_widget(
                preset_combo: QComboBox = preset_combo,
                width_mm_spin: QDoubleSpinBox = width_mm_spin,
                dpi_spin: QSpinBox = dpi_spin,
                width_px_spin: QSpinBox = width_px_spin,
                height_px_spin: QSpinBox = height_px_spin,
                bg_button: QPushButton = bg_button,
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
                self._apply_button_color(bg_button, artboard.background)

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
                # プリセット選択は4フィールドをまとめて1コマンド（P2契約 §3.5）。
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
                preset_combo: QComboBox = preset_combo,
                width_mm_spin: QDoubleSpinBox = width_mm_spin,
                dpi_spin: QSpinBox = dpi_spin,
                width_px_spin: QSpinBox = width_px_spin,
                height_px_spin: QSpinBox = height_px_spin,
            ) -> None:
                if self._rebuilding:
                    return
                old_artboard = self.scene.document.artboard
                new_artboard = Artboard(
                    width_px=width_px_spin.value(),
                    height_px=height_px_spin.value(),
                    physical=Physical(width_mm=width_mm_spin.value(), target_dpi=dpi_spin.value()),
                    background=old_artboard.background,
                )
                if new_artboard == old_artboard:
                    return
                preset_combo.blockSignals(True)
                preset_combo.setCurrentIndex(self._matching_preset_index(new_artboard))
                preset_combo.blockSignals(False)
                push_artboard(new_artboard)

            def on_bg_click(_checked: bool = False, bg_button: QPushButton = bg_button) -> None:
                old_artboard = self.scene.document.artboard
                color = QColorDialog.getColor(
                    QColor(old_artboard.background),
                    self,
                    "背景色",
                    options=QColorDialog.ColorDialogOption.DontUseNativeDialog,
                )
                if not color.isValid():
                    return
                new_value = color.name()
                if new_value == old_artboard.background:
                    return
                new_artboard = Artboard(
                    width_px=old_artboard.width_px,
                    height_px=old_artboard.height_px,
                    physical=old_artboard.physical,
                    background=new_value,
                )
                self._apply_button_color(bg_button, new_value)
                push_artboard(new_artboard, text="背景色")

            preset_combo.currentIndexChanged.connect(on_preset_changed)
            width_mm_spin.valueChanged.connect(on_field_changed)
            dpi_spin.valueChanged.connect(on_field_changed)
            width_px_spin.valueChanged.connect(on_field_changed)
            height_px_spin.valueChanged.connect(on_field_changed)
            bg_button.clicked.connect(on_bg_click)

            # 公開ヘルパ（row_for_key 等）が artboard モードでも引けるようにする。
            for label, widget, key in (
                ("プリセット", preset_combo, "preset"),
                ("幅 (mm)", width_mm_spin, "width_mm"),
                ("解像度 (dpi)", dpi_spin, "dpi"),
                ("幅 (px)", width_px_spin, "width_px"),
                ("高さ (px)", height_px_spin, "height_px"),
                ("背景色", bg_button, "background"),
            ):
                row = self._form.rowCount()
                self._form.addRow(label, widget)
                self._row_keys[key] = row
                self._ordered_keys.append(key)

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

    def _commit_scalar(self, obj: BaseObject, key: str, new_value: Any, old_value: Any) -> None:
        if key in _GEOMETRY_KEYS:
            cmd = SetGeometryCommand(
                self.scene.document, obj, {key: new_value}, {key: old_value}, mergeable=True
            )
            self._push(cmd)
            return
        cmd = SetPropertyCommand(self.scene.document, obj, key, new_value, old_value)
        follow = self._math_follow_command(obj, key, new_value)
        if follow is not None:
            # math の box は自然サイズ×表示倍率の派生値なので、latex/font_size の
            # 変更には寸法追従を同一マクロで添える（math_item.follow_math_box 参照）。
            self._push_macro("数式を変更", [cmd, follow])
            return
        self._push(cmd)

    def _math_follow_command(self, obj: BaseObject, key: str, new_value: Any) -> Any | None:
        """math の latex/font_size 変更に伴う box 追従の SetGeometryCommand を返す。

        追従が不要（math 以外・寸法に効かないキー・変化が微小・不正 latex）なら None。
        """
        if obj.type != "math" or key not in ("latex", "font_size"):
            return None
        from app.scene.items.math_item import follow_math_box

        new_latex = str(new_value) if key == "latex" else obj.latex
        new_font_size = float(new_value) if key == "font_size" else float(obj.font_size)
        follow = follow_math_box(
            obj.latex,
            float(obj.font_size),
            new_latex,
            new_font_size,
            obj.color,
            float(obj.width),
            float(obj.height),
        )
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
        空マクロを作らないため）。1確定 = 1マクロ = undo 1回（P2契約 §3.6）。
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

    @staticmethod
    def _apply_button_color(
        button: QPushButton, color: str | None, null_label: str = "なし"
    ) -> None:
        """ボタンの見た目をモデル値（色 or None）に同期する。

        以前は `setText(color_str)` で hex を文字として出していたが、これは
        `sizeHint` を値依存にしてしまい、色を変えるたびに行高/行幅が揺れる
        （項目3「高さが揃っていない」と衝突する）。色ありは空文字テキスト +
        `background-color` + tooltip に hex、null（線・塗りなし）は
        `null_label` をテキストに出し `background-color` を外す（色面ではなく
        通常ボタンの見た目に戻す）。`null_label` の既定 "なし" は artboard の
        背景色ボタン等（常に非 null）からの既存の2引数呼び出しをそのまま通す
        ためのもの。
        """
        if color is None:
            button.setText(null_label)
            button.setStyleSheet("")
            button.setToolTip(null_label)
            return
        button.setText("")
        button.setStyleSheet(f"background-color: {color};")
        button.setToolTip(color)
