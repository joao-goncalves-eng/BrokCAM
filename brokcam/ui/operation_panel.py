"""Dock "Propriedades": detalhes do item selecionado e operações de CAM compatíveis.

* Camada: papel, cor, dimensões, avisos do parser, transformações (espelhar/deslocar)
  e a lista de operações compatíveis com formulário gerado + "Gerar caminho".
* Job: estatísticas, pós-processador GRBL, "Gerar G-code", simular, salvar e enviar.
"""
from __future__ import annotations

import html
from typing import Any

import numpy as np
from PySide6.QtCore import Qt, Signal
from PySide6.QtGui import QColor, QIcon, QPainter, QPen, QPixmap
from PySide6.QtWidgets import (
    QAbstractItemView, QColorDialog, QFrame, QGridLayout, QHBoxLayout, QHeaderView, QLabel,
    QProgressBar, QPushButton, QScrollArea, QSizePolicy, QTableWidget, QTableWidgetItem, QToolButton,
    QVBoxLayout, QWidget,
)

from brokcam.core.model import DrillLayer, GerberLayer

from . import backend, theme, units
from .icons import icon, pixmap
from .param_form import Combo, DoubleSpin, HelpPanel, ParamForm
from .ribbon import CollapsibleSection
from .project import JobEntry, LayerEntry, Transform
from .settings import AppSettings
from .tool_library import ToolPicker, library, on_library_changed
from .theme import ROLE_LABELS


SHORT_TITLES = {"isolation": "Isolamento", "ncc": "Limpeza (NCC)", "cutout": "Recorte",
                "drilling": "Furação", "panelize": "Painelizar"}


OP_ICONS = {"isolation": "isolation", "ncc": "ncc", "cutout": "cutout", "drilling": "drill",
            "panelize": "panelize"}


def _stat(grid: QGridLayout, idx: int, label: str, value: str, cols: int = 2) -> None:
    """Cartão compacto valor + rótulo (estatísticas do caminho)."""
    f = QFrame()
    f.setObjectName("StatChip")
    v = QVBoxLayout(f)
    v.setContentsMargins(10, 6, 10, 6)
    v.setSpacing(0)
    val = QLabel(value)
    val.setObjectName("StatValue")
    val.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
    lab = QLabel(label)
    lab.setObjectName("StatLabel")
    v.addWidget(val)
    v.addWidget(lab)
    f.setAccessibleName(f"{label}: {value}")
    grid.addWidget(f, idx // cols, idx % cols)


def _section(title: str) -> QLabel:
    lab = QLabel(title)
    lab.setObjectName("SectionTitle")
    return lab


def _card() -> tuple[QFrame, QGridLayout]:
    f = QFrame()
    f.setObjectName("Card")
    g = QGridLayout(f)
    g.setContentsMargins(12, 10, 12, 10)
    g.setHorizontalSpacing(12)
    g.setVerticalSpacing(4)
    return f, g


def _kv(grid: QGridLayout, row: int, key: str, value: str) -> QLabel:
    k = QLabel(key)
    k.setObjectName("Muted")
    v = QLabel(value)
    v.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
    v.setWordWrap(True)
    grid.addWidget(k, row, 0, Qt.AlignmentFlag.AlignTop)
    grid.addWidget(v, row, 1)
    return v


def _fmt_dia(d: float) -> str:
    """Diâmetro na unidade de exibição, vírgula decimal, sem zeros inúteis (0,8 / 1,25)."""
    if units.display_units() == "in":
        return units.fmt_len(d, 4, False)
    return f"{float(d):.3f}".rstrip("0").rstrip(".").replace(".", ",")


def layer_drill_rows(layer: DrillLayer) -> list[dict[str, Any]]:
    """Linhas da tabela de diâmetros de uma camada Excellon (ordem crescente = cor)."""
    by: dict[float, dict[str, Any]] = {}
    for t in layer.tools.values():
        d = round(float(t.diameter), 4)
        r = by.setdefault(d, {"dia": d, "sources": [], "holes": 0, "slots": 0, "milled": False})
        r["sources"].append(int(t.number))
        r["holes"] += len(np.asarray(t.points).reshape(-1, 2))
        r["slots"] += len(np.asarray(t.slots).reshape(-1, 4))
    rows = [r for _d, r in sorted(by.items()) if r["holes"] or r["slots"]]
    for i, r in enumerate(sorted(by)):
        by[r]["rank"] = i
    return rows


def job_drill_rows(job: Any) -> list[dict[str, Any]]:
    """Linhas da tabela de brocas de um job de furação (``settings['bits']`` ou, em jobs
    antigos/editados, a partir das operações)."""
    bits = (job.settings or {}).get("bits") if job.settings else None
    if bits and not job.settings.get("edited"):
        return [{"dia": float(b["bit"]), "sources": list(b.get("sources", [])), "holes": int(b.get("holes", 0)),
                 "slots": int(b.get("slots", 0)), "milled": bool(b.get("milled")), "rank": int(b.get("rank", i))}
                for i, b in enumerate(bits)]
    by: dict[float, dict[str, Any]] = {}
    for op in job.ops:
        if op.kind != "drill" or not len(op.points):
            continue
        d = round(float(op.tool.diameter), 4)
        r = by.setdefault(d, {"dia": d, "sources": [], "holes": 0, "slots": 0, "milled": False})
        r["holes"] += len(op.points)
    rows = [r for _d, r in sorted(by.items())]
    for i, r in enumerate(rows):
        r["rank"] = i
    return rows


class DrillTable(QTableWidget):
    """Tabela "cor · Ø · origem · furos" com amostra colorida igual ao canvas.

    A cor nunca é a única pista: o diâmetro e a quantidade estão sempre em texto.
    """

    rankSelected = Signal(object)          # rank (int) ou None

    def __init__(self, rows: list[dict[str, Any]], dia_header: str = "Ø furo",
                 parent: QWidget | None = None) -> None:
        super().__init__(len(rows), 5, parent)
        self.rows = rows
        self.setHorizontalHeaderLabels(["", f"{dia_header} ({units.unit_label()})", "Arquivo", "Furos",
                                        f"Broca ({units.unit_label()})"])
        self.setColumnHidden(4, True)          # só aparece quando a associação troca o diâmetro
        self.verticalHeader().hide()
        self.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        self.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
        self.setSelectionMode(QAbstractItemView.SelectionMode.SingleSelection)
        self.setShowGrid(False)
        self.setAlternatingRowColors(True)
        self.setWordWrap(False)
        self.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        self.setAccessibleName("Diâmetros de furo")
        self.setAccessibleDescription("Uma linha por diâmetro, com a mesma cor usada no canvas. Selecione uma "
                                      "linha para destacar esses furos no canvas.")
        pal = theme.palette()
        for i, r in enumerate(rows):
            col = QColor(theme.drill_color(r["rank"], pal))
            pm = QPixmap(14, 14)
            pm.fill(Qt.GlobalColor.transparent)
            p = QPainter(pm)
            p.setRenderHint(QPainter.RenderHint.Antialiasing)
            p.setPen(QPen(QColor(pal.text), 1))
            p.setBrush(col)
            p.drawEllipse(1, 1, 12, 12)
            p.end()
            sw = QTableWidgetItem()
            sw.setIcon(QIcon(pm))
            sw.setData(Qt.ItemDataRole.AccessibleTextRole, f"cor {i + 1}")
            dia = QTableWidgetItem(("fresa " if r.get("milled") else "") + _fmt_dia(r["dia"]))
            src = QTableWidgetItem(" ".join(f"T{n}" for n in r["sources"]) or "—")
            cnt = str(r["holes"])
            if r.get("slots"):
                cnt += f" + {r['slots']} slot{'s' if r['slots'] != 1 else ''}"
            n = QTableWidgetItem(cnt)
            for it in (dia, n):
                it.setTextAlignment(Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter)
            tip = (f"Ø {_fmt_dia(r['dia'])} {units.unit_label()} · {cnt} furo(s) · origem "
                   f"{src.text()}" + (" · fresados com fresa de topo" if r.get("milled") else ""))
            for c, it in enumerate((sw, dia, src, n)):
                it.setToolTip(tip)
                self.setItem(i, c, it)
            bit = QTableWidgetItem("")
            bit.setTextAlignment(Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter)
            self.setItem(i, 4, bit)
        hh = self.horizontalHeader()
        hh.setSectionResizeMode(0, QHeaderView.ResizeMode.Fixed)
        self.setColumnWidth(0, 28)
        for c in (1, 3, 4):
            hh.setSectionResizeMode(c, QHeaderView.ResizeMode.ResizeToContents)
        hh.setSectionResizeMode(2, QHeaderView.ResizeMode.Stretch)
        self.resizeRowsToContents()
        h = hh.height() + sum(self.rowHeight(i) for i in range(len(rows))) + 2 * self.frameWidth() + 2
        self.setFixedHeight(min(h, 300))
        self.itemSelectionChanged.connect(self._sel)

    def set_bits(self, bits: list[tuple[float, bool] | None] | None) -> None:
        """Mostra a coluna "Broca" (Ø que a associação de diâmetros vai usar em cada linha).

        ``bits`` = um ``(Ø da broca, fresado?)`` por linha, ou ``None`` para esconder a coluna.
        Ela só aparece quando alguma linha NÃO será furada com o próprio diâmetro — assim o
        usuário vê, antes de gerar, que "Ø0,4" vai sair com a broca de 0,8 mm.
        """
        changed = False
        for i, r in enumerate(self.rows):
            b = bits[i] if bits is not None and i < len(bits) else None
            r["bit"] = b
            it = self.item(i, 4)
            if b is None:
                it.setText("")
                continue
            dia, milled = b
            same = not milled and abs(float(dia) - float(r["dia"])) < 1e-6
            changed |= not same
            it.setText(("fresa " if milled else "") + _fmt_dia(dia) + ("" if same else " ⚠"))
            tip = (f"Com a associação de diâmetros atual, os furos de Ø {_fmt_dia(r['dia'])} "
                   + (f"serão fresados com a fresa de {_fmt_dia(dia)}" if milled
                      else f"serão feitos com a broca de {_fmt_dia(dia)}") + f" {units.unit_label()}.")
            it.setToolTip(tip)
            it.setData(Qt.ItemDataRole.AccessibleTextRole,
                       ("fresa " if milled else "broca ") + _fmt_dia(dia) + ("" if same else ", diferente do furo"))
        self.setColumnHidden(4, not changed)

    def bit_of(self, row: dict[str, Any]) -> tuple[float, bool] | None:
        b = row.get("bit")
        return tuple(b) if b else None                   # type: ignore[return-value]

    def current_row(self) -> dict[str, Any] | None:
        r = self.currentRow()
        return self.rows[r] if 0 <= r < len(self.rows) and self.selectionModel().hasSelection() else None

    def _sel(self) -> None:
        r = self.current_row()
        self.rankSelected.emit(r["rank"] if r else None)


class OperationPanel(QWidget):
    generateRequested = Signal(int, str, dict)       # layer_id, op_key, settings
    cancelRequested = Signal()
    gcodeRequested = Signal(int, dict)               # job_id, post settings
    simulateRequested = Signal(int)
    saveGcodeRequested = Signal(int)
    sendRequested = Signal(int)
    roleChanged = Signal(int, str)
    colorChanged = Signal(int, str)
    transformRequested = Signal(int, object)         # layer_id, Transform
    wizardRequested = Signal()
    openRequested = Signal()
    libraryRequested = Signal(str)                   # uid da ferramenta a destacar ("" = nenhuma)
    editPathsRequested = Signal(int)                 # job_id
    alignRequested = Signal()
    batchRequested = Signal(str, list)               # ação em lote, [(tipo, id)]
    gcodeAction = Signal(str, str, int)              # ação ("preview","save","send","remove"), tipo, id
    generateOnceRequested = Signal(int, str, dict)   # como generateRequested, sem salvar como padrão
    drillHighlight = Signal(int, object)             # chave do item no canvas, rank do diâmetro (ou None)

    def __init__(self, settings: AppSettings, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setObjectName("Panel")
        self.settings = settings
        self.entry: LayerEntry | JobEntry | None = None
        self.form: ParamForm | None = None
        self.post_form: ParamForm | None = None
        self.op_key = ""
        self._ops: list[backend.OperationInfo] = []
        self.busy = False
        self.picker: ToolPicker | None = None
        self.op_combo: Combo | None = None
        on_library_changed(self._library_changed)

        root = QVBoxLayout(self)
        root.setContentsMargins(0, 0, 0, 0)
        root.setSpacing(0)
        self.scroll = QScrollArea()
        self.scroll.setWidgetResizable(True)
        self.scroll.setFrameShape(QFrame.Shape.NoFrame)
        self.scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        root.addWidget(self.scroll, 1)

        footer = QWidget()
        footer.setObjectName("Panel")
        fl = QVBoxLayout(footer)
        fl.setContentsMargins(12, 8, 12, 12)
        fl.setSpacing(8)
        self.help = HelpPanel()
        self.help.setMaximumHeight(150)
        # recolhida por padrão: no inspetor o espaço vertical é disputado (tabela de furos,
        # formulário); o estado escolhido pelo usuário continua sendo lembrado
        self.help_section = CollapsibleSection(
            "Ajuda", self.help, False, key="section_help",
            state_get=lambda k, d: bool(self.settings.get(k, d)),
            state_set=lambda k, v: self.settings.set(k, v))
        fl.addWidget(self.help_section)
        self.progress_row = QWidget()
        pr = QHBoxLayout(self.progress_row)
        pr.setContentsMargins(0, 0, 0, 0)
        pr.setSpacing(6)
        self.progress = QProgressBar()
        self.progress.setRange(0, 1000)
        self.progress.setTextVisible(False)
        self.progress.setAccessibleName("Progresso da operação")
        self.progress_msg = QLabel()
        self.progress_msg.setObjectName("Muted")
        self.cancel_btn = QToolButton()
        self.cancel_btn.setIcon(icon("cancel"))
        self.cancel_btn.setToolTip("Cancelar a operação em andamento (Esc)")
        self.cancel_btn.setAccessibleName("Cancelar operação")
        self.cancel_btn.clicked.connect(self.cancelRequested.emit)
        col = QVBoxLayout()
        col.setSpacing(2)
        col.addWidget(self.progress)
        col.addWidget(self.progress_msg)
        pr.addLayout(col, 1)
        pr.addWidget(self.cancel_btn)
        self.progress_row.hide()
        fl.addWidget(self.progress_row)
        self.primary = QPushButton()
        self.primary.setProperty("primary", True)
        self.primary.setMinimumHeight(34)
        self.primary.clicked.connect(self._primary_clicked)
        fl.addWidget(self.primary)
        root.addWidget(footer)
        theme.on_change(self, lambda _p: self.refresh())
        self.show_empty()

    # ------------------------------------------------------------------ util
    def _new_body(self) -> QVBoxLayout:
        self.drill_table = None
        self.drill_only_btn = None
        self.drill_all_btn = None
        self._refresh_drill_bits = None
        self.drillHighlight.emit(-1, None)
        body = QWidget()
        body.setObjectName("PanelBody")
        lay = QVBoxLayout(body)
        lay.setContentsMargins(12, 12, 12, 12)
        lay.setSpacing(10)
        old = self.scroll.takeWidget()
        if old is not None:
            old.deleteLater()
        self.scroll.setWidget(body)
        return lay

    def _header(self, lay: QVBoxLayout, icon_name: str, color: str, title: str, subtitle: str) -> None:
        row = QHBoxLayout()
        row.setSpacing(10)
        ic = QLabel()
        ic.setFixedSize(36, 36)
        ic.setAlignment(Qt.AlignmentFlag.AlignCenter)
        c = QColor(color)
        bg = QColor(c)
        bg.setAlphaF(0.2)
        ic.setStyleSheet(f"background: rgba({bg.red()},{bg.green()},{bg.blue()},{bg.alpha()});"
                         f" border: 1px solid {c.name()}; border-radius: 8px;")
        ic.setPixmap(pixmap(icon_name, 20, c.name(), self.devicePixelRatioF()))
        row.addWidget(ic, 0, Qt.AlignmentFlag.AlignTop)
        col = QVBoxLayout()
        col.setSpacing(0)
        t = QLabel(title)
        t.setObjectName("Heading")
        t.setWordWrap(True)
        s = QLabel(subtitle)
        s.setObjectName("Muted")
        s.setWordWrap(True)
        col.addWidget(t)
        col.addWidget(s)
        row.addLayout(col, 1)
        lay.addLayout(row)

    def refresh(self) -> None:
        if isinstance(self.entry, LayerEntry):
            self.show_layer(self.entry)
        elif isinstance(self.entry, JobEntry):
            self.show_job(self.entry)
        else:
            self.show_empty()

    # ------------------------------------------------------------------ vazio
    def show_empty(self) -> None:
        self.entry = None
        self._gcode_target = None
        self.form = None
        self.post_form = None
        lay = self._new_body()
        lay.addStretch(1)
        ic = QLabel()
        ic.setAlignment(Qt.AlignmentFlag.AlignCenter)
        ic.setPixmap(pixmap("layers", 40, theme.palette().text_muted, self.devicePixelRatioF()))
        lay.addWidget(ic)
        t = QLabel("Nada selecionado")
        t.setObjectName("Heading")
        t.setAlignment(Qt.AlignmentFlag.AlignCenter)
        lay.addWidget(t)
        d = QLabel("Selecione uma camada no painel Projeto (ou clique nela no canvas) para ver suas "
                   "propriedades e gerar caminhos de ferramenta.")
        d.setObjectName("Muted")
        d.setWordWrap(True)
        d.setAlignment(Qt.AlignmentFlag.AlignCenter)
        lay.addWidget(d)
        w = QPushButton("Abrir o assistente Gerber → GRBL")
        w.setIcon(icon("wizard"))
        w.setProperty("link", True)
        w.clicked.connect(self.wizardRequested.emit)
        lay.addWidget(w, 0, Qt.AlignmentFlag.AlignCenter)
        lay.addStretch(2)
        self.help.set_default("Propriedades", "Aqui aparecem as opções do item selecionado. Toda opção "
                              "tem explicação: passe o mouse ou dê foco a um campo para lê-la neste quadro.")
        self.primary.setText("Abrir arquivos…")
        self.primary.setIcon(icon("open"))
        self.primary.setEnabled(True)
        self.primary.setAccessibleName("Abrir arquivos")

    # ------------------------------------------------------------------ G-code
    def show_gcode(self, kind: str, id_: int, name: str, text: str, estimate_s: float,
                   origin: str, shown: bool) -> None:
        """Programa G-code selecionado na categoria G-code do painel Projeto."""
        self.entry = None
        self.form = None
        self.post_form = None
        self.op_combo = None
        lay = self._new_body()
        self._header(lay, "gcode", theme.palette().done, name, f"G-code · {origin}")
        lines = len(text.splitlines()) if text else 0
        grid = QGridLayout()
        grid.setSpacing(6)
        _stat(grid, 0, "linhas", f"{lines:,}".replace(",", "."))
        _stat(grid, 1, "tempo estimado", units.fmt_time(estimate_s) if estimate_s else "—")
        _stat(grid, 2, "tamanho", f"{len(text.encode('utf-8')) / 1024:.1f} KB")
        _stat(grid, 3, "no canvas", "exibido" if shown else "oculto")
        lay.addLayout(grid)
        acts = QGridLayout()
        acts.setSpacing(6)

        def act(i: int, text_: str, icon_name: str, tip: str, action: str) -> None:
            b = QPushButton(text_)
            b.setIcon(icon(icon_name))
            b.setToolTip(tip)
            b.setAccessibleName(text_.replace("…", ""))
            b.clicked.connect(lambda: self.gcodeAction.emit(action, kind, id_))
            acts.addWidget(b, i // 2, i % 2)

        act(0, "Ocultar do canvas" if shown else "Mostrar no canvas", "eye-off" if shown else "eye",
            "Desenhar (ou esconder) o percurso deste programa no canvas", "preview")
        act(1, "Salvar G-code…", "save", "Salvar este programa em arquivo (.nc)", "save")
        act(2, "Enviar à máquina", "send", "Carregar no painel Máquina para executar", "send")
        act(3, "Remover G-code", "trash", "Remover este programa da lista e do canvas (o caminho de "
            "ferramenta continua na categoria Geometria)", "remove")
        lay.addLayout(acts)
        note = QLabel("O G-code é o resultado final: o texto que vai para a máquina. Ele é gerado a partir de "
                      "um caminho da categoria Geometria — se o caminho mudar, gere o G-code de novo.")
        note.setObjectName("Muted")
        note.setWordWrap(True)
        lay.addWidget(note)
        lay.addStretch(1)
        self.help.set_default("G-code", "Programa de máquina gerado pelo pós-processador GRBL. Use Enviar à "
                              "máquina para executá-lo ou Salvar para usar em outro programa de envio.")
        self._set_primary("Enviar à máquina", "send", True)
        self._gcode_target = (kind, id_)

    # ------------------------------------------------------------------ seleção múltipla
    def show_multi(self, layers: list[LayerEntry], jobs: list[JobEntry],
                   group: tuple[int, str] | None = None) -> None:
        """Ações em lote quando vários itens estão selecionados no painel Projeto."""
        self.entry = None
        self._gcode_target = None
        self.form = None
        self.post_form = None
        self.op_combo = None
        lay = self._new_body()
        n = len(layers) + len(jobs)
        parts = []
        if layers:
            parts.append(f"{len(layers)} camada{'s' if len(layers) != 1 else ''}")
        if jobs:
            parts.append(f"{len(jobs)} caminho{'s' if len(jobs) != 1 else ''}")
        if group is not None:
            self._header(lay, "group", theme.palette().accent, f"Grupo “{group[1]}”",
                         " · ".join(parts) + " · movem-se juntas")
            gnote = QLabel("Camadas agrupadas ficam travadas entre si: mover, girar ou posicionar uma delas move "
                           "o grupo inteiro, então furos, cobre e contorno nunca se desalinham por acidente.")
            gnote.setObjectName("Muted")
            gnote.setWordWrap(True)
            lay.addWidget(gnote)
            grow = QHBoxLayout()
            ren = QPushButton("Renomear grupo…")
            ren.setIcon(icon("edit-path"))
            ren.clicked.connect(lambda: self.batchRequested.emit("rename_group", [("group", group[0])]))
            ung = QPushButton("Desagrupar")
            ung.setIcon(icon("ungroup"))
            ung.setToolTip("Desfazer o grupo (Ctrl+Shift+G): as camadas passam a se mover separadamente")
            ung.clicked.connect(lambda: self.batchRequested.emit("ungroup", [("layer", e.id) for e in layers]))
            grow.addWidget(ren)
            grow.addWidget(ung)
            lay.addLayout(grow)
        else:
            self._header(lay, "layers", theme.palette().accent, f"{n} itens selecionados", " · ".join(parts))
        names = QLabel("<br>".join(html.escape(x.name) for x in (layers + jobs)[:10])
                       + ("<br>…" if n > 10 else ""))
        names.setObjectName("Muted")
        names.setWordWrap(True)
        lay.addWidget(names)
        grid = QGridLayout()
        grid.setSpacing(6)
        ids = [("layer", e.id) for e in layers] + [("job", j.id) for j in jobs]

        def add(i: int, text: str, icon_name: str, tip: str, action: str) -> None:
            b = QPushButton(text)
            b.setIcon(icon(icon_name))
            b.setToolTip(tip)
            b.setAccessibleName(text.replace("…", ""))
            b.clicked.connect(lambda: self.batchRequested.emit(action, ids))
            grid.addWidget(b, i // 2, i % 2)

        add(0, "Mostrar", "eye", "Tornar visíveis todos os selecionados", "show")
        add(1, "Ocultar", "eye-off", "Ocultar todos os selecionados", "hide")
        add(2, "Mostrar só estes", "eye", "Ocultar tudo que não está selecionado", "solo")
        add(3, "Enquadrar", "fit", "Ajustar a vista aos itens selecionados", "zoom")
        i = 4
        if layers:
            add(i, "Virar (dupla face)…", "mirror",
                "Espelhar juntas as camadas selecionadas — e os caminhos delas — para fresar o lado de baixo",
                "flip"); i += 1
        if group is None and len(layers) >= 2:
            add(i, "Agrupar", "group", "Agrupar as camadas selecionadas para que se movam juntas (Ctrl+G)",
                "group"); i += 1
        if jobs:
            add(i, "Salvar G-codes…", "save", "Gerar e salvar numa pasta um arquivo .nc para cada caminho "
                "selecionado, com nomes seguros para cartão SD (Ctrl+Shift+E)", "save_gcodes"); i += 1
        add(i, "Remover", "trash", "Remover todos os selecionados (Ctrl+Z desfaz)", "remove")
        lay.addLayout(grid)
        tip = QLabel("Dica: Ctrl+clique soma itens à seleção, Shift+clique seleciona um intervalo e Ctrl+A "
                     "seleciona tudo. Espaço mostra/oculta os selecionados.")
        tip.setObjectName("Muted")
        tip.setWordWrap(True)
        lay.addWidget(tip)
        lay.addStretch(1)
        self.help.set_default("Seleção múltipla", "Ações aplicadas a todos os itens selecionados de uma vez.")
        self._set_primary("Ações em lote acima", "layers", False)

    # ------------------------------------------------------------------ camada
    def _section_state(self, key: str, default: bool) -> bool:
        return bool(self.settings.get(key, default))

    def _collapsible(self, title: str, content: QWidget, key: str, expanded: bool,
                     summary: str = "") -> CollapsibleSection:
        return CollapsibleSection(title, content, expanded, summary=summary, key=key,
                                  state_get=self._section_state,
                                  state_set=lambda k, v: self.settings.set(k, v))

    def show_layer(self, e: LayerEntry) -> None:
        self.entry = e
        self._gcode_target = None
        self.post_form = None
        lay = self._new_body()
        kind_lbl = {"gerber": "Gerber", "excellon": "Excellon (furação)", "geometry": "Geometria"}.get(e.kind, e.kind)
        icon_name = {"gerber": "copper", "excellon": "drill"}.get(e.kind, "outline")
        self._header(lay, icon_name, e.color, e.name, f"{kind_lbl} · {e.summary()}")

        # papel e cor (uma linha)
        row = QHBoxLayout()
        row.setSpacing(6)
        role_lbl = QLabel("&Papel")
        role_lbl.setObjectName("Muted")
        self.role = Combo()
        self.role.setAccessibleName("Papel da camada")
        self.role.setToolTip("Função da camada na placa. Define a cor padrão e as sugestões do assistente.")
        for k, lab in ROLE_LABELS.items():
            self.role.addItem(lab, k)
        self.role.setCurrentIndex(max(0, self.role.findData(e.role)))
        self.role.currentIndexChanged.connect(lambda: self.roleChanged.emit(e.id, self.role.currentData()))
        role_lbl.setBuddy(self.role)
        color = QToolButton()
        color.setToolTip("Cor da camada no canvas")
        color.setAccessibleName("Cor da camada")
        sw = QColor(e.color)
        color.setStyleSheet(f"QToolButton {{ background: {sw.name()}; border: 1px solid {theme.palette().border_strong};"
                            f" border-radius: 6px; min-width: 22px; min-height: 22px; }}")
        color.clicked.connect(lambda: self._pick_color(e))
        row.addWidget(role_lbl)
        row.addWidget(self.role, 1)
        row.addWidget(color)
        lay.addLayout(row)

        warnings = list(getattr(e.layer, "warnings", []) or [])
        if warnings:
            wl = QLabel("⚠ " + "<br>⚠ ".join(html.escape(x) for x in warnings[:6]))
            wl.setWordWrap(True)
            wl.setStyleSheet(f"color: {theme.palette().warning};")
            lay.addWidget(wl)

        if isinstance(e.layer, DrillLayer):
            self._add_layer_drill_table(lay, e)

        # ---- operação (o principal: fica no topo)
        self._ops = backend.operations_for(e.kind)
        self.op_combo = None
        if not self._ops:
            msg = QLabel("Nenhuma operação disponível para este tipo de camada." if backend.status()["cam"]
                         else "O módulo de CAM ainda não está disponível.")
            msg.setObjectName("Muted")
            msg.setWordWrap(True)
            lay.addWidget(msg)
            self.form = None
            self.help.set_default(e.name, "Camada sem operações disponíveis.")
            self._set_primary("Gerar caminho", "play", False)
        else:
            orow = QHBoxLayout()
            orow.setSpacing(6)
            olab = QLabel("&Operação")
            olab.setObjectName("Muted")
            self.op_combo = Combo()
            self.op_combo.setAccessibleName("Operação de CAM")
            self.op_combo.setToolTip("O que fazer com esta camada. Também disponível na faixa de comandos "
                                     "(grupo Caminhos).")
            for op in self._ops:
                self.op_combo.addItem(icon(OP_ICONS.get(op.key, "job")), op.title, op.key)
                self.op_combo.setItemData(self.op_combo.count() - 1, op.description, Qt.ItemDataRole.ToolTipRole)
            olab.setBuddy(self.op_combo)
            orow.addWidget(olab)
            orow.addWidget(self.op_combo, 1)
            lay.addLayout(orow)
            if self.op_key not in [o.key for o in self._ops]:
                self.op_key = self._preferred_op(e)
            self.op_combo.setCurrentIndex(max(0, self.op_combo.findData(self.op_key)))
            self.op_combo.currentIndexChanged.connect(lambda _i: self._select_op(self.op_combo.currentData()))
            self.form_host = QVBoxLayout()
            self.form_host.setSpacing(8)
            lay.addLayout(self.form_host)

        # ---- posição (recolhida por padrão)
        b = e.effective.bounds
        pos_w = QWidget()
        tg = QGridLayout(pos_w)
        tg.setContentsMargins(0, 0, 0, 0)
        tg.setHorizontalSpacing(8)
        tg.setVerticalSpacing(6)
        board = QPushButton("Posicionar placa inteira no zero…")
        board.setIcon(icon("move"))
        board.setToolTip("Move/gira todas as camadas e caminhos juntos para o zero de trabalho (Ctrl+Shift+A)")
        board.setAccessibleName("Posicionar placa inteira no zero")
        board.clicked.connect(self.alignRequested.emit)
        tg.addWidget(board, 0, 0, 1, 3)
        mir_lbl = QLabel("Espelhar")
        mir_lbl.setObjectName("Muted")
        self.mirror = Combo()
        self.mirror.setAccessibleName("Espelhar camada")
        self.mirror.setToolTip("Espelha a camada em torno do centro da placa. Use para o lado de baixo "
                               "(bottom) em placas fresadas pelo verso.")
        for v, lab in ((None, "Não espelhar"), ("x", "Horizontal (inverte X)"), ("y", "Vertical (inverte Y)")):
            self.mirror.addItem(lab, v)
        self.mirror.setCurrentIndex(max(0, self.mirror.findData(e.transform.mirror)))
        mir_lbl.setBuddy(self.mirror)
        tg.addWidget(mir_lbl, 1, 0)
        tg.addWidget(self.mirror, 1, 1, 1, 2)
        off_lbl = QLabel("Deslocar")
        off_lbl.setObjectName("Muted")
        self.off_x = DoubleSpin()
        self.off_y = DoubleSpin()
        for sp, ax in ((self.off_x, "X"), (self.off_y, "Y")):
            sp.setRange(-10000, 10000)
            sp.setDecimals(3)
            sp.setPrefix(f"{ax} ")
            sp.setSuffix(f" {units.unit_label()}")
            sp.setAccessibleName(f"Deslocamento em {ax}")
            sp.setMinimumWidth(96)
            sp.setSizePolicy(QSizePolicy.Policy.Ignored, QSizePolicy.Policy.Fixed)
        self.off_x.setValue(units.to_display(e.transform.offset[0]))
        self.off_y.setValue(units.to_display(e.transform.offset[1]))
        off_lbl.setBuddy(self.off_x)
        tg.addWidget(off_lbl, 2, 0)
        tg.addWidget(self.off_x, 2, 1)
        tg.addWidget(self.off_y, 2, 2)
        btns = QHBoxLayout()
        origin = QPushButton("Só esta camada à origem")
        origin.setProperty("flat", True)
        origin.setToolTip("Desloca APENAS esta camada para que o canto inferior esquerdo fique em X0 Y0. "
                          "Atenção: as outras camadas não se movem — para manter o alinhamento entre cobre, "
                          "furos e contorno use “Posicionar placa inteira”.")
        origin.clicked.connect(lambda: self._to_origin(e))
        apply_t = QPushButton("Aplicar")
        apply_t.setAccessibleName("Aplicar transformação")
        apply_t.clicked.connect(lambda: self._apply_transform(e))
        btns.addWidget(origin)
        btns.addStretch(1)
        btns.addWidget(apply_t)
        tg.addLayout(btns, 3, 0, 1, 3)
        tg.setColumnStretch(1, 1)
        tg.setColumnStretch(2, 1)
        if e.group is not None:
            gl = QLabel("⧉ Esta camada está agrupada: deslocar move o grupo inteiro, e espelhar só ela é "
                        "bloqueado para não desalinhar furos e cobre.")
            gl.setObjectName("Muted")
            gl.setWordWrap(True)
            tg.addWidget(gl, 4, 0, 1, 3)
        pos_sum = f"X {units.fmt_len(b[0], 2, False)} · Y {units.fmt_len(b[1], 2, False)}"
        if e.transform.mirror:
            pos_sum += " · espelhada"
        lay.addWidget(self._collapsible("Posição", pos_w, "section_layer_position", False, pos_sum))

        # ---- informações (recolhidas por padrão)
        card, g = _card()
        w, h = b[2] - b[0], b[3] - b[1]
        r = 0
        _kv(g, r, "Tamanho", f"{units.fmt_len(w, 2, False)} × {units.fmt_len(h, 2)}"); r += 1
        _kv(g, r, "Origem", f"X {units.fmt_len(b[0], 3, False)}  Y {units.fmt_len(b[1], 3, False)}"); r += 1
        _kv(g, r, "Conteúdo", e.summary()); r += 1
        lay_obj = e.effective
        if isinstance(lay_obj, DrillLayer) and lay_obj.tools:
            diams = sorted({round(t.diameter, 3) for t in lay_obj.tools.values()})
            _kv(g, r, "Diâmetros", ", ".join(units.fmt_len(d, 2, False) for d in diams) + f" {units.unit_label()}"); r += 1
        if isinstance(lay_obj, GerberLayer) and lay_obj.apertures:
            _kv(g, r, "Aberturas", str(len(lay_obj.apertures))); r += 1
        src = QLabel()
        src.setObjectName("Muted")
        path = e.layer.source_path or "—"
        src.setText(src.fontMetrics().elidedText(path, Qt.TextElideMode.ElideMiddle, 280))
        src.setToolTip(path)
        src.setMinimumWidth(10)
        src.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
        g.addWidget(src, r, 0, 1, 2)
        lay.addWidget(self._collapsible("Informações", card, "section_layer_info", False,
                                        f"{units.fmt_len(w, 1, False)} × {units.fmt_len(h, 1)}"))
        lay.addStretch(1)
        if self._ops:
            self._select_op(self.op_key)

    def select_operation(self, key: str) -> bool:
        """Escolhe a operação (usado pela faixa de comandos). Retorna False se indisponível."""
        if self.op_combo is None or self.op_combo.findData(key) < 0:
            return False
        if self.op_combo.currentData() == key:
            self._select_op(key)
        else:
            self.op_combo.setCurrentIndex(self.op_combo.findData(key))
        if self.form is not None:
            first = next(iter(self.form.rows.values()), None)
            if first is not None:
                first.editor.setFocus()
        return True

    def _preferred_op(self, e: LayerEntry) -> str:
        keys = [o.key for o in self._ops]
        pref = {"outline": "cutout", "drill": "drilling", "top_copper": "isolation",
                "bottom_copper": "isolation"}.get(e.role)
        if pref in keys:
            return pref  # type: ignore[return-value]
        return keys[0]

    def _select_op(self, key: str) -> None:
        self.op_key = key
        op = next((o for o in self._ops if o.key == key), None)
        while self.form_host.count():
            it = self.form_host.takeAt(0)
            if it.widget():
                it.widget().deleteLater()
        if op is None:
            return
        vals = self.settings.group_values(op.group)
        self.form = ParamForm(op.group, vals, show_header=False, help_panel=self.help)
        self.form.valuesChanged.connect(lambda v, g=op.group: self._remember(g, v))
        self.picker = None
        if self.op_combo is not None and self.op_combo.currentData() != key:
            self.op_combo.blockSignals(True)
            self.op_combo.setCurrentIndex(max(0, self.op_combo.findData(key)))
            self.op_combo.blockSignals(False)
        if key in ("isolation", "ncc", "cutout", "drilling"):
            uid = self.settings.get(f"library_tool_{key}") or None
            if library().get(uid) is None:
                uid = None
            self.picker = ToolPicker(key, uid)
            self.picker.toolChosen.connect(lambda t, o=op: self._apply_tool(o, t))
            self.picker.manageRequested.connect(
                lambda: self.libraryRequested.emit(self.picker.current_uid() or "" if self.picker else ""))
            self.form.preset_extra = lambda: {"library_tool": self.picker.current_uid() if self.picker else None}
            self.form.presetApplied.connect(self._preset_applied)
            self.form_host.addWidget(self.picker)
        self.tool_info = None
        if "tool_dia" in self.form.rows:
            self.tool_info = QLabel()
            self.tool_info.setObjectName("ToolInfo")
            self.tool_info.setWordWrap(True)
            self.tool_info.setTextFormat(Qt.TextFormat.RichText)
            self.tool_info.setAccessibleName("Largura efetiva do corte")
            self.form_host.addWidget(self.tool_info)
            self.form.valuesChanged.connect(self._update_tool_info)
            self._update_tool_info(self.form.values())
        self.form_host.addWidget(self.form)
        if self._refresh_drill_bits is not None:
            if key == "drilling":
                # coluna "Broca" da tabela acompanha a associação de diâmetros do formulário
                self.form.valuesChanged.connect(lambda _v: self._refresh_drill_bits and self._refresh_drill_bits())
            self._refresh_drill_bits()
        self.help.set_default(op.title, op.description)
        self._set_primary("&Gerar caminho", "play", not self.busy)

    def _remember(self, group: Any, values: dict) -> None:
        self.settings.set_group_values(group, values)

    def _update_tool_info(self, v: dict) -> None:
        """Mostra a largura real do corte — o que de fato muda entre fresa V e fresa de topo."""
        lab = getattr(self, "tool_info", None)
        if lab is None:
            return
        from brokcam.cam.tools import vbit_effective_width
        pal = theme.palette()
        if v.get("laser_mode"):
            lab.setText(f"<span style='color:{pal.text_muted}'>Laser: largura do traço =</span> "
                        f"<b>{units.fmt_len(float(v.get('tool_dia', 0.1)), 3)}</b>")
            return
        if v.get("tool_shape", "flat") == "vbit":
            w = vbit_effective_width(float(v.get("vbit_angle", 30)), float(v.get("vbit_tip", 0.1)),
                                     float(v.get("cut_z", -0.05)))
            lab.setText(f"<span style='color:{pal.text_muted}'>Largura efetiva do corte:</span> "
                        f"<b>{units.fmt_len(w, 3)}</b> <span style='color:{pal.text_muted}'>"
                        f"— V {float(v.get('vbit_angle', 30)):g}°, ponta {units.fmt_len(float(v.get('vbit_tip', 0.1)), 2)}, "
                        f"Z de corte automático {units.fmt_len(float(v.get('cut_z', 0)), 3)} "
                        f"(travado). Para mudar a profundidade, altere a largura do sulco.</span>")
        else:
            lab.setText(f"<span style='color:{pal.text_muted}'>Largura do corte (= diâmetro):</span> "
                        f"<b>{units.fmt_len(float(v.get('tool_dia', 0.2)), 3)}</b>")

    # ------------------------------------------------------------------ biblioteca
    def _apply_tool(self, op: Any, tool: Any) -> None:
        """Preenche o formulário com geometria e dados de corte da ferramenta escolhida."""
        if self.form is None:
            return
        vals = tool.apply(op.key, self.form.values(), op.group, library())
        self.form.set_values(vals)
        self.form._emit_changed()
        self.settings.set(f"library_tool_{op.key}", tool.uid)
        self.help.set_default(tool.name, f"Ferramenta da biblioteca aplicada: {tool.summary()}. "
                              + (tool.notes or "Os campos de ferramenta e de corte foram preenchidos; "
                                 "ajuste o que precisar antes de gerar."))
        self.help.show_default()

    def _preset_applied(self, raw: dict) -> None:
        if self.picker is None:
            return
        uid = raw.get("library_tool") if raw else None
        self.picker.set_current(uid if library().get(uid) else None)
        self.settings.set(f"library_tool_{self.op_key}", self.picker.current_uid())

    def _library_changed(self) -> None:
        if self.picker is not None:
            self.picker.reload()

    def selected_library_tool(self) -> str | None:
        return self.picker.current_uid() if self.picker is not None else None

    def _pick_color(self, e: LayerEntry) -> None:
        c = QColorDialog.getColor(QColor(e.color), self, "Cor da camada")
        if c.isValid():
            self.colorChanged.emit(e.id, c.name())

    def _apply_transform(self, e: LayerEntry) -> None:
        t = Transform(self.mirror.currentData(), e.transform.center,
                      (units.from_display(self.off_x.value()), units.from_display(self.off_y.value())),
                      e.transform.rotation)
        self.transformRequested.emit(e.id, t)

    def _to_origin(self, e: LayerEntry) -> None:
        tmp = Transform(self.mirror.currentData(), e.transform.center, (0.0, 0.0), e.transform.rotation)
        from .project import apply_transform
        b = apply_transform(e.layer, tmp).bounds
        self.off_x.setValue(units.to_display(-b[0]))
        self.off_y.setValue(units.to_display(-b[1]))
        self._apply_transform(e)

    # ------------------------------------------------------------------ job
    def show_job(self, j: JobEntry) -> None:
        self.entry = j
        self._gcode_target = None
        self.form = None
        self.op_combo = None
        lay = self._new_body()
        ops = backend.operations()
        op_title = ops[j.job.operation].title if j.job.operation in ops else (j.job.operation or "Caminho")
        self._header(lay, OP_ICONS.get(j.job.operation, "job"),
                     j.color or theme.palette().cut, j.name, f"Caminho de ferramenta · {op_title}")

        st = j.job.stats()
        grid = QGridLayout()
        grid.setSpacing(6)
        i = 0
        cut_len = float(st.get("cut_length_mm", 0.0) or 0.0)
        only_holes = bool(st.get("holes")) and cut_len < 1e-6
        if only_holes:
            # furação pura: comprimento de corte (0 mm) não diz nada — mostra brocas
            n_bits = len({(round(float(o.tool.diameter), 4), o.tool.shape) for o in j.job.ops})
            _stat(grid, i, "furos", str(int(st["holes"]))); i += 1
            _stat(grid, i, "tempo estimado", units.fmt_time(j.estimate_s) if j.estimate_s else "—"); i += 1
            _stat(grid, i, "broca" if n_bits == 1 else "brocas", str(n_bits)); i += 1
        else:
            _stat(grid, i, "comprimento de corte", units.fmt_len(cut_len, 0)); i += 1
            _stat(grid, i, "tempo estimado", units.fmt_time(j.estimate_s) if j.estimate_s else "—"); i += 1
            if st.get("holes"):
                _stat(grid, i, "furos", str(int(st["holes"]))); i += 1
        if j.job.ops:
            p = j.job.ops[0].params
            _stat(grid, i, "profundidade", units.fmt_len(p.cut_z, 3)); i += 1
            if not st.get("holes"):
                _stat(grid, i, "avanço XY", f"{units.fmt_len(p.feed_xy, 0, False)} {units.unit_label()}/min"); i += 1
        lay.addLayout(grid)
        tools = ", ".join(sorted({(o.tool.name or units.fmt_len(o.tool.diameter, 2)) for o in j.job.ops}))
        tl = QLabel(f"<span style='color:{theme.palette().text_muted}'>Ferramenta:</span> {html.escape(tools or '—')}")
        tl.setWordWrap(True)
        lay.addWidget(tl)
        if j.job.settings.get("edited"):
            ed = QLabel("✎ Editado manualmente — a edição fica salva no projeto")
            ed.setStyleSheet(f"color: {theme.palette().warning};")
            ed.setWordWrap(True)
            lay.addWidget(ed)
        for w in (j.job.settings.get("warnings") or [])[:4]:
            wl = QLabel("⚠ " + html.escape(str(w)))
            wl.setWordWrap(True)
            wl.setStyleSheet(f"color: {theme.palette().warning};")
            lay.addWidget(wl)
        if j.job.operation == "drilling":
            self._add_job_drill_table(lay, j)

        acts = QGridLayout()
        acts.setSpacing(6)

        def act(text: str, icon_name: str, tip: str, slot: Any, enabled: bool = True) -> QPushButton:
            b = QPushButton(text)
            b.setIcon(icon(icon_name))
            b.setToolTip(tip)
            b.setAccessibleName(text.replace("…", ""))
            b.setEnabled(enabled)
            b.clicked.connect(slot)
            return b

        acts.addWidget(act("Simular", "simulate", "Animar o caminho da ferramenta no canvas (Ctrl+R)",
                           lambda: self.simulateRequested.emit(j.id)), 0, 0)
        edit_btn = act("Editar caminhos", "edit-path",
                       "Selecionar e remover caminhos ou trechos inúteis direto no canvas (E)",
                       lambda: self.editPathsRequested.emit(j.id))
        edit_btn.setAccessibleDescription(
            "Abre o modo de edição no canvas: clique ou arraste para selecionar caminhos e furos, Delete "
            "para remover, T para apagar apenas o trecho dentro de uma área. Tudo pode ser desfeito.")
        acts.addWidget(edit_btn, 0, 1)
        if j.job.gcode:
            acts.addWidget(act("Salvar G-code…", "save", "Salvar o G-code deste caminho em arquivo",
                               lambda: self.saveGcodeRequested.emit(j.id)), 1, 0)
            acts.addWidget(act("Enviar à máquina", "send", "Carregar este G-code no painel Máquina",
                               lambda: self.sendRequested.emit(j.id)), 1, 1)
        lay.addLayout(acts)

        grp = backend.grbl_post_group()
        if grp is None:
            msg = QLabel("O pós-processador GRBL ainda não está disponível.")
            msg.setObjectName("Muted")
            msg.setWordWrap(True)
            lay.addWidget(msg)
            self.post_form = None
            self._set_primary("Gerar G-code", "gcode", False)
        else:
            vals = self.settings.group_values(grp)
            self.post_form = ParamForm(grp, vals, show_header=False, help_panel=self.help)
            self.post_form.valuesChanged.connect(lambda v, g=grp: self._remember(g, v))
            variant = vals.get("variant", "")
            try:
                variant = dict(grp.spec("variant").choices).get(variant, variant)
            except KeyError:
                pass
            lay.addWidget(self._collapsible("Pós-processador GRBL", self.post_form, "section_post", False,
                                            str(variant)))
            self.help.set_default(grp.title, grp.description)
            self._set_primary("Gerar &G-code" if not j.job.gcode else "Gerar &G-code novamente", "gcode",
                              not self.busy)
        lay.addStretch(1)

    # ------------------------------------------------------------------ furação por diâmetro
    def _drill_values(self) -> dict[str, Any]:
        """Valores atuais de furação (do formulário aberto, ou os salvos)."""
        if self.form is not None and self.op_key == "drilling":
            vals = dict(self.form.values())
        else:
            op = backend.operations().get("drilling")
            vals = dict(self.settings.group_values(op.group)) if op is not None else {}
        uid = self.selected_library_tool() if self.op_key == "drilling" else None
        if uid:
            vals["library_tool"] = uid
        return vals

    def _layer_bits(self, e: LayerEntry, rows: list[dict[str, Any]]) -> list[tuple[float, bool] | None] | None:
        """Broca que cada linha (diâmetro) da tabela da camada vai usar com as opções atuais."""
        try:
            from brokcam.cam.drilling import bit_assignment
            amap = bit_assignment(e.effective, self._drill_values())     # type: ignore[arg-type]
        except Exception:  # noqa: BLE001 - valores inválidos no formulário: só não mostra a coluna
            return None
        out: list[tuple[float, bool] | None] = []
        for r in rows:
            bs = {amap[n] for n in r["sources"] if n in amap}
            out.append(next(iter(bs)) if len(bs) == 1 else (max(bs) if bs else None))
        return out

    def _add_layer_drill_table(self, lay: QVBoxLayout, e: LayerEntry) -> None:
        rows = layer_drill_rows(e.effective)            # type: ignore[arg-type]
        if not rows:
            return
        w = QWidget()
        v = QVBoxLayout(w)
        v.setContentsMargins(0, 0, 0, 0)
        v.setSpacing(6)
        table = DrillTable(rows, "Ø furo")
        table.rankSelected.connect(lambda rk: self.drillHighlight.emit(e.id, rk))
        v.addWidget(table)
        # Decisão (QA): "Gerar só este diâmetro" gera os furos do diâmetro escolhido com a broca
        # que a associação ATUAL escolheria (a mesma do programa completo — sem resultado
        # diferente entre um caminho e outro). Quando essa broca difere do furo, a coluna
        # "Broca" da tabela mostra isso (⚠) e o rótulo do botão passa a citar a broca.
        brow = QVBoxLayout()            # empilhados: lado a lado não cabem no inspetor estreito
        brow.setSpacing(6)
        btn = QPushButton("Gerar só este diâmetro")
        btn.setIcon(icon("drill"))
        btn.setEnabled(False)
        btn.setToolTip("Gera um caminho de furação apenas com os furos do diâmetro selecionado na tabela "
                       "(as demais opções de Furação são as do formulário abaixo)")
        btn.setAccessibleName("Gerar furação só do diâmetro selecionado")
        allb = QPushButton("Gerar um G-code por broca")
        allb.setIcon(icon("drill", "accent_text"))
        allb.setProperty("primary", True)
        allb.setToolTip("Cria de uma vez um caminho (com G-code próprio, sem pausas de troca) para cada broca, "
                        "usando as opções de Furação abaixo. Ctrl+Z desfaz todos juntos.")
        allb.setAccessibleName("Gerar um G-code por broca")
        allb.setEnabled(not self.busy)
        for b in (allb, btn):
            b.setMinimumWidth(10)
            b.setSizePolicy(QSizePolicy.Policy.Ignored, QSizePolicy.Policy.Fixed)
            brow.addWidget(b)
        self.drill_only_btn = btn
        self.drill_all_btn = allb

        def sync_btn(_rk: Any = None) -> None:
            r = table.current_row()
            btn.setEnabled(r is not None and not self.busy)
            b = table.bit_of(r) if r is not None else None
            if r is not None and b is not None and (b[1] or abs(b[0] - r["dia"]) > 1e-6):
                tool = "fresa" if b[1] else "broca"
                btn.setText(f"Gerar Ø{_fmt_dia(r['dia'])} (com {tool} {_fmt_dia(b[0])})")
                btn.setAccessibleName(f"Gerar furação só do diâmetro {_fmt_dia(r['dia'])}, "
                                      f"feita com a {tool} de {_fmt_dia(b[0])}")
            else:
                btn.setText("Gerar só este diâmetro")
                btn.setAccessibleName("Gerar furação só do diâmetro selecionado")

        table.rankSelected.connect(sync_btn)
        self._refresh_drill_bits = lambda: (table.set_bits(self._layer_bits(e, rows)), sync_btn())

        def gen() -> None:
            r = table.current_row()
            if r is None:
                return
            vals = self._drill_values()
            vals.update({"tool_select": ", ".join(f"T{n}" for n in r["sources"]),
                         "split_jobs": "single", "bit_filter": 0.0})
            self.generateOnceRequested.emit(e.id, "drilling", vals)

        def gen_all() -> None:
            vals = self._drill_values()
            vals.update({"tool_select": "", "split_jobs": "per_bit", "bit_filter": 0.0})
            self.generateOnceRequested.emit(e.id, "drilling", vals)

        btn.clicked.connect(gen)
        allb.clicked.connect(gen_all)
        v.addLayout(brow)
        hint = QLabel("Cada cor é um diâmetro, igual no canvas. Selecione uma linha para destacar esses furos. "
                      "<b>Gerar um G-code por broca</b> cria um caminho para cada broca de uma vez.")
        hint.setObjectName("Muted")
        hint.setWordWrap(True)
        v.addWidget(hint)
        self.drill_table = table
        n = sum(r["holes"] for r in rows)
        lay.addWidget(self._collapsible("Diâmetros de furo", w, "section_layer_drills", True,
                                        f"{len(rows)} diâmetro{'s' if len(rows) != 1 else ''} · {n} furos"))

    def _add_job_drill_table(self, lay: QVBoxLayout, j: JobEntry) -> None:
        rows = job_drill_rows(j.job)
        if not rows:
            return
        w = QWidget()
        v = QVBoxLayout(w)
        v.setContentsMargins(0, 0, 0, 0)
        v.setSpacing(6)
        table = DrillTable(rows, "Ø broca")
        table.rankSelected.connect(lambda rk: self.drillHighlight.emit(j.id, rk))
        v.addWidget(table)
        src_ok = j.job.settings.get("bits") and not j.job.settings.get("edited") and len(rows) > 1
        if src_ok:
            btn = QPushButton("Gerar só esta broca")
            btn.setIcon(icon("drill"))
            btn.setEnabled(False)
            btn.setToolTip("Cria um trabalho separado (com G-code próprio, sem pausas) só com a broca "
                           "selecionada, usando as mesmas opções deste caminho")
            btn.setAccessibleName("Gerar trabalho só da broca selecionada")
            table.rankSelected.connect(lambda rk: btn.setEnabled(rk is not None and not self.busy))

            def gen() -> None:
                r = table.current_row()
                if r is None:
                    return
                vals = {k: val for k, val in j.job.settings.items()
                        if k not in ("warnings", "bits", "holes_in", "edited")}
                vals.update({"split_jobs": "single", "bit_filter": -1.0 if r.get("milled") else float(r["dia"])})
                self.generateOnceRequested.emit(j.job.source_id, "drilling", vals)

            btn.clicked.connect(gen)
            v.addWidget(btn)
        self.drill_table = table
        n = sum(r["holes"] for r in rows)
        lay.addWidget(self._collapsible("Brocas deste caminho", w, "section_job_drills", True,
                                        f"{len(rows)} broca{'s' if len(rows) != 1 else ''} · {n} furos"))

    # ------------------------------------------------------------------ rodapé
    def _set_primary(self, text: str, icon_name: str, enabled: bool) -> None:
        self.primary.setText(text)
        self.primary.setIcon(icon(icon_name, "accent_text"))
        self.primary.setEnabled(enabled)
        self.primary.setAccessibleName(text.replace("&", ""))

    def _primary_clicked(self) -> None:
        e = self.entry
        if e is None and getattr(self, "_gcode_target", None):
            self.gcodeAction.emit("send", *self._gcode_target)
        elif e is None:
            self.openRequested.emit()
        elif isinstance(e, LayerEntry) and self.form is not None:
            vals = dict(self.form.values())
            uid = self.selected_library_tool()
            if uid:
                vals["library_tool"] = uid
            self.generateRequested.emit(e.id, self.op_key, vals)
        elif isinstance(e, JobEntry) and self.post_form is not None:
            self.gcodeRequested.emit(e.id, self.post_form.values())

    def set_busy(self, busy: bool, frac: float = 0.0, msg: str = "") -> None:
        self.busy = busy
        self.progress_row.setVisible(busy)
        self.progress.setValue(int(frac * 1000))
        self.progress_msg.setText(msg)
        if self.drill_all_btn is not None:
            self.drill_all_btn.setEnabled(not busy)
        if self.drill_only_btn is not None and self.drill_table is not None:
            self.drill_only_btn.setEnabled(not busy and self.drill_table.current_row() is not None)
        if isinstance(self.entry, LayerEntry):
            self.primary.setEnabled(not busy and self.form is not None)
        elif isinstance(self.entry, JobEntry):
            self.primary.setEnabled(not busy and self.post_form is not None)
