"""FontFamilyCombo: インストール済みフォントのドロップダウン（P2契約 D1-5・要望1）。

`QFontComboBox` は `setCurrentFont` が未インストールのフォントを別のフォントへ
黙って置き換える（実測: "Helvetica"→"Nimbus Sans [urw]"、"Arial"→"Arimo"、
"NoSuchFontXYZ"→"Noto Sans"。`reports/panel.md` §1）。モデルに保存された値を
黙って書き換えないため、このコンボは非編集式で一覧を自前で構築し、未インストール
の値は専用の一時項目として表示する（選択しても既存の一覧へ紛れ込ませない）。
"""

from __future__ import annotations

from typing import Any

from PySide6.QtCore import QModelIndex, QSize, Qt, Signal
from PySide6.QtGui import QColor, QFont, QFontDatabase, QFontMetrics, QPalette
from PySide6.QtWidgets import (
    QApplication,
    QComboBox,
    QListView,
    QStyle,
    QStyledItemDelegate,
    QStyleOptionViewItem,
    QWidget,
)

from app.model.fonts import strip_foundry_suffix, unique_families
from app.ui.widgets.wheel_guard import install_wheel_guard

_MIXED_PLACEHOLDER = "混在"
_ROW_PADDING_PX = 8
_SAMPLE_TEXT = "AaBb"
_SAMPLE_GAP_PX = 12
_SAMPLE_ALPHA = 140
# `QFontDatabase` はテストで擬似実装に差し替えられることがある（symbol系フォントの
# 検証、finding #9）。列挙値そのものは差し替えの影響を受けないので、実行時に
# （差し替え後の）`QFontDatabase.WritingSystem` を読みに行かず、import 時点の
# 本物を捕まえておく。
_WS = QFontDatabase.WritingSystem


class _FontNameDelegate(QStyledItemDelegate):
    """一覧の各行を「名前は常に UI フォント・右にそのファミリでのサンプル」で描く。

    symbol 系フォント（cmex10/cmsy10/msam10/wasy10/STIX*Sym 等）は ASCII の
    コードポイントに記号グリフを割り当てているため、そのフォント自身の書体で
    家族名を描くと名前そのものが読めなくなる（finding #9、`QFontComboBox` の
    描画方式に倣う）。行の高さも一律にする——フォントごとのメトリクスに任せると
    cmex10 だけ極端に高くなる等、560行のリストがガタつく（finding #8 の一部）。
    """

    def __init__(self, combo: QComboBox) -> None:
        super().__init__(combo)
        self._combo = combo
        self._sample_font_cache: dict[str, QFont] = {}
        self._writing_systems_cache: dict[str, tuple[Any, ...]] = {}

    def _name_font_for(self, index: QModelIndex) -> QFont:  # noqa: ARG002 (テストの差し替え口)
        """フォント名を描くフォント。常に一覧自身の（UI）フォントで、家族依存しない。"""
        return self._combo.font()

    def _writing_systems(self, family: str) -> tuple[Any, ...]:
        systems = self._writing_systems_cache.get(family)
        if systems is None:
            systems = tuple(QFontDatabase.writingSystems(family))
            self._writing_systems_cache[family] = systems
        return systems

    def _sample_font_for(self, family: str) -> QFont:
        font = self._sample_font_cache.get(family)
        if font is None:
            font = QFont(family)
            font.setPointSize(self._combo.font().pointSize())
            self._sample_font_cache[family] = font
        return font

    def _sample_text_for(self, family: str) -> str:
        sample = _SAMPLE_TEXT
        if _WS.Japanese in self._writing_systems(family):
            sample += " あ"
        return sample

    def sizeHint(  # noqa: N802 (Qt override)
        self, option: QStyleOptionViewItem, index: QModelIndex
    ) -> QSize:
        metrics = QFontMetrics(self._combo.font())
        width = super().sizeHint(option, index).width()
        return QSize(width, metrics.height() + _ROW_PADDING_PX)

    def paint(  # noqa: N802 (Qt override)
        self, painter: Any, option: QStyleOptionViewItem, index: QModelIndex
    ) -> None:
        opt = QStyleOptionViewItem(option)
        self.initStyleOption(opt, index)
        name = str(index.data(Qt.ItemDataRole.DisplayRole) or "")
        family = str(index.data(Qt.ItemDataRole.UserRole) or name)
        # 既定の drawControl はテキストも描くので、背景/選択ハイライトだけ
        # 描かせ、テキストは（フォントを使い分けるため）自前で上書きする。
        opt.text = ""
        widget = opt.widget
        style = widget.style() if widget is not None else QApplication.style()
        style.drawControl(QStyle.ControlElement.CE_ItemViewItem, opt, painter, widget)

        painter.save()
        try:
            selected = bool(opt.state & QStyle.StateFlag.State_Selected)
            role = QPalette.ColorRole.HighlightedText if selected else QPalette.ColorRole.Text
            text_color = opt.palette.color(QPalette.ColorGroup.Normal, role)
            rect = opt.rect.adjusted(4, 0, -4, 0)

            name_font = self._name_font_for(index)
            painter.setFont(name_font)
            painter.setPen(text_color)
            name_align = int(Qt.AlignmentFlag.AlignVCenter | Qt.AlignmentFlag.AlignLeft)
            painter.drawText(rect, name_align, name)
            name_width = QFontMetrics(name_font).horizontalAdvance(name)

            sample_rect = rect.adjusted(name_width + _SAMPLE_GAP_PX, 0, 0, 0)
            if sample_rect.width() > 0:
                sample_font = self._sample_font_for(family)
                sample_metrics = QFontMetrics(sample_font)
                sample_color = QColor(text_color)
                sample_color.setAlpha(_SAMPLE_ALPHA)
                painter.setFont(sample_font)
                painter.setPen(sample_color)
                elided = sample_metrics.elidedText(
                    self._sample_text_for(family),
                    Qt.TextElideMode.ElideRight,
                    sample_rect.width(),
                )
                painter.drawText(
                    sample_rect,
                    int(Qt.AlignmentFlag.AlignVCenter | Qt.AlignmentFlag.AlignLeft),
                    elided,
                )
        finally:
            painter.restore()


class FontFamilyCombo(QComboBox):
    """インストール済みフォントファミリを選ぶ、編集不可のドロップダウン。"""

    # ユーザー操作（`activated`）でだけ発火。値は `strip_foundry_suffix` 済み。
    family_chosen = Signal(str)

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setEditable(False)
        # 560 件規模のリストでもパネル最小幅（340px 以内）を保つための固定
        # コンテンツ長（`reports/panel.md` §1 で実測: 243px → 204-233px）。
        self.setMinimumContentsLength(8)
        self.setSizeAdjustPolicy(QComboBox.SizeAdjustPolicy.AdjustToMinimumContentsLengthWithIcon)
        self.setPlaceholderText(_MIXED_PLACEHOLDER)
        # クリックで選んだ直後もフォーカスが残るため、既定のホイールガードでは
        # フォーカス中に約560件の一覧を1段ずつ回されてしまう
        # （`_make_font_family_widget` の説明・finding #4）。
        install_wheel_guard(self, forward_when_focused=True)
        view = self.view()
        if isinstance(view, QListView):
            # `_populate()` 前に付ける（QFontComboBox もこの順序）。付けないと
            # 一覧がパネルへ実際に置かれた瞬間に約560項目を各々の書体で採寸し、
            # 選択1回ごとに約190msのUI固まりが生じる（finding #8）。
            view.setUniformItemSizes(True)
        self.setItemDelegate(_FontNameDelegate(self))
        self._uninstalled_index: int | None = None
        self._populate()
        self.activated.connect(self._on_activated)

    def _populate(self) -> None:
        families = unique_families(QFontDatabase.families())
        for family in families:
            self.addItem(family, userData=family)
            # 各項目を Qt.FontRole でそのフォント自身の書体に切り替えることは
            # しない: symbol系フォント（cmex10 等）は ASCII に記号グリフを割り
            # 当てているため、名前そのものが読めなくなる（finding #9）。名前は
            # 常に UI フォントで、右にそのファミリのサンプルを添えるのは
            # `_FontNameDelegate` の役目。

    def _on_activated(self, index: int) -> None:
        value = self.itemData(index)
        if value is None:
            return
        self.family_chosen.emit(strip_foundry_suffix(str(value)))

    def set_family(self, name: str | None) -> None:
        """シグナルを出さずに現在値を設定する。

        インストール済みなら、ファウンドリ接尾辞を除いた上での完全一致
        （casefold）で選択する（`name` 自身に接尾辞が付いていても構わない。
        finding #3）。未インストールなら「`name`（未インストール）」という
        一時項目を先頭に差し込んで選択する（`QFontComboBox` のように別
        フォントへ黙って置き換えない）。`None` は複数選択の混在状態
        （プレースホルダ「混在」を表示）。
        """
        self.blockSignals(True)
        try:
            self._clear_uninstalled_item()
            if name is None:
                self.setCurrentIndex(-1)
                return
            index = self._find_family_index(name)
            if index is not None:
                self.setCurrentIndex(index)
                return
            self.insertItem(0, f"{name}（未インストール）", userData=name)
            self._uninstalled_index = 0
            self.setCurrentIndex(0)
        finally:
            self.blockSignals(False)

    def family(self) -> str | None:
        """現在選択中のファミリ名（生の保存値）。混在（index==-1）なら `None`。"""
        index = self.currentIndex()
        if index < 0:
            return None
        value = self.itemData(index)
        return str(value) if value is not None else None

    def _find_family_index(self, name: str) -> int | None:
        # `name` 自体にファウンドリ接尾辞が残っていることがある（エージェント経由の
        # update_objects・旧バージョンの自由入力パネル・手編集の prefs.json 由来）。
        # 一覧の userData は `_populate()` で既に strip 済みなので、比較側も揃えて
        # strip しないと、実際にはインストール済みのフォントが「未インストール」
        # 扱いになってしまう（finding #3）。
        target = strip_foundry_suffix(name).casefold()
        for i in range(self.count()):
            data = self.itemData(i)
            if data is not None and str(data).casefold() == target:
                return i
        return None

    def _clear_uninstalled_item(self) -> None:
        if self._uninstalled_index is not None:
            self.removeItem(self._uninstalled_index)
            self._uninstalled_index = None
