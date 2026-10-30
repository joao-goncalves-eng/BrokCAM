"""Assistente "Gerber → GRBL em 4 passos".

1. Importar arquivos e detectar papéis (``guess_role``).
2. Configurar ferramentas/máquina (formulários dos grupos das operações e do pós).
3. Gerar isolamento + furação + recorte (em segundo plano, com progresso e cancelar).
4. Revisar e exportar/enviar.
"""
from __future__ import annotations

import os
from dataclasses import dataclass, field
from typing import Any, Callable

import numpy as np
from PySide6.QtCore import Qt, Signal
from PySide6.QtWidgets import (
    QCheckBox, QComboBox, QFileDialog, QHBoxLayout, QHeaderView, QLabel, QListWidget,
    QProgressBar, QPushButton, QRadioButton, QScrollArea, QTableWidget,
    QTableWidgetItem, QTabWidget, QToolButton, QVBoxLayout, QWidget, QWizard, QWizardPage, QFrame,
)

from brokcam.core.model import Job

from . import backend, units
from .icons import icon
from .param_form import HelpPanel, ParamForm
from .project import Transform, apply_transform
from .settings import AppSettings
from .theme import ROLE_LABELS
from .workers import Task, TaskRunner

FILE_FILTER = ("Arquivos de PCB (*.gbr *.ger *.gtl *.gbl *.gko *.gm1 *.gml *.gto *.gbo *.gts *.gbs "
               "*.gtp *.gbp *.drl *.xln *.exc *.txt *.drd *.cmp *.sol *.art *.pho);;Todos os arquivos (*)")

ROLE_OPS: dict[str, list[str]] = {
    "top_copper": ["isolation"], "bottom_copper": ["isolation"],
    "drill": ["drilling"], "outline": ["cutout"],
}
OP_ORDER = {"isolation": 0, "ncc": 1, "drilling": 2, "cutout": 3}


@dataclass
class WizardFile:
    path: str
    role: str
    layer: Any = None            # camada já carregada (se veio do projeto)
    layer_id: int | None = None  # id no projeto, se já existir


@dataclass
class WizardResult:
    layers: list[tuple[Any, str, Transform, int | None]] = field(default_factory=list)  # layer, role, transform, id existente
    jobs: list[tuple[int, Job]] = field(default_factory=list)                           # índice da camada, job
    program: str = ""
    estimate_s: float = 0.0
    log: list[str] = field(default_factory=list)


# =========================================================================== páginas

class ImportPage(QWizardPage):
    def __init__(self, wiz: "PcbWizard") -> None:
        super().__init__()
        self.wiz = wiz
        self.setTitle("1. Importar arquivos")
        self.setSubTitle("Adicione os arquivos Gerber e Excellon da placa. O papel de cada um é detectado "
                         "pelo nome — confira e corrija na coluna Papel.")
        lay = QVBoxLayout(self)
        lay.setSpacing(8)
        bar = QHBoxLayout()
        add = QPushButton("&Adicionar arquivos…")
        add.setIcon(icon("plus"))
        add.clicked.connect(self._add)
        rem = QPushButton("&Remover")
        rem.setIcon(icon("trash"))
        rem.clicked.connect(self._remove)
        bar.addWidget(add)
        bar.addWidget(rem)
        bar.addStretch(1)
        lay.addLayout(bar)
        self.table = QTableWidget(0, 3)
        self.table.setHorizontalHeaderLabels(["Arquivo", "Papel", "Origem"])
        self.table.verticalHeader().setVisible(False)
        self.table.setAccessibleName("Arquivos a importar")
        self.table.setSelectionBehavior(QTableWidget.SelectionBehavior.SelectRows)
        hh = self.table.horizontalHeader()
        hh.setSectionResizeMode(0, QHeaderView.ResizeMode.Stretch)
        hh.setSectionResizeMode(1, QHeaderView.ResizeMode.ResizeToContents)
        hh.setSectionResizeMode(2, QHeaderView.ResizeMode.ResizeToContents)
        lay.addWidget(self.table, 1)
        self.mirror = QCheckBox("Placa de face única fresada pelo lado de baixo: espelhar cobre inferior, "
                                "furação e contorno")
        self.mirror.setToolTip("Espelha horizontalmente em torno do centro da placa para que o desenho "
                               "visto de baixo corresponda à placa física virada.")
        lay.addWidget(self.mirror)
        self.hint = QLabel()
        self.hint.setObjectName("Muted")
        self.hint.setWordWrap(True)
        lay.addWidget(self.hint)
        self.files: list[WizardFile] = []

    def add_files(self, files: list[WizardFile]) -> None:
        known = {os.path.normcase(os.path.abspath(f.path)) for f in self.files}
        for f in files:
            if os.path.normcase(os.path.abspath(f.path)) in known:
                continue
            self.files.append(f)
            r = self.table.rowCount()
            self.table.insertRow(r)
            it = QTableWidgetItem(os.path.basename(f.path))
            it.setToolTip(f.path)
            it.setFlags(it.flags() & ~Qt.ItemFlag.ItemIsEditable)
            self.table.setItem(r, 0, it)
            combo = QComboBox()
            combo.setAccessibleName(f"Papel de {os.path.basename(f.path)}")
            for k, lab in ROLE_LABELS.items():
                combo.addItem(lab, k)
            combo.setCurrentIndex(max(0, combo.findData(f.role)))
            combo.currentIndexChanged.connect(lambda _i, ff=f, c=combo: setattr(ff, "role", c.currentData()))
            combo.currentIndexChanged.connect(self._update_hint)
            self.table.setCellWidget(r, 1, combo)
            src = QTableWidgetItem("projeto aberto" if f.layer is not None else "arquivo")
            src.setFlags(src.flags() & ~Qt.ItemFlag.ItemIsEditable)
            self.table.setItem(r, 2, src)
        self._update_hint()
        self.completeChanged.emit()

    def _add(self) -> None:
        paths, _ = QFileDialog.getOpenFileNames(self, "Adicionar arquivos da placa", self.wiz.last_dir, FILE_FILTER)
        if paths:
            self.wiz.last_dir = os.path.dirname(paths[0])
            self.add_files([WizardFile(p, backend.guess_role(p)) for p in paths])

    def _remove(self) -> None:
        rows = sorted({i.row() for i in self.table.selectedIndexes()}, reverse=True)
        for r in rows:
            self.table.removeRow(r)
            del self.files[r]
        self._update_hint()
        self.completeChanged.emit()

    def _update_hint(self) -> None:
        roles = [f.role for f in self.files]
        parts = []
        for role, txt in (("top_copper", "cobre superior"), ("bottom_copper", "cobre inferior"),
                          ("drill", "furação"), ("outline", "contorno")):
            n = roles.count(role)
            parts.append(f"{'✓' if n else '—'} {txt}")
        missing = "" if "outline" in roles else (" Sem contorno: o recorte usará o retângulo envolvente "
                                                 "do cobre, se a operação suportar.")
        self.hint.setText("   ".join(parts) + "." + missing)
        self.mirror.setEnabled("bottom_copper" in roles)

    def isComplete(self) -> bool:  # noqa: N802
        return any(f.role in ROLE_OPS for f in self.files)


class ConfigPage(QWizardPage):
    def __init__(self, wiz: "PcbWizard") -> None:
        super().__init__()
        self.wiz = wiz
        self.setTitle("2. Ferramentas e máquina")
        self.setSubTitle("Confira os parâmetros principais. Os valores ficam salvos para a próxima vez.")
        lay = QVBoxLayout(self)
        lay.setSpacing(8)
        ops_row = QHBoxLayout()
        self.op_checks: dict[str, QCheckBox] = {}
        for key, label, default in (("isolation", "Isolamento", True), ("drilling", "Furação", True),
                                    ("cutout", "Recorte", True), ("ncc", "Limpeza de cobre (NCC)", False)):
            cb = QCheckBox(label)
            cb.setChecked(default)
            self.op_checks[key] = cb
            ops_row.addWidget(cb)
        ops_row.addStretch(1)
        lay.addLayout(ops_row)
        self.tabs = QTabWidget()
        self.tabs.setDocumentMode(True)
        lay.addWidget(self.tabs, 1)
        self.help = HelpPanel()
        self.help.setMaximumHeight(170)
        lay.addWidget(self.help)
        self.forms: dict[str, ParamForm] = {}

    def initializePage(self) -> None:  # noqa: N802
        self.tabs.clear()
        self.forms.clear()
        ops = backend.operations()
        for key in ("isolation", "drilling", "cutout", "ncc"):
            cb = self.op_checks[key]
            op = ops.get(key)
            cb.setEnabled(op is not None)
            if op is None:
                cb.setChecked(False)
                cb.setToolTip("Operação indisponível (módulo de CAM pendente)")
                continue
            self.forms[key] = self._add_tab(op.group, op.title)
        post = backend.grbl_post_group()
        if post is not None:
            self.forms["__post__"] = self._add_tab(post, "Máquina / G-code")
        if not self.forms:
            lab = QLabel("Os módulos de CAM e de G-code ainda não estão disponíveis.")
            lab.setWordWrap(True)
            self.tabs.addTab(lab, "Indisponível")

    def _add_tab(self, group: Any, title: str) -> ParamForm:
        form = ParamForm(group, self.wiz.settings.group_values(group), show_header=False, help_panel=self.help)
        sc = QScrollArea()
        sc.setWidgetResizable(True)
        sc.setFrameShape(QFrame.Shape.NoFrame)
        host = QWidget()
        hl = QVBoxLayout(host)
        hl.setContentsMargins(4, 8, 12, 4)
        hl.addWidget(form)
        hl.addStretch(1)
        sc.setWidget(host)
        self.tabs.addTab(sc, title)
        return form

    def validatePage(self) -> bool:  # noqa: N802
        for key, form in self.forms.items():
            self.wiz.settings.set_group_values(form.group, form.values())
        self.wiz.settings.save()
        return True

    def selected_ops(self) -> list[str]:
        return [k for k, cb in self.op_checks.items() if cb.isChecked() and cb.isEnabled()]

    def values(self, key: str) -> dict[str, Any]:
        f = self.forms.get(key)
        return f.values() if f else {}


class GeneratePage(QWizardPage):
    def __init__(self, wiz: "PcbWizard") -> None:
        super().__init__()
        self.wiz = wiz
        self.setTitle("3. Gerar caminhos")
        self.setSubTitle("Lendo os arquivos e calculando isolamento, furação e recorte em segundo plano.")
        lay = QVBoxLayout(self)
        lay.setSpacing(8)
        self.bar = QProgressBar()
        self.bar.setRange(0, 1000)
        self.bar.setAccessibleName("Progresso da geração")
        self.msg = QLabel()
        self.msg.setObjectName("Muted")
        row = QHBoxLayout()
        row.addWidget(self.bar, 1)
        self.cancel = QToolButton()
        self.cancel.setIcon(icon("cancel"))
        self.cancel.setToolTip("Cancelar")
        self.cancel.setAccessibleName("Cancelar geração")
        self.cancel.clicked.connect(self._cancel)
        row.addWidget(self.cancel)
        lay.addLayout(row)
        lay.addWidget(self.msg)
        self.log = QListWidget()
        self.log.setAccessibleName("Etapas concluídas")
        lay.addWidget(self.log, 1)
        self.retry = QPushButton("Tentar novamente")
        self.retry.setIcon(icon("refresh"))
        self.retry.clicked.connect(self.initializePage)
        self.retry.hide()
        lay.addWidget(self.retry, 0, Qt.AlignmentFlag.AlignLeft)
        self.task: Task | None = None
        self.done = False

    def initializePage(self) -> None:  # noqa: N802
        self.done = False
        self.retry.hide()
        self.log.clear()
        self.bar.setValue(0)
        self.wiz.result = None
        cfg = self.wiz.config_page
        files = list(self.wiz.import_page.files)
        mirror = self.wiz.import_page.mirror.isChecked() and self.wiz.import_page.mirror.isEnabled()
        ops = cfg.selected_ops()
        values = {k: cfg.values(k) for k in ops}
        post = cfg.values("__post__")
        rapid = float(self.wiz.settings.ui("rapid_rate"))
        fn = _make_job(files, mirror, ops, values, post, rapid)
        self.task = self.wiz.runner.submit("Assistente", fn, on_done=self._done, on_error=self._error,
                                           on_cancel=self._cancelled)
        self.task.signals.progress.connect(self._progress)
        self.cancel.setEnabled(True)
        self.completeChanged.emit()

    def _progress(self, frac: float, msg: str) -> None:
        self.bar.setValue(int(frac * 1000))
        if msg and msg != self.msg.text():
            self.msg.setText(msg)
            if msg.startswith("✓") or msg.startswith("⚠"):
                self.log.addItem(msg)

    def _done(self, res: WizardResult) -> None:
        self.wiz.result = res
        self.done = True
        self.bar.setValue(1000)
        self.msg.setText("Concluído.")
        for line in res.log:
            if not self.log.findItems(line, Qt.MatchFlag.MatchExactly):
                self.log.addItem(line)
        self.cancel.setEnabled(False)
        self.completeChanged.emit()

    def _error(self, msg: str, _tb: str) -> None:
        self.msg.setText(f"Falhou: {msg}")
        self.log.addItem(f"✖ {msg}")
        self.cancel.setEnabled(False)
        self.retry.show()

    def _cancelled(self) -> None:
        self.msg.setText("Cancelado.")
        self.cancel.setEnabled(False)
        self.retry.show()

    def _cancel(self) -> None:
        if self.task:
            self.task.cancel()

    def isComplete(self) -> bool:  # noqa: N802
        return self.done

    def cleanupPage(self) -> None:  # noqa: N802
        self._cancel()


class ReviewPage(QWizardPage):
    sendRequested = Signal()

    def __init__(self, wiz: "PcbWizard") -> None:
        super().__init__()
        self.wiz = wiz
        self.setTitle("4. Revisar e exportar")
        self.setSubTitle("Confira os caminhos gerados. Ao concluir, tudo é adicionado ao projeto; "
                         "você pode salvar o G-code agora ou enviá-lo direto à máquina.")
        lay = QVBoxLayout(self)
        lay.setSpacing(8)
        self.table = QTableWidget(0, 4)
        self.table.setHorizontalHeaderLabels(["Caminho", "Corte", "Furos", "Tempo"])
        self.table.verticalHeader().setVisible(False)
        self.table.setEditTriggers(QTableWidget.EditTrigger.NoEditTriggers)
        self.table.setAccessibleName("Caminhos gerados")
        hh = self.table.horizontalHeader()
        hh.setSectionResizeMode(0, QHeaderView.ResizeMode.Stretch)
        for c in (1, 2, 3):
            hh.setSectionResizeMode(c, QHeaderView.ResizeMode.ResizeToContents)
        lay.addWidget(self.table, 1)
        self.total = QLabel()
        lay.addWidget(self.total)
        self.single = QRadioButton("Um único programa (troca de ferramenta com pausa M0/M6)")
        self.multi = QRadioButton("Um arquivo por caminho (recomendado para máquinas sem troca automática)")
        self.multi.setChecked(True)
        lay.addWidget(self.single)
        lay.addWidget(self.multi)
        row = QHBoxLayout()
        self.save_btn = QPushButton("&Salvar G-code…")
        self.save_btn.setIcon(icon("save"))
        self.save_btn.clicked.connect(self._save)
        self.send_btn = QPushButton("Enviar para a &máquina")
        self.send_btn.setIcon(icon("send"))
        self.send_btn.setToolTip("Conclui o assistente e abre a aba Máquina com o programa carregado")
        self.send_btn.clicked.connect(self._send)
        row.addWidget(self.save_btn)
        row.addWidget(self.send_btn)
        row.addStretch(1)
        lay.addLayout(row)
        self.saved = QLabel()
        self.saved.setObjectName("Muted")
        self.saved.setWordWrap(True)
        lay.addWidget(self.saved)

    def initializePage(self) -> None:  # noqa: N802
        res = self.wiz.result
        self.table.setRowCount(0)
        if res is None:
            return
        tot_len = 0.0
        tot_holes = 0
        for _idx, job in res.jobs:
            st = job.stats()
            est = backend.estimate_job_time_s(job, float(self.wiz.settings.ui("rapid_rate")))
            r = self.table.rowCount()
            self.table.insertRow(r)
            for c, v in enumerate((job.name, units.fmt_len(st["cut_length_mm"], 0),
                                   str(int(st["holes"])) if st["holes"] else "—", units.fmt_time(est))):
                self.table.setItem(r, c, QTableWidgetItem(v))
            tot_len += st["cut_length_mm"]
            tot_holes += int(st["holes"])
        has_gcode = any(j.gcode for _i, j in res.jobs) or bool(res.program)
        self.total.setText(f"<b>Total:</b> {len(res.jobs)} caminhos · {units.fmt_len(tot_len, 0)} de corte · "
                           f"{tot_holes} furos · ≈ {units.fmt_time(res.estimate_s)}")
        self.save_btn.setEnabled(has_gcode)
        self.send_btn.setEnabled(has_gcode)
        if not has_gcode:
            self.saved.setText("O pós-processador GRBL não está disponível: os caminhos serão adicionados ao "
                               "projeto, mas o G-code precisa ser gerado depois.")

    def _save(self) -> None:
        res = self.wiz.result
        if res is None:
            return
        if self.single.isChecked() and res.program:
            path, _ = QFileDialog.getSaveFileName(self, "Salvar programa G-code",
                                                  os.path.join(self.wiz.last_dir, "placa.nc"),
                                                  "G-code (*.nc *.gcode *.ngc *.tap)")
            if path:
                with open(path, "w", encoding="utf-8", newline="\n") as fh:
                    fh.write(res.program)
                self.saved.setText(f"Salvo: {path}")
            return
        folder = QFileDialog.getExistingDirectory(self, "Pasta para os arquivos G-code", self.wiz.last_dir)
        if not folder:
            return
        written = []
        for i, (_idx, job) in enumerate(res.jobs, 1):
            if not job.gcode:
                continue
            safe = "".join(ch if ch.isalnum() or ch in "-_ " else "_" for ch in job.name).strip() or f"job{i}"
            path = os.path.join(folder, f"{i:02d}_{safe}.nc")
            with open(path, "w", encoding="utf-8", newline="\n") as fh:
                fh.write(job.gcode)
            written.append(os.path.basename(path))
        self.saved.setText(f"{len(written)} arquivos salvos em {folder}: " + ", ".join(written))

    def _send(self) -> None:
        self.wiz.send_after = True
        self.wiz.accept()


# =========================================================================== trabalho em segundo plano

def _make_job(files: list[WizardFile], mirror: bool, ops: list[str], values: dict[str, dict],
              post: dict[str, Any], rapid: float) -> Callable[..., WizardResult]:
    def run(progress: Callable[[float, str], None], cancel: Callable[[], bool]) -> WizardResult:
        res = WizardResult()
        cancelled = backend.cancelled_exception()
        n_files = max(1, len(files))
        loaded: list[tuple[Any, str, int | None]] = []
        for i, f in enumerate(files):
            if cancel():
                raise cancelled()
            progress(0.25 * i / n_files, f"Lendo {os.path.basename(f.path)}…")
            if f.layer is not None:
                loaded.append((f.layer, f.role, f.layer_id))
                continue
            try:
                lay = backend.load_file(f.path)
            except Exception as exc:  # noqa: BLE001
                res.log.append(f"⚠ {os.path.basename(f.path)}: {exc}")
                progress(0.25 * (i + 1) / n_files, f"⚠ {os.path.basename(f.path)}: {exc}")
                continue
            loaded.append((lay, f.role, None))
            progress(0.25 * (i + 1) / n_files, f"✓ {os.path.basename(f.path)} lido")
        # centro da placa
        outline = [l for l, r, _ in loaded if r == "outline"] or [l for l, _r, _ in loaded]
        bs = np.asarray([l.bounds for l in outline if l.bounds != (0, 0, 0, 0)] or [(0, 0, 0, 0)], dtype=float)
        center = (float((bs[:, 0].min() + bs[:, 2].max()) / 2), float((bs[:, 1].min() + bs[:, 3].max()) / 2))
        for lay, role, lid in loaded:
            t = Transform()
            if mirror and role in ("bottom_copper", "drill", "outline"):
                t = Transform("x", center, (0.0, 0.0))
            res.layers.append((lay, role, t, lid))
        # operações
        all_ops = backend.operations()
        plan: list[tuple[int, str]] = []
        for idx, (_lay, role, _t, _lid) in enumerate(res.layers):
            keys = list(ROLE_OPS.get(role, []))
            if role in ("top_copper", "bottom_copper") and "ncc" in ops:
                keys.append("ncc")
            for k in keys:
                if k in ops and k in all_ops:
                    plan.append((idx, k))
        plan.sort(key=lambda p: OP_ORDER.get(p[1], 9))
        n = max(1, len(plan))
        for j, (idx, key) in enumerate(plan):
            if cancel():
                raise cancelled()
            lay, role, t, _lid = res.layers[idx]
            op = all_ops[key]
            base = 0.25 + 0.65 * j / n
            span = 0.65 / n

            def sub(frac: float, msg: str = "", base: float = base, span: float = span, title: str = op.title) -> None:
                progress(base + span * max(0.0, min(1.0, frac)), f"{title}: {msg}" if msg else title)

            eff = apply_transform(lay, t)
            try:
                out = backend.run_operation(op, eff, values.get(key, {}), progress=sub, cancel=cancel)
            except cancelled:
                raise
            except Exception as exc:  # noqa: BLE001
                res.log.append(f"⚠ {op.title} em {lay.name}: {exc}")
                continue
            # furação com "um trabalho por broca" devolve uma lista de jobs
            for job in (out if isinstance(out, list) else [out]):
                if not job.name:
                    job.name = f"{op.title} — {lay.name}"
                res.jobs.append((idx, job))
                st = job.stats()
                detail = f"{units.fmt_len(st['cut_length_mm'], 0)}" + (f", {int(st['holes'])} furos" if st["holes"] else "")
                what = job.name if isinstance(out, list) else lay.name
                res.log.append(f"✓ {op.title} — {what} ({detail})")
                progress(base + span, f"✓ {op.title} — {what} ({detail})")
        # G-code
        if backend.grbl_post_group() is not None and res.jobs:
            progress(0.92, "Gerando G-code…")
            for _idx, job in res.jobs:
                try:
                    job.gcode = backend.generate_gcode(job, post)
                except Exception as exc:  # noqa: BLE001
                    res.log.append(f"⚠ G-code de {job.name}: {exc}")
            try:
                res.program = backend.generate_program([j for _i, j in res.jobs], post)
            except Exception as exc:  # noqa: BLE001
                res.log.append(f"⚠ Programa único: {exc}")
            res.log.append("✓ G-code gerado")
        res.estimate_s = sum(backend.estimate_job_time_s(j, rapid) for _i, j in res.jobs)
        progress(1.0, "Concluído")
        return res
    return run


# =========================================================================== assistente

class PcbWizard(QWizard):
    def __init__(self, settings: AppSettings, runner: TaskRunner, parent: QWidget | None = None,
                 initial: list[WizardFile] | None = None, last_dir: str = "") -> None:
        super().__init__(parent)
        self.setWindowTitle("Assistente Gerber → GRBL em 4 passos")
        self.setWizardStyle(QWizard.WizardStyle.ModernStyle)
        self.setOption(QWizard.WizardOption.NoBackButtonOnLastPage, False)
        self.setOption(QWizard.WizardOption.HaveHelpButton, False)
        self.resize(900, 700)
        self.settings = settings
        self.runner = runner
        self.last_dir = last_dir
        self.result: WizardResult | None = None
        self.send_after = False
        self.import_page = ImportPage(self)
        self.config_page = ConfigPage(self)
        self.generate_page = GeneratePage(self)
        self.review_page = ReviewPage(self)
        for p in (self.import_page, self.config_page, self.generate_page, self.review_page):
            self.addPage(p)
        self.setButtonText(QWizard.WizardButton.NextButton, "&Avançar ›")
        self.setButtonText(QWizard.WizardButton.BackButton, "‹ &Voltar")
        self.setButtonText(QWizard.WizardButton.FinishButton, "&Concluir")
        self.setButtonText(QWizard.WizardButton.CancelButton, "Cancelar")
        if initial:
            self.import_page.add_files(initial)

    def reject(self) -> None:
        if self.generate_page.task is not None:
            self.generate_page.task.cancel()
        super().reject()


def ask_files(parent: QWidget, start_dir: str) -> list[str]:
    paths, _ = QFileDialog.getOpenFileNames(parent, "Abrir arquivos", start_dir, FILE_FILTER)
    return paths

