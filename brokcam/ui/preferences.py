"""Diálogo de Preferências gerado a partir de todos os ``ParamGroup`` registrados."""
from __future__ import annotations

from PySide6.QtCore import Qt, Signal
from PySide6.QtWidgets import (
    QDialog, QDialogButtonBox, QFrame, QHBoxLayout, QLineEdit, QListWidget, QListWidgetItem,
    QPushButton, QScrollArea, QStackedWidget, QVBoxLayout, QWidget,
)

from brokcam.core.params import ParamGroup

from .help_dialogs import collect_groups
from .icons import icon
from .param_form import HelpPanel, ParamForm
from .settings import UI_GROUP, AppSettings

GROUP_ICONS = {"ui": "settings", "isolation": "isolation", "ncc": "ncc", "cutout": "cutout",
               "drilling": "drill", "panelize": "panelize", "grbl_post": "gcode", "grbl": "gcode",
               "probe_z": "probe", "gcode_import": "import"}


class PreferencesDialog(QDialog):
    applied = Signal()

    def __init__(self, settings: AppSettings, parent: QWidget | None = None, start_key: str | None = None) -> None:
        super().__init__(parent)
        self.setWindowTitle("Preferências — BrokCAM")
        self.resize(980, 700)
        self.settings = settings
        groups = [g for g in collect_groups() if g.key != "probe_z"]
        groups.sort(key=lambda g: (g.key != "ui", g.title))
        self.groups: list[ParamGroup] = groups
        self.forms: dict[str, ParamForm] = {}

        root = QVBoxLayout(self)
        root.setContentsMargins(0, 0, 0, 0)
        root.setSpacing(0)
        body = QHBoxLayout()
        body.setSpacing(0)
        left = QVBoxLayout()
        left.setContentsMargins(10, 10, 6, 10)
        left.setSpacing(8)
        self.filter = QLineEdit()
        self.filter.setPlaceholderText("Filtrar categorias…")
        self.filter.setClearButtonEnabled(True)
        self.filter.setAccessibleName("Filtrar categorias de preferências")
        self.filter.textChanged.connect(self._filter)
        left.addWidget(self.filter)
        self.cats = QListWidget()
        self.cats.setObjectName("CategoryList")
        self.cats.setAccessibleName("Categorias de preferências")
        self.cats.setMinimumWidth(220)
        self.cats.setMaximumWidth(280)
        left.addWidget(self.cats, 1)
        lw = QWidget()
        lw.setLayout(left)
        body.addWidget(lw)

        right = QVBoxLayout()
        right.setContentsMargins(16, 16, 16, 8)
        right.setSpacing(10)
        self.pages = QStackedWidget()
        self.help = HelpPanel()
        self.help.setMaximumHeight(220)
        for g in groups:
            it = QListWidgetItem(icon(GROUP_ICONS.get(g.key, "settings")), g.title)
            it.setData(Qt.ItemDataRole.UserRole, g.key)
            it.setToolTip(g.description)
            self.cats.addItem(it)
            form = ParamForm(g, settings.group_values(g), show_presets=False, help_panel=self.help)
            self.forms[g.key] = form
            sc = QScrollArea()
            sc.setWidgetResizable(True)
            sc.setFrameShape(QFrame.Shape.NoFrame)
            host = QWidget()
            hl = QVBoxLayout(host)
            hl.setContentsMargins(0, 0, 8, 0)
            hl.addWidget(form)
            hl.addStretch(1)
            sc.setWidget(host)
            self.pages.addWidget(sc)
        right.addWidget(self.pages, 1)
        right.addWidget(self.help)
        rw = QWidget()
        rw.setLayout(right)
        body.addWidget(rw, 1)
        root.addLayout(body, 1)

        sep = QFrame()
        sep.setFrameShape(QFrame.Shape.HLine)
        root.addWidget(sep)
        foot = QHBoxLayout()
        foot.setContentsMargins(16, 10, 16, 12)
        self.reset_btn = QPushButton("Restaurar padrões desta página")
        self.reset_btn.setIcon(icon("restore"))
        self.reset_btn.clicked.connect(self._reset_page)
        foot.addWidget(self.reset_btn)
        foot.addStretch(1)
        bb = QDialogButtonBox(QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel
                              | QDialogButtonBox.StandardButton.Apply)
        bb.button(QDialogButtonBox.StandardButton.Ok).setText("OK")
        bb.button(QDialogButtonBox.StandardButton.Cancel).setText("Cancelar")
        bb.button(QDialogButtonBox.StandardButton.Apply).setText("Aplicar")
        bb.button(QDialogButtonBox.StandardButton.Ok).setProperty("primary", True)
        bb.accepted.connect(self._ok)
        bb.rejected.connect(self.reject)
        bb.button(QDialogButtonBox.StandardButton.Apply).clicked.connect(self.apply)
        foot.addWidget(bb)
        root.addLayout(foot)

        self.cats.currentRowChanged.connect(self._page)
        start = 0
        if start_key:
            start = next((i for i, g in enumerate(groups) if g.key == start_key), 0)
        self.cats.setCurrentRow(start)

    def _page(self, row: int) -> None:
        if row < 0:
            return
        self.pages.setCurrentIndex(row)
        g = self.groups[row]
        self.help.set_default(g.title, g.description)

    def _filter(self, text: str) -> None:
        t = text.lower().strip()
        for i, g in enumerate(self.groups):
            hay = (g.title + " " + g.description + " " + " ".join(p.label for p in g.params)).lower()
            self.cats.item(i).setHidden(bool(t) and t not in hay)

    def _reset_page(self) -> None:
        row = self.cats.currentRow()
        if row >= 0:
            self.forms[self.groups[row].key].reset_all()

    def apply(self) -> None:
        for g in self.groups:
            self.settings.set_group_values(g, self.forms[g.key].values())
        self.settings.save()
        self.applied.emit()

    def _ok(self) -> None:
        self.apply()
        self.accept()


__all__ = ["PreferencesDialog", "UI_GROUP"]
