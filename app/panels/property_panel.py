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
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QPushButton,
    QSpinBox,
    QToolButton,
    QVBoxLayout,
    QWidget,
)

from app.commands.commands import SetArtboardCommand, SetGeometryCommand, SetPropertyCommand
from app.model.document import Artboard, Physical
from app.model.objects import BaseObject
from app.model.properties import COMMON_PROPS, PROPERTIES, PropSpec
from app.ui.artboard_presets import ARTBOARD_CUSTOM_LABEL, ARTBOARD_PRESETS, preset_px_size
from app.ui.theme import icons
from app.ui.widgets.scrub_label import ScrubLabel

if TYPE_CHECKING:
    from app.scene.canvas_scene import CanvasScene
    from app.ui.controllers.edit_controller import EditController

# x/y/width/height/rotation は SetGeometryCommand、それ以外は SetPropertyCommand（§9.1）。
_GEOMETRY_KEYS = {"x", "y", "width", "height", "rotation"}

_SPIN_RANGE = 1_000_000.0
_DEFAULT_COLOR = "#000000"
_COLOR_PROP = "charta_color"
_MIXED_TEXT = "混在"
# 要件: 数値スピナーの表示・入力は小数第1位まで（int の QSpinBox は対象外）。
# ただし step が 0.1 未満の項目（opacity/brightness/contrast/smoothing、
# properties.py の step=0.05 指定）はこの丸めだと刻み幅が実質失われる
# （例: setValue(0.85)->0.8, stepBy(-1) が 1.0->0.9 になり singleStep と
# 表示が食い違う）ため、PropSpec.decimals で個別に上書きできるようにする。
_NUMBER_DECIMALS = 1

# パネル固定幅（選択対象に依らず一定にする要件への対応）。
# 実測（QT_QPA_PLATFORM=offscreen, decimals=1 適用後）: 9 種別すべての
# PropertyPanel.sizeHint().width() のうち line/arrow（point ウィジェット
# ×2 行を含む）が最大で 332px。他の全種別・未選択・複数選択はこれより
# 小さい。この最大値にフォント差など環境差のマージンを載せて 340px とする
# （setFixedWidth は最小=最大幅を固定するため、これより狭いとラベル/
# フィールドが切り詰められる — 実測値を下回らないことが必須）。
# P2 で整列行/artboard モード/セクション見出しを追加した後も 340px に収まる
# ことを実測で確認済み（§3.7、tests/test_panel_layout.py が定数 import で検証）。
_PANEL_FIXED_WIDTH = 340

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

# multi モードで対象にする kind（point/text/color_opt は対象外、P2契約 §3.6）。
_MULTI_KINDS = frozenset({"number", "int", "color", "enum", "bool"})


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


class _HeaderedLabel(QWidget):
    """セクション見出し + 実ラベルを縦に積んだ複合ラベル欄（P2契約 §3.4）。

    `QFormLayout` の行数を変えずに「見出し行を挿入した」見た目を得るため、
    見出しはこの行自身のラベル欄（`LabelRole`）にだけ埋め込み、フィールド欄
    （`FieldRole`）は一切変更しない。これは `tests/test_panel_layout.py` /
    `tests/test_panel_edit_m8.py` / `tests/test_interaction.py` が
    「`PROPERTIES[type]` の並び順 == `QFormLayout` の行番号」という前提で
    `_form.itemAt(row, FieldRole)` / `_form.rowCount()` を検証しているため
    （見出しを独立した行として `addRow` すると行番号がずれてこれらが壊れる）。
    `tests/test_type_anchors.py` は `LabelRole` 側ウィジェットの `.text()` を
    呼ぶため、実ラベルへ委譲する `text()` を用意して互換性を保つ。
    """

    def __init__(
        self, header_text: str, label_widget: QWidget, parent: QWidget | None = None
    ) -> None:
        super().__init__(parent)
        self._label_widget = label_widget
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(2)
        header = QLabel(header_text)
        header.setProperty("role", "section")
        layout.addWidget(header)
        layout.addWidget(label_widget)

    def text(self) -> str:
        get_text = getattr(self._label_widget, "text", None)
        return get_text() if callable(get_text) else ""


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

        self._align_row = self._build_align_row()
        self._info_label = QLabel("")
        self._form = QFormLayout()
        self._form_widget = QWidget()
        self._form_widget.setLayout(self._form)

        layout = QVBoxLayout(self)
        layout.addWidget(self._align_row)
        layout.addWidget(self._info_label)
        layout.addWidget(self._form_widget)
        layout.addStretch(1)

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

    # ------------------------------------------------------------------
    # object モード: フォーム構築（PROPERTIES 駆動）
    # ------------------------------------------------------------------

    def _build_form(self, obj: BaseObject) -> None:
        self._rebuilding = True
        self._updaters = []
        try:
            specs = PROPERTIES.get(obj.type, [])
            common_keys = {p.key for p in COMMON_PROPS}
            # 「スタイル」見出しの挿入位置: COMMON_PROPS に無い最初の key
            # （P2契約 §3.4）。line/arrow/connector のように x を持たない型では
            # 「変形」見出しは自然と発生しない（spec.key=="x" が存在しないため）。
            style_header_index = next(
                (i for i, s in enumerate(specs) if s.key not in common_keys), None
            )
            for i, spec in enumerate(specs):
                if spec.requires is not None and not getattr(obj, spec.requires, None):
                    continue
                widget = self._make_widget(obj, spec)
                label_widget = self._make_label_widget(spec, widget)
                header_text: str | None = None
                if spec.key == "x":
                    header_text = "変形"
                elif i == style_header_index:
                    header_text = "スタイル"
                if header_text is not None:
                    label_widget = _HeaderedLabel(header_text, label_widget)
                self._form.addRow(label_widget, widget)
        finally:
            self._rebuilding = False

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
    # ウィジェット生成（kind ごと、object モード用）
    # ------------------------------------------------------------------

    def _make_widget(self, obj: BaseObject, spec: PropSpec) -> QWidget:
        if spec.kind == "number":
            return self._make_number_widget(obj, spec)
        if spec.kind == "int":
            return self._make_int_widget(obj, spec)
        if spec.kind == "point":
            return self._make_point_widget(obj, spec)
        if spec.kind == "color":
            return self._make_color_widget(obj, spec)
        if spec.kind == "color_opt":
            return self._make_color_opt_widget(obj, spec)
        if spec.kind == "enum":
            return self._make_enum_widget(obj, spec)
        if spec.kind == "bool":
            return self._make_bool_widget(obj, spec)
        if spec.kind == "text":
            return self._make_text_widget(obj, spec)
        raise NotImplementedError(f"unknown PropSpec.kind: {spec.kind!r}")

    def _make_number_widget(self, obj: BaseObject, spec: PropSpec) -> QDoubleSpinBox:
        spin = QDoubleSpinBox()
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
        hlayout = QHBoxLayout(container)
        hlayout.setContentsMargins(0, 0, 0, 0)

        point: list[float] = list(getattr(obj, spec.key))
        spin_x = QDoubleSpinBox()
        spin_y = QDoubleSpinBox()
        for spin in (spin_x, spin_y):
            spin.setKeyboardTracking(False)
            spin.setDecimals(_NUMBER_DECIMALS)
            spin.setMinimum(-_SPIN_RANGE)
            spin.setMaximum(_SPIN_RANGE)
        spin_x.blockSignals(True)
        spin_x.setValue(float(point[0]))
        spin_x.blockSignals(False)
        spin_y.blockSignals(True)
        spin_y.setValue(float(point[1]))
        spin_y.blockSignals(False)

        hlayout.addWidget(QLabel("x"))
        hlayout.addWidget(spin_x)
        hlayout.addWidget(QLabel("y"))
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
        button = QPushButton()
        self._apply_button_color(button, getattr(obj, spec.key) or _DEFAULT_COLOR)

        def on_click(
            _checked: bool = False,
            obj: BaseObject = obj,
            spec: PropSpec = spec,
            button: QPushButton = button,
        ) -> None:
            old_value = getattr(obj, spec.key)
            initial = QColor(old_value) if old_value else QColor(_DEFAULT_COLOR)
            color = QColorDialog.getColor(initial, self, "色を選択")
            if not color.isValid():
                return
            new_value = color.name()
            if new_value == old_value:
                return
            self._apply_button_color(button, new_value)
            self._push(SetPropertyCommand(self.scene.document, obj, spec.key, new_value, old_value))

        button.clicked.connect(on_click)

        def update_widget(
            button: QPushButton = button, obj: BaseObject = obj, spec: PropSpec = spec
        ) -> None:
            if not shiboken6.isValid(button):
                return
            self._apply_button_color(button, getattr(obj, spec.key) or _DEFAULT_COLOR)

        self._updaters.append(update_widget)
        return button

    def _make_color_opt_widget(self, obj: BaseObject, spec: PropSpec) -> QWidget:
        container = QWidget()
        hlayout = QHBoxLayout(container)
        hlayout.setContentsMargins(0, 0, 0, 0)

        current: str | None = getattr(obj, spec.key)
        checkbox = QCheckBox("透明")
        button = QPushButton()
        checkbox.blockSignals(True)
        checkbox.setChecked(current is None)
        checkbox.blockSignals(False)
        button.setEnabled(current is not None)
        self._apply_button_color(button, current or _DEFAULT_COLOR)

        hlayout.addWidget(checkbox)
        hlayout.addWidget(button)

        def on_toggle(
            checked: bool,
            obj: BaseObject = obj,
            spec: PropSpec = spec,
            button: QPushButton = button,
        ) -> None:
            button.setEnabled(not checked)
            if self._rebuilding:
                return
            old_value = getattr(obj, spec.key)
            stored_color = button.property(_COLOR_PROP) or _DEFAULT_COLOR
            new_value: str | None = None if checked else stored_color
            if new_value == old_value:
                return
            self._push(SetPropertyCommand(self.scene.document, obj, spec.key, new_value, old_value))

        def on_click(
            _checked: bool = False,
            obj: BaseObject = obj,
            spec: PropSpec = spec,
            button: QPushButton = button,
        ) -> None:
            old_value = getattr(obj, spec.key)
            initial = QColor(old_value) if old_value else QColor(_DEFAULT_COLOR)
            color = QColorDialog.getColor(initial, self, "色を選択")
            if not color.isValid():
                return
            new_value = color.name()
            if new_value == old_value:
                return
            self._apply_button_color(button, new_value)
            self._push(SetPropertyCommand(self.scene.document, obj, spec.key, new_value, old_value))

        checkbox.toggled.connect(on_toggle)
        button.clicked.connect(on_click)

        def update_widget(
            checkbox: QCheckBox = checkbox,
            button: QPushButton = button,
            obj: BaseObject = obj,
            spec: PropSpec = spec,
        ) -> None:
            if not shiboken6.isValid(checkbox) or not shiboken6.isValid(button):
                return
            current: str | None = getattr(obj, spec.key)
            checkbox.blockSignals(True)
            try:
                checkbox.setChecked(current is None)
            finally:
                checkbox.blockSignals(False)
            button.setEnabled(current is not None)
            # current が None（透明）の間はボタンの色/_COLOR_PROP に触れない。
            # ここで _DEFAULT_COLOR を書き込んでしまうと、on_toggle が
            # 「直前の色を覚えておいて透明解除時に復元する」ために読む
            # button.property(_COLOR_PROP) を上書きしてしまい、透明トグルの
            # on/off を往復するだけで元の色を失う（実際に発生した回帰）。
            if current is not None:
                self._apply_button_color(button, current)

        self._updaters.append(update_widget)
        return container

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
            self._push(SetPropertyCommand(self.scene.document, obj, spec.key, new_value, old_value))

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
        text/point/color_opt は対象外。`requires` 付き spec も対象外にする
        （mask_src 等インスタンス依存の可否を型交差だけでは判定できないため）。
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
            for spec_map in other_spec_maps:
                other = spec_map.get(spec.key)
                if other is None or other.kind != spec.kind or other.options != spec.options:
                    ok = False
                    break
            if ok:
                result.append(spec)
        return result

    def _build_multi_form(self, objs: list[BaseObject]) -> None:
        self._rebuilding = True
        self._updaters = []
        try:
            for spec in self._multi_common_specs(objs):
                widget = self._make_multi_widget(objs, spec)
                self._form.addRow(QLabel(spec.label), widget)
        finally:
            self._rebuilding = False

    def _make_multi_widget(self, objs: list[BaseObject], spec: PropSpec) -> QWidget:
        if spec.kind == "number":
            return self._make_multi_number_widget(objs, spec)
        if spec.kind == "int":
            return self._make_multi_int_widget(objs, spec)
        if spec.kind == "color":
            return self._make_multi_color_widget(objs, spec)
        if spec.kind == "enum":
            return self._make_multi_enum_widget(objs, spec)
        if spec.kind == "bool":
            return self._make_multi_bool_widget(objs, spec)
        raise NotImplementedError(f"multi モード未対応の kind: {spec.kind!r}")

    def _make_multi_number_widget(self, objs: list[BaseObject], spec: PropSpec) -> QDoubleSpinBox:
        spin = _MixedDoubleSpinBox()
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
            self._push_macro(f"{spec.label}を変更", cmds)

        spin.valueChanged.connect(on_changed)
        self._updaters.append(sync_widget)
        return spin

    def _make_multi_int_widget(self, objs: list[BaseObject], spec: PropSpec) -> QSpinBox:
        spin = _MixedSpinBox()
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
        button = QPushButton()

        def sync_widget(button: QPushButton = button) -> None:
            if not shiboken6.isValid(button):
                return
            values = {getattr(o, spec.key) for o in objs}
            if len(values) == 1:
                self._apply_button_color(button, next(iter(values)) or _DEFAULT_COLOR)
            else:
                button.setText(_MIXED_TEXT)
                button.setStyleSheet("")
                button.setProperty(_COLOR_PROP, None)

        sync_widget()

        def on_click(_checked: bool = False, button: QPushButton = button) -> None:
            values = {getattr(o, spec.key) for o in objs}
            initial = QColor(next(iter(values))) if len(values) == 1 else QColor(_DEFAULT_COLOR)
            color = QColorDialog.getColor(initial, self, "色を選択")
            if not color.isValid():
                return
            new_value = color.name()
            cmds: list[Any] = []
            for o in objs:
                old_value = getattr(o, spec.key)
                if old_value == new_value:
                    continue
                cmds.append(
                    SetPropertyCommand(self.scene.document, o, spec.key, new_value, old_value)
                )
            self._apply_button_color(button, new_value)
            self._push_macro(f"{spec.label}を変更", cmds)

        button.clicked.connect(on_click)
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
            header = QLabel("アートボード")
            header.setProperty("role", "section")
            self._form.addRow(header)

            preset_combo = QComboBox()
            for label, *_rest in ARTBOARD_PRESETS:
                preset_combo.addItem(label)
            preset_combo.addItem(ARTBOARD_CUSTOM_LABEL)

            width_mm_spin = QDoubleSpinBox()
            width_mm_spin.setKeyboardTracking(False)
            width_mm_spin.setRange(1.0, 2000.0)
            width_mm_spin.setDecimals(1)

            dpi_spin = QSpinBox()
            dpi_spin.setKeyboardTracking(False)
            dpi_spin.setRange(1, 2400)

            width_px_spin = QSpinBox()
            width_px_spin.setKeyboardTracking(False)
            width_px_spin.setRange(1, 20000)

            height_px_spin = QSpinBox()
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
                color = QColorDialog.getColor(QColor(old_artboard.background), self, "背景色")
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

            self._form.addRow("プリセット", preset_combo)
            self._form.addRow("幅 (mm)", width_mm_spin)
            self._form.addRow("解像度 (dpi)", dpi_spin)
            self._form.addRow("幅 (px)", width_px_spin)
            self._form.addRow("高さ (px)", height_px_spin)
            self._form.addRow("背景色", bg_button)

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
        else:
            cmd = SetPropertyCommand(self.scene.document, obj, key, new_value, old_value)
        self._push(cmd)

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
    def _apply_button_color(button: QPushButton, color_str: str) -> None:
        button.setText(color_str)
        button.setStyleSheet(f"background-color: {color_str};")
        button.setProperty(_COLOR_PROP, color_str)
