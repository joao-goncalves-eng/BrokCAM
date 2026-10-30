"""Biblioteca de ferramentas: gerenciador (diálogo) e seletor usado nas operações.

* :func:`library` — instância única carregada de ``%APPDATA%/BrokCAM/tools.json``.
* :class:`ToolLibraryDialog` — lista à esquerda (busca e filtro por formato), formulário
  explicado à direita (gerado de ``TOOL_ENTRY``), adicionar/duplicar/excluir/ordenar,
  importar/exportar e restaurar padrões. Cada alteração é salva imediatamente.
* :class:`ToolPicker` — combo "Ferramenta" no painel da operação; escolher uma
  ferramenta preenche geometria e dados de corte do formulário.
"""
from __future__ import annotations

import logging
from typing import Any

from PySide6.QtCore import QSize, Qt, Signal
from PySide6.QtWidgets import (
    QDialog, QDialogButtonBox, QFileDialog, QHBoxLayout, QLabel, QLineEdit, QListWidget,
    QListWidgetItem, QMenu, QMessageBox, QPushButton, QScrollArea, QSplitter, QToolButton,
    QVBoxLayout, QWidget,
)

from brokcam.cam.library import SHAPES, TOOL_ENTRY, LibraryTool, ToolLibrary

from .icons import icon
from .param_form import Combo, HelpPanel, ParamForm
from .settings import config_dir

log = logging.getLogger(__name__)

SHAPE_LABELS = dict(SHAPES)
SHAPE_ICONS = {"vbit": "isolation", "flat": "ncc", "ball": "ncc", "drill": "drill", "laser": "spindle"}

_LIBRARY: ToolLibrary | None = None
_listeners: list[Any] = []


def library_path():  # type: ignore[no-untyped-def]
    return config_dir() / "tools.json"


def library() -> ToolLibrary:
    """Biblioteca do usuário (carregada na primeira chamada)."""
    global _LIBRARY
    if _LIBRARY is None:
        try:
            _LIBRARY = ToolLibrary.load(library_path())
        except Exception as exc:  # noqa: BLE001
            log.error("Biblioteca de ferramentas corrompida (%s); usando a padrão.", exc)
            _LIBRARY = ToolLibrary(path=library_path())
    return _LIBRARY


def set_library(lib: ToolLibrary) -> None:
    """Substitui a biblioteca (usado em testes)."""
    global _LIBRARY
    _LIBRARY = lib
    _notify()


def save_library() -> None:
    lib = library()
    try:
        lib.save(lib.path or library_path())
    except OSError as exc:
        log.error("Não foi possível salvar a biblioteca de ferramentas: %s", exc)
    _notify()


def on_library_changed(callback: Any) -> None:
    _listeners.append(callback)


def _notify() -> None:
    for cb in list(_listeners):
        try:
            cb()
        except RuntimeError:          # widget já destruído
            _listeners.remove(cb)


# =========================================================================== seletor

class ToolPicker(QWidget):
    """Combo de ferramentas da biblioteca compatíveis com uma operação."""

    toolChosen = Signal(object)          # LibraryTool
    manageRequested = Signal()

    def __init__(self, op_key: str, current_uid: str | None = None, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.op_key = op_key
        lay = QHBoxLayout(self)
        lay.setContentsMargins(0, 0, 0, 0)
        lay.setSpacing(6)
        lab = QLabel("&Ferramenta")
        lab.setObjectName("Muted")
        self.combo = Combo()
        self.combo.setAccessibleName("Ferramenta da biblioteca")
        self.combo.setAccessibleDescription(
            "Escolher uma ferramenta preenche diâmetro, formato e dados de corte do formulário "
            "com os valores salvos na biblioteca.")
        self.combo.setToolTip("Preenche o formulário com a geometria e os dados de corte da ferramenta "
                              "escolhida na sua biblioteca.")
        lab.setBuddy(self.combo)
        manage = QToolButton()
        manage.setIcon(icon("tool-library"))
        manage.setToolTip("Abrir a biblioteca de ferramentas (Ctrl+T)")
        manage.setAccessibleName("Gerenciar biblioteca de ferramentas")
        manage.clicked.connect(self.manageRequested.emit)
        lay.addWidget(lab)
        lay.addWidget(self.combo, 1)
        lay.addWidget(manage)
        self.reload(current_uid)
        self.combo.activated.connect(self._chosen)

    def reload(self, select_uid: str | None = None) -> None:
        if select_uid is None:
            select_uid = self.current_uid()
        c = self.combo
        c.blockSignals(True)
        c.clear()
        c.addItem("— valores do formulário —", None)
        for t in library().for_operation(self.op_key):
            c.addItem(icon(SHAPE_ICONS.get(t.shape, "job")), t.name, t.uid)
            c.setItemData(c.count() - 1, t.summary(), Qt.ItemDataRole.ToolTipRole)
        i = c.findData(select_uid) if select_uid else 0
        c.setCurrentIndex(max(0, i))
        c.blockSignals(False)

    def current_uid(self) -> str | None:
        return self.combo.currentData() if self.combo.count() else None

    def current_tool(self) -> LibraryTool | None:
        return library().get(self.current_uid())

    def set_current(self, uid: str | None) -> None:
        self.combo.setCurrentIndex(max(0, self.combo.findData(uid) if uid else 0))

    def _chosen(self, _idx: int) -> None:
        t = self.current_tool()
        if t is not None:
            self.toolChosen.emit(t)


# =========================================================================== diálogo

class ToolLibraryDialog(QDialog):
    """Gerenciador da biblioteca de ferramentas."""

    def __init__(self, parent: QWidget | None = None, select_uid: str | None = None) -> None:
        super().__init__(parent)
        self.setWindowTitle("Biblioteca de ferramentas")
        self.setAccessibleName("Biblioteca de ferramentas")
        self.resize(980, 680)
        self.lib = library()
        self._loading = False

        root = QVBoxLayout(self)
        root.setContentsMargins(12, 12, 12, 12)
        root.setSpacing(10)
        intro = QLabel("Cadastre aqui as ferramentas que você possui, com os dados de corte que funcionam "
                       "na sua máquina. Nas operações, escolha a ferramenta no campo “Ferramenta” para "
                       "preencher tudo automaticamente.")
        intro.setObjectName("Muted")
        intro.setWordWrap(True)
        root.addWidget(intro)

        split = QSplitter(Qt.Orientation.Horizontal)
        root.addWidget(split, 1)

        # ---- lista
        left = QWidget()
        ll = QVBoxLayout(left)
        ll.setContentsMargins(0, 0, 0, 0)
        ll.setSpacing(6)
        row = QHBoxLayout()
        self.search = QLineEdit()
        self.search.setPlaceholderText("Buscar ferramenta…")
        self.search.setAccessibleName("Buscar ferramenta")
        self.search.setClearButtonEnabled(True)
        self.search.textChanged.connect(lambda _t: self._fill())
        self.filter = Combo()
        self.filter.setAccessibleName("Filtrar por formato")
        self.filter.addItem("Todos os formatos", None)
        for k, lab in SHAPES:
            self.filter.addItem(icon(SHAPE_ICONS[k]), lab, k)
        self.filter.currentIndexChanged.connect(lambda _i: self._fill())
        self.filter.setMinimumContentsLength(14)
        row.addWidget(self.search, 1)
        row.addWidget(self.filter, 0)
        ll.addLayout(row)
        self.list = QListWidget()
        self.list.setAccessibleName("Ferramentas cadastradas")
        self.list.setIconSize(QSize(18, 18))
        self.list.setAlternatingRowColors(False)
        self.list.currentItemChanged.connect(lambda cur, _prev: self._show(cur))
        ll.addWidget(self.list, 1)

        btns = QHBoxLayout()
        btns.setSpacing(4)
        add = QToolButton()
        add.setIcon(icon("plus"))
        add.setText("Nova")
        add.setToolButtonStyle(Qt.ToolButtonStyle.ToolButtonTextBesideIcon)
        add.setPopupMode(QToolButton.ToolButtonPopupMode.InstantPopup)
        add.setAccessibleName("Nova ferramenta")
        m = QMenu(add)
        for k, lab in SHAPES:
            m.addAction(icon(SHAPE_ICONS[k]), lab, lambda k=k: self.add_tool(k))
        add.setMenu(m)
        self.dup_btn = self._tbtn("bookmark", "Duplicar a ferramenta selecionada", self.duplicate)
        self.del_btn = self._tbtn("trash", "Excluir a ferramenta selecionada (Delete)", self.delete)
        self.up_btn = self._tbtn("arrow-up", "Mover para cima", lambda: self.move(-1))
        self.down_btn = self._tbtn("arrow-down", "Mover para baixo", lambda: self.move(1))
        more = QToolButton()
        more.setIcon(icon("settings"))
        more.setToolTip("Importar, exportar ou restaurar a biblioteca")
        more.setAccessibleName("Mais ações da biblioteca")
        more.setPopupMode(QToolButton.ToolButtonPopupMode.InstantPopup)
        mm = QMenu(more)
        mm.addAction(icon("import"), "Importar ferramentas…", self.import_file)
        mm.addAction(icon("export"), "Exportar biblioteca…", self.export_file)
        mm.addSeparator()
        mm.addAction(icon("restore"), "Restaurar biblioteca padrão…", self.reset_defaults)
        more.setMenu(mm)
        btns.addWidget(add)
        for b in (self.dup_btn, self.del_btn, self.up_btn, self.down_btn):
            btns.addWidget(b)
        btns.addStretch(1)
        btns.addWidget(more)
        ll.addLayout(btns)
        split.addWidget(left)

        # ---- formulário
        right = QWidget()
        rl = QVBoxLayout(right)
        rl.setContentsMargins(8, 0, 0, 0)
        rl.setSpacing(8)
        self.summary = QLabel()
        self.summary.setObjectName("Heading")
        self.summary.setWordWrap(True)
        rl.addWidget(self.summary)
        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setFrameShape(QScrollArea.Shape.NoFrame)
        self.help = HelpPanel()
        self.help.setMaximumHeight(180)
        self.form = ParamForm(TOOL_ENTRY, show_header=False, show_presets=False, help_panel=self.help)
        self.form.valuesChanged.connect(self._edited)
        scroll.setWidget(self.form)
        rl.addWidget(scroll, 1)
        rl.addWidget(self.help)
        split.addWidget(right)
        split.setStretchFactor(0, 2)
        split.setStretchFactor(1, 3)
        split.setSizes([360, 600])

        bb = QDialogButtonBox(QDialogButtonBox.StandardButton.Close)
        bb.rejected.connect(self.reject)
        bb.accepted.connect(self.accept)
        root.addWidget(bb)

        self._fill(select_uid)

    # ------------------------------------------------------------------ util
    def _tbtn(self, name: str, tip: str, slot: Any) -> QToolButton:
        b = QToolButton()
        b.setIcon(icon(name))
        b.setToolTip(tip)
        b.setAccessibleName(tip)
        b.clicked.connect(slot)
        return b

    def _fill(self, select_uid: str | None = None) -> None:
        if select_uid is None and self.current() is not None:
            select_uid = self.current().uid  # type: ignore[union-attr]
        q = self.search.text().strip().lower()
        shape = self.filter.currentData()
        self.list.blockSignals(True)
        self.list.clear()
        sel_item = None
        for t in self.lib:
            if shape and t.shape != shape:
                continue
            if q and q not in t.name.lower() and q not in t.notes.lower():
                continue
            it = QListWidgetItem(icon(SHAPE_ICONS.get(t.shape, "job")), f"{t.name}\n{t.summary()}")
            it.setData(Qt.ItemDataRole.UserRole, t.uid)
            it.setToolTip(t.notes or t.summary())
            it.setData(Qt.ItemDataRole.AccessibleTextRole, f"{t.name}, {SHAPE_LABELS.get(t.shape, t.shape)}, {t.summary()}")
            self.list.addItem(it)
            if t.uid == select_uid:
                sel_item = it
        self.list.blockSignals(False)
        if sel_item is None and self.list.count():
            sel_item = self.list.item(0)
        self.list.setCurrentItem(sel_item)
        self._show(sel_item)

    def current(self) -> LibraryTool | None:
        it = self.list.currentItem()
        return self.lib.get(it.data(Qt.ItemDataRole.UserRole)) if it is not None else None

    def _show(self, item: QListWidgetItem | None) -> None:
        t = self.lib.get(item.data(Qt.ItemDataRole.UserRole)) if item is not None else None
        has = t is not None
        for w in (self.form, self.dup_btn, self.del_btn, self.up_btn, self.down_btn):
            w.setEnabled(has)
        if t is None:
            self.summary.setText("Nenhuma ferramenta selecionada")
            return
        self._loading = True
        self.form.set_values(t.form_values())
        self._loading = False
        self._update_summary(t)

    def _update_summary(self, t: LibraryTool) -> None:
        self.summary.setText(f"{t.name}  ·  {SHAPE_LABELS.get(t.shape, t.shape)}  ·  largura de corte "
                             f"{t.effective_diameter:.3f} mm")

    # ------------------------------------------------------------------ ações
    def _edited(self, values: dict[str, Any]) -> None:
        if self._loading:
            return
        cur = self.current()
        if cur is None:
            return
        t = LibraryTool.from_form_values(values, uid=cur.uid)
        self.lib.update(t)
        save_library()
        it = self.list.currentItem()
        if it is not None:
            it.setText(f"{t.name}\n{t.summary()}")
            it.setIcon(icon(SHAPE_ICONS.get(t.shape, "job")))
        self._update_summary(t)

    def add_tool(self, shape: str) -> None:
        base = {"vbit": dict(diameter=0.1, cut_z=-0.08, feed_xy=120),
                "flat": dict(diameter=1.0, cut_z=-0.1, feed_xy=150),
                "ball": dict(diameter=1.0, cut_z=-0.1, feed_xy=150),
                "drill": dict(diameter=0.8, cut_z=-1.8, travel_z=2.0, feed_z=50),
                "laser": dict(diameter=0.1, cut_z=0.0, feed_xy=600, spindle_rpm=0)}[shape]
        t = LibraryTool(name=f"Nova {SHAPE_LABELS[shape].lower()}", shape=shape, **base)  # type: ignore[arg-type]
        cur = self.current()
        self.lib.add(t, self.lib.tools.index(cur) + 1 if cur is not None else None)
        save_library()
        self.search.clear()
        self.filter.setCurrentIndex(0)
        self._fill(t.uid)
        self.form.editor("name").setFocus()

    def duplicate(self) -> None:
        cur = self.current()
        if cur is None:
            return
        d = self.lib.duplicate(cur.uid)
        save_library()
        self._fill(d.uid if d else None)

    def delete(self) -> None:
        cur = self.current()
        if cur is None:
            return
        if QMessageBox.question(self, "Excluir ferramenta", f"Excluir “{cur.name}” da biblioteca?") \
                != QMessageBox.StandardButton.Yes:
            return
        idx = self.list.currentRow()
        self.lib.remove(cur.uid)
        save_library()
        self._fill("")
        if self.list.count():
            self.list.setCurrentRow(min(idx, self.list.count() - 1))

    def move(self, delta: int) -> None:
        cur = self.current()
        if cur is None:
            return
        self.lib.move(cur.uid, delta)
        save_library()
        self._fill(cur.uid)

    def import_file(self) -> None:
        path, _ = QFileDialog.getOpenFileName(self, "Importar ferramentas", "", "Biblioteca BrokCAM (*.json)")
        if not path:
            return
        try:
            n = self.lib.import_file(path)
        except Exception as exc:  # noqa: BLE001
            QMessageBox.warning(self, "Importar", f"Não foi possível importar:\n{exc}")
            return
        save_library()
        self._fill()
        QMessageBox.information(self, "Importar", f"{n} ferramenta(s) importada(s).")

    def export_file(self) -> None:
        path, _ = QFileDialog.getSaveFileName(self, "Exportar biblioteca", "ferramentas.json",
                                              "Biblioteca BrokCAM (*.json)")
        if path:
            try:
                self.lib.export_file(path)
            except OSError as exc:
                QMessageBox.warning(self, "Exportar", f"Não foi possível salvar:\n{exc}")

    def reset_defaults(self) -> None:
        if QMessageBox.question(self, "Restaurar padrão",
                                "Substituir toda a biblioteca pelas ferramentas padrão? Suas ferramentas "
                                "cadastradas serão perdidas (exporte antes se quiser guardá-las).") \
                != QMessageBox.StandardButton.Yes:
            return
        self.lib.reset_defaults()
        save_library()
        self._fill("")

    def keyPressEvent(self, ev) -> None:  # type: ignore[no-untyped-def]  # noqa: N802
        if ev.key() == Qt.Key.Key_Delete and self.list.hasFocus():
            self.delete()
            return
        super().keyPressEvent(ev)
