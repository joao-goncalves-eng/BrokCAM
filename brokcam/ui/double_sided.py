"""Diálogo "Dupla face": vira o lado de baixo da placa de uma só vez.

Para fresar o lado de baixo, a placa é virada na mesa. Por isso o cobre inferior, a
furação e o contorno precisam ser espelhados juntos, em torno do mesmo eixo, para que
os furos e o recorte coincidam com o lado de cima. Aqui o usuário marca as camadas (as
adequadas já vêm marcadas pelo papel), escolhe o eixo e, opcionalmente, cria furos de
pino de alinhamento sobre o eixo — eles ficam no mesmo lugar nas duas faces.
"""
from __future__ import annotations

import numpy as np
from PySide6.QtCore import Qt
from PySide6.QtWidgets import (
    QCheckBox, QDialog, QDialogButtonBox, QGridLayout, QHBoxLayout, QLabel, QListWidget,
    QListWidgetItem, QPushButton, QVBoxLayout, QWidget,
)

from brokcam.core.model import DrillLayer, DrillTool

from . import units
from .icons import icon
from .param_form import Combo, DoubleSpin, HelpPanel
from .project import Project
from .theme import ROLE_LABELS

# papéis que normalmente acompanham o lado de baixo ao virar a placa
FLIP_ROLES = ("bottom_copper", "drill", "outline")

HELP = {
    "layers": ("Camadas a virar",
               "Marque tudo que será fresado com a placa virada: o cobre de baixo, a furação e o contorno "
               "(o recorte normalmente é feito por último, com a placa virada). O cobre de cima NÃO deve ser "
               "virado. As camadas já vêm marcadas pelo papel definido no painel Propriedades."),
    "axis": ("Eixo de virada",
             "Como você vai virar a placa na mesa. Horizontal: vira da esquerda para a direita (como uma "
             "página de livro) — o X é espelhado. Vertical: vira de cima para baixo — o Y é espelhado. "
             "Use o mesmo movimento ao virar a placa de verdade."),
    "center": ("Posição do eixo",
               "Linha em torno da qual a placa é espelhada. O padrão é o centro da placa (pelo contorno): "
               "assim a placa virada ocupa o mesmo lugar na mesa. Com pinos de alinhamento, esta linha "
               "deve passar pelos pinos."),
    "pins": ("Furos de pino de alinhamento",
             "Cria dois furos sobre o eixo de virada, fora da placa. Fure-os junto com o lado de cima, "
             "coloque pinos (ou brocas) neles na mesa de sacrifício e use-os para encaixar a placa virada: "
             "como estão sobre o eixo, ficam no mesmo lugar nas duas faces e garantem o alinhamento."),
    "pin_dia": ("Diâmetro dos pinos", "Diâmetro dos furos de alinhamento (o do pino ou broca que você vai usar "
                "como guia; 3 mm é comum)."),
    "pin_margin": ("Distância da placa", "Quanto os furos de alinhamento ficam afastados da borda da placa."),
}


class DoubleSidedDialog(QDialog):
    """Escolhe camadas, eixo e pinos. Resultado: ``layer_ids``, ``axis``, ``center``, ``pins``."""

    def __init__(self, project: Project, parent: QWidget | None = None,
                 preselect: list[int] | None = None) -> None:
        super().__init__(parent)
        self.setWindowTitle("Dupla face — virar placa")
        self.setAccessibleName("Dupla face, virar placa")
        self.project = project
        self.layer_ids: list[int] = []
        self.axis = "x"
        self.center = 0.0
        self.pins: DrillLayer | None = None

        root = QVBoxLayout(self)
        root.setContentsMargins(16, 16, 16, 16)
        root.setSpacing(12)
        intro = QLabel("Espelha de uma vez o lado de baixo da placa — cobre inferior, furos e contorno — "
                       "em torno do mesmo eixo, para que tudo coincida depois de virar a placa na mesa. "
                       "Caminhos já gerados também são virados (o sentido de corte é preservado).")
        intro.setWordWrap(True)
        intro.setObjectName("Muted")
        root.addWidget(intro)

        body = QHBoxLayout()
        body.setSpacing(16)
        root.addLayout(body, 1)
        left = QVBoxLayout()
        body.addLayout(left, 3)
        self.help = HelpPanel()

        lab = QLabel("&Camadas a virar")
        left.addWidget(lab)
        self.list = QListWidget()
        self.list.setAccessibleName("Camadas a virar")
        self.list.setMinimumHeight(150)
        pre = set(preselect or [])
        for e in project.layers:
            it = QListWidgetItem(f"{e.name}   ·   {ROLE_LABELS.get(e.role, e.role)}"
                                 + ("   ·   já espelhada" if e.transform.matrix()[0][0, 0] * e.transform.matrix()[0][1, 1] < 0 else ""))
            it.setData(Qt.ItemDataRole.UserRole, e.id)
            it.setFlags(it.flags() | Qt.ItemFlag.ItemIsUserCheckable)
            checked = (e.id in pre) if pre else (e.role in FLIP_ROLES)
            it.setCheckState(Qt.CheckState.Checked if checked else Qt.CheckState.Unchecked)
            it.setIcon(icon({"gerber": "copper", "excellon": "drill"}.get(e.kind, "outline")))
            self.list.addItem(it)
        lab.setBuddy(self.list)
        self._bind(self.list, "layers")
        left.addWidget(self.list, 1)
        quick = QHBoxLayout()
        for text, fn in (("Lado de baixo", self._select_bottom), ("Todas", lambda: self._check_all(True)),
                         ("Nenhuma", lambda: self._check_all(False))):
            b = QPushButton(text)
            b.setProperty("flat", True)
            b.clicked.connect(fn)
            quick.addWidget(b)
        quick.addStretch(1)
        left.addLayout(quick)

        grid = QGridLayout()
        grid.setHorizontalSpacing(10)
        grid.setVerticalSpacing(8)
        r = 0
        lab = QLabel("&Eixo de virada")
        self.axis_combo = Combo()
        self.axis_combo.addItem(icon("mirror"), "Horizontal (espelha X)", "x")
        self.axis_combo.addItem(icon("mirror"), "Vertical (espelha Y)", "y")
        self.axis_combo.setAccessibleName("Eixo de virada")
        self._bind(self.axis_combo, "axis")
        lab.setBuddy(self.axis_combo)
        grid.addWidget(lab, r, 0)
        grid.addWidget(self.axis_combo, r, 1)
        r += 1
        lab = QLabel("&Posição do eixo")
        crow = QHBoxLayout()
        self.center_mode = Combo()
        self.center_mode.addItem("Centro da placa", "board")
        self.center_mode.addItem("Valor informado", "value")
        self.center_mode.setAccessibleName("Referência do eixo")
        self.center_val = DoubleSpin()
        self.center_val.setRange(-10000, 10000)
        self.center_val.setDecimals(3)
        self.center_val.setSuffix(f" {units.unit_label()}")
        self.center_val.setAccessibleName("Posição do eixo")
        for w in (self.center_mode, self.center_val):
            self._bind(w, "center")
        crow.addWidget(self.center_mode, 1)
        crow.addWidget(self.center_val, 1)
        lab.setBuddy(self.center_mode)
        grid.addWidget(lab, r, 0)
        grid.addLayout(crow, r, 1)
        r += 1
        self.pins_cb = QCheckBox("Criar furos de pino de alinhamento sobre o eixo")
        self._bind(self.pins_cb, "pins")
        grid.addWidget(self.pins_cb, r, 0, 1, 2)
        r += 1
        prow = QHBoxLayout()
        self.pin_dia = DoubleSpin()
        self.pin_dia.setRange(0.5, 10)
        self.pin_dia.setDecimals(2)
        self.pin_dia.setValue(units.to_display(3.0))
        self.pin_dia.setPrefix("Ø ")
        self.pin_dia.setSuffix(f" {units.unit_label()}")
        self.pin_dia.setAccessibleName("Diâmetro dos pinos")
        self._bind(self.pin_dia, "pin_dia")
        self.pin_margin = DoubleSpin()
        self.pin_margin.setRange(1, 100)
        self.pin_margin.setDecimals(1)
        self.pin_margin.setValue(units.to_display(5.0))
        self.pin_margin.setPrefix("afastados ")
        self.pin_margin.setSuffix(f" {units.unit_label()}")
        self.pin_margin.setAccessibleName("Distância dos pinos até a placa")
        self._bind(self.pin_margin, "pin_margin")
        prow.addWidget(self.pin_dia)
        prow.addWidget(self.pin_margin)
        grid.addLayout(prow, r, 1)
        r += 1
        left.addLayout(grid)
        self.info = QLabel()
        self.info.setWordWrap(True)
        self.info.setTextFormat(Qt.TextFormat.RichText)
        left.addWidget(self.info)

        right = QVBoxLayout()
        right.addWidget(self.help, 1)
        body.addLayout(right, 2)

        bb = QDialogButtonBox(QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel)
        self.ok_btn = bb.button(QDialogButtonBox.StandardButton.Ok)
        self.ok_btn.setText("&Virar")
        self.ok_btn.setProperty("primary", True)
        bb.accepted.connect(self._accept)
        bb.rejected.connect(self.reject)
        root.addWidget(bb)

        self.list.itemChanged.connect(lambda _i: self._update())
        self.axis_combo.currentIndexChanged.connect(lambda _i: self._update(reset_center=True))
        self.center_mode.currentIndexChanged.connect(lambda _i: self._update(reset_center=True))
        self.center_val.valueChanged.connect(lambda _v: self._update())
        self.pins_cb.toggled.connect(lambda _on: self._update())
        self.help.set_default("Dupla face", HELP["layers"][1])
        self.resize(820, 520)
        self._update(reset_center=True)

    # ------------------------------------------------------------------ ajuda
    def _bind(self, w: QWidget, key: str) -> None:
        w.setProperty("help_key", key)
        w.installEventFilter(self)
        w.setAccessibleDescription(HELP[key][1])
        w.setToolTip(HELP[key][1].split(". ")[0] + ".")

    def eventFilter(self, obj, ev) -> bool:  # type: ignore[no-untyped-def]  # noqa: N802
        from PySide6.QtCore import QEvent
        if ev.type() in (QEvent.Type.Enter, QEvent.Type.FocusIn):
            key = obj.property("help_key")
            if key in HELP:
                self.help.set_default(*HELP[key])
                self.help.show_default()
        return super().eventFilter(obj, ev)

    # ------------------------------------------------------------------ seleção
    def _check_all(self, on: bool) -> None:
        for i in range(self.list.count()):
            self.list.item(i).setCheckState(Qt.CheckState.Checked if on else Qt.CheckState.Unchecked)

    def _select_bottom(self) -> None:
        for i in range(self.list.count()):
            it = self.list.item(i)
            e = self.project.layer(it.data(Qt.ItemDataRole.UserRole))
            it.setCheckState(Qt.CheckState.Checked if e is not None and e.role in FLIP_ROLES
                             else Qt.CheckState.Unchecked)

    def checked_ids(self) -> list[int]:
        return [self.list.item(i).data(Qt.ItemDataRole.UserRole) for i in range(self.list.count())
                if self.list.item(i).checkState() == Qt.CheckState.Checked]

    # ------------------------------------------------------------------ cálculo
    def _board(self) -> tuple[float, float, float, float] | None:
        return self.project.board_bounds("outline")

    def _update(self, reset_center: bool = False) -> None:
        axis = self.axis_combo.currentData()
        b = self._board()
        board_mode = self.center_mode.currentData() == "board"
        self.center_val.setEnabled(not board_mode)
        if b is not None and (board_mode or reset_center):
            c = (b[0] + b[2]) / 2 if axis == "x" else (b[1] + b[3]) / 2
            self.center_val.blockSignals(True)
            self.center_val.setValue(units.to_display(c))
            self.center_val.blockSignals(False)
        for w in (self.pin_dia, self.pin_margin):
            w.setEnabled(self.pins_cb.isChecked())
        ids = self.checked_ids()
        roles = {self.project.layer(i).role for i in ids if self.project.layer(i)}
        n_jobs = sum(1 for j in self.project.jobs if j.source_id in set(ids))
        lines = [f"<b>{len(ids)}</b> camada(s) e <b>{n_jobs}</b> caminho(s) serão virados em torno de "
                 f"<b>{'X' if axis == 'x' else 'Y'} = {units.fmt_len(units.from_display(self.center_val.value()), 3)}</b>."]
        if "top_copper" in roles:
            lines.append("⚠ O cobre de cima está marcado — normalmente ele NÃO é virado.")
        if ids and "drill" not in roles and any(e.role == "drill" for e in self.project.layers):
            lines.append("⚠ A furação não está marcada: os furos não vão coincidir com o lado de baixo.")
        self.info.setText("<br>".join(lines))
        self.ok_btn.setEnabled(bool(ids) or self.pins_cb.isChecked())

    def make_pins(self) -> DrillLayer | None:
        b = self._board()
        if b is None:
            return None
        d = units.from_display(self.pin_dia.value())
        m = units.from_display(self.pin_margin.value()) + d / 2
        c = units.from_display(self.center_val.value())
        if self.axis_combo.currentData() == "x":       # eixo vertical x = c: pinos acima e abaixo
            pts = np.array([[c, b[1] - m], [c, b[3] + m]])
        else:                                          # eixo horizontal y = c: pinos à esquerda e à direita
            pts = np.array([[b[0] - m, c], [b[2] + m, c]])
        tool = DrillTool(number=1, diameter=d, points=pts, slots=np.zeros((0, 4)), plated=False)
        return DrillLayer(name="Furos de alinhamento (dupla face)", source_path="", tools={1: tool})

    def _accept(self) -> None:
        self.layer_ids = self.checked_ids()
        self.axis = self.axis_combo.currentData()
        self.center = units.from_display(self.center_val.value())
        self.pins = self.make_pins() if self.pins_cb.isChecked() else None
        self.accept()
