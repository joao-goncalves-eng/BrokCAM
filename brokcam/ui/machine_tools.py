"""Painel "Comandos da máquina": posição e offset Z, sonda Z, heightmap (autolevel) e macros.

Tudo roda pelo :class:`GrblBridge` (comandos bloqueantes numa thread auxiliar; resultado
volta por sinal), então a interface nunca trava enquanto a máquina se move.
"""
from __future__ import annotations

import html
import json
import logging
from typing import Any, Callable

import numpy as np
from PySide6.QtCore import QPointF, QRectF, Qt, Signal
from PySide6.QtGui import QColor, QImage, QPainter, QPen
from PySide6.QtWidgets import (
    QCheckBox, QFileDialog, QFrame, QGridLayout, QHBoxLayout, QInputDialog, QLabel, QListWidget,
    QListWidgetItem, QMessageBox, QPlainTextEdit, QProgressBar, QPushButton, QScrollArea, QSizePolicy,
    QTabWidget, QVBoxLayout, QWidget,
)

from brokcam.gcode.autolevel import (
    HeightMap, apply_heightmap, grid_axes, grid_counts_for_spacing, probe_order,
)

from . import theme, units
from .icons import icon
from .param_form import Combo, DoubleSpin
from .settings import AppSettings

log = logging.getLogger("brokcam.ui.machine_tools")

DEFAULT_MACROS = [
    {"name": "Subir Z para 5 mm", "gcode": "G90 G0 Z5"},
    {"name": "Ir para o zero XY (Z seguro)", "gcode": "G90 G0 Z5\nG0 X0 Y0"},
    {"name": "Estacionar (posição G28)", "gcode": "G90 G0 Z5\nG28"},
    {"name": "Ar/refrigeração liga", "gcode": "M8"},
    {"name": "Ar/refrigeração desliga", "gcode": "M9"},
    {"name": "Laser: disparo de teste fraco (1 s)", "gcode": "M3 S10\nG4 P1\nM5"},
]


def _section(title: str, text: str = "") -> QWidget:
    w = QWidget()
    lay = QVBoxLayout(w)
    lay.setContentsMargins(0, 6, 0, 0)
    lay.setSpacing(2)
    t = QLabel(title.upper())
    t.setObjectName("SectionTitle")
    lay.addWidget(t)
    if text:
        d = QLabel(text)
        d.setObjectName("Muted")
        d.setWordWrap(True)
        lay.addWidget(d)
    return w


def _spin(lo: float, hi: float, val: float, dec: int = 3, step: float = 0.1, suffix: str = " mm",
          name: str = "") -> DoubleSpin:
    s = DoubleSpin()
    s.setRange(lo, hi)
    s.setDecimals(dec)
    s.setSingleStep(step)
    s.setValue(val)
    s.setSuffix(suffix)
    s.setAccessibleName(name)
    s.setMinimumWidth(90)
    return s


# =========================================================================== visualização do mapa

class HeightMapView(QWidget):
    """Mapa de cores da superfície sondada, com os pontos e a escala."""

    def __init__(self) -> None:
        super().__init__()
        self.hm: HeightMap | None = None
        self.current = -1
        self.order: list[tuple[int, int]] = []
        self.setMinimumSize(260, 200)
        self.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Expanding)
        self.setAccessibleName("Mapa de alturas da placa")

    def set_map(self, hm: HeightMap | None, current: int = -1) -> None:
        self.hm = hm
        self.current = current
        if hm is not None:
            self.order = probe_order(hm.xs, hm.ys)
        self.update()

    @staticmethod
    def _color(t: float) -> QColor:
        # azul (baixo) → verde → amarelo → vermelho (alto)
        stops = [(0.0, (59, 130, 246)), (0.35, (34, 197, 94)), (0.7, (250, 204, 21)), (1.0, (239, 68, 68))]
        t = min(1.0, max(0.0, t))
        for (a, ca), (b, cb) in zip(stops, stops[1:]):
            if t <= b:
                f = (t - a) / (b - a)
                return QColor(*(int(ca[k] + (cb[k] - ca[k]) * f) for k in range(3)))
        return QColor(*stops[-1][1])

    def paintEvent(self, _ev: Any) -> None:  # noqa: N802
        pal = theme.palette()
        p = QPainter(self)
        p.setRenderHint(QPainter.RenderHint.Antialiasing)
        p.fillRect(self.rect(), QColor(pal.canvas_bg))
        hm = self.hm
        if hm is None:
            p.setPen(QColor(pal.text_muted))
            p.drawText(self.rect(), Qt.AlignmentFlag.AlignCenter | Qt.TextFlag.TextWordWrap,
                       "Nenhum mapa ainda.\nDefina a área e clique em Iniciar sondagem.")
            p.end()
            return
        x0, y0, x1, y1 = hm.bounds
        w, h = max(x1 - x0, 1e-6), max(y1 - y0, 1e-6)
        pad, legend = 18.0, 46.0
        s = min((self.width() - 2 * pad - legend) / w, (self.height() - 2 * pad) / h)
        ox, oy = pad, self.height() - pad

        def m(x: float, y: float) -> QPointF:
            return QPointF(ox + (x - x0) * s, oy - (y - y0) * s)

        st = hm.stats()
        lo, hi = st["min"], st["max"]
        rng = max(hi - lo, 1e-6)
        if hm.complete:
            nx, ny = max(2, int(w * s / 4)), max(2, int(h * s / 4))
            gx, gy = np.meshgrid(np.linspace(x0, x1, nx), np.linspace(y1, y0, ny))
            z = hm.at(gx, gy)
            img = QImage(nx, ny, QImage.Format.Format_RGB32)
            for j in range(ny):
                for i in range(nx):
                    img.setPixelColor(i, j, self._color((z[j, i] - lo) / rng))
            p.drawImage(QRectF(m(x0, y1), m(x1, y0)), img)
        else:
            p.setPen(QPen(QColor(pal.border_strong), 1))
            p.drawRect(QRectF(m(x0, y1), m(x1, y0)))
        # pontos
        for k, (j, i) in enumerate(self.order):
            pt = m(hm.xs[i], hm.ys[j])
            v = hm.z[j, i]
            if np.isfinite(v):
                p.setBrush(self._color((v - lo) / rng))
                p.setPen(QPen(QColor(pal.text), 1))
                p.drawEllipse(pt, 4, 4)
            else:
                p.setBrush(Qt.BrushStyle.NoBrush)
                p.setPen(QPen(QColor(pal.text_muted), 1, Qt.PenStyle.DotLine))
                p.drawEllipse(pt, 3, 3)
            if k == self.current:
                p.setBrush(Qt.BrushStyle.NoBrush)
                p.setPen(QPen(QColor(pal.accent), 2))
                p.drawEllipse(pt, 8, 8)
        # legenda
        lx = min(self.width() - legend + 8, ox + w * s + 14)
        top, bot = pad, self.height() - pad
        for k in range(int(bot - top)):
            p.setPen(self._color(1 - k / max(bot - top, 1)))
            p.drawLine(QPointF(lx, top + k), QPointF(lx + 10, top + k))
        p.setPen(QColor(pal.text_muted))
        f = p.font()
        f.setPointSizeF(max(7.0, f.pointSizeF() * 0.8))
        p.setFont(f)
        p.drawText(QPointF(lx + 13, top + 8), f"{hi:+.3f}")
        p.drawText(QPointF(lx + 13, bot), f"{lo:+.3f}")
        p.end()


# =========================================================================== painel

class MachineToolsPanel(QWidget):
    """Comandos avançados: posição/offset Z, sonda, heightmap e macros."""

    programChanged = Signal(str, str)        # novo texto do programa, descrição (autolevel aplicado/removido)

    def __init__(self, bridge: Any, settings: AppSettings,
                 get_program: Callable[[], tuple[str, str]],
                 get_board_bounds: Callable[[], tuple[float, float, float, float] | None],
                 parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setObjectName("Panel")
        self.bridge = bridge
        self.settings = settings
        self.get_program = get_program
        self.get_board_bounds = get_board_bounds
        self.connected = False
        self.streaming = False
        self.wpos: tuple[float, float, float] | None = None
        self.z_adjust_total = 0.0
        self.hm: HeightMap | None = None
        self._probing = False
        self._cancel = False
        self._original_program: tuple[str, str] | None = None

        root = QVBoxLayout(self)
        root.setContentsMargins(8, 6, 8, 8)
        root.setSpacing(6)
        self.banner = QLabel()
        self.banner.setObjectName("Muted")
        self.banner.setWordWrap(True)
        root.addWidget(self.banner)
        self.tabs = QTabWidget()
        self.tabs.setDocumentMode(True)
        self.tabs.addTab(self._scroll(self._build_position()), icon("move"), "Posição e Z")
        self.tabs.addTab(self._scroll(self._build_probe()), icon("probe"), "Sonda Z")
        self.tabs.addTab(self._build_heightmap(), icon("grid"), "Heightmap (autolevel)")
        self.tabs.addTab(self._scroll(self._build_limits()), icon("warning"), "Fins de curso e homing")
        self.tabs.addTab(self._build_macros(), icon("terminal"), "Macros")
        root.addWidget(self.tabs, 1)

        bridge.connection.connect(self._on_connection)
        bridge.status.connect(self._on_status)
        bridge.streaming.connect(self._on_streaming)
        bridge.commandResult.connect(self._on_result)
        bridge.commandFailed.connect(self._on_failed)
        self._update_enabled()

    # ------------------------------------------------------------------ util
    @staticmethod
    def _scroll(w: QWidget) -> QScrollArea:
        sa = QScrollArea()
        sa.setWidgetResizable(True)
        sa.setFrameShape(QFrame.Shape.NoFrame)
        sa.setWidget(w)
        return sa

    def _ctrl(self) -> Any:
        return getattr(self.bridge, "ctrl", None)

    def _run(self, name: str, method: str, *args: Any, **kwargs: Any) -> None:
        c = self._ctrl()
        if c is None or not self.connected:
            self._say("Conecte-se à máquina primeiro (painel Máquina).", error=True)
            return
        if self.streaming:
            self._say("Há um programa em execução — aguarde terminar ou pare o envio.", error=True)
            return
        fn = getattr(c, method, None)
        if fn is None:
            self._say(f"O controlador não suporta “{method}”.", error=True)
            return
        self.bridge.run(f"tools:{name}", fn, *args, **kwargs)

    def _say(self, text: str, error: bool = False) -> None:
        col = theme.palette().danger if error else theme.palette().text_muted
        self.banner.setText(f"<span style='color:{col}'>{html.escape(text)}</span>")

    def _btn(self, text: str, icon_name: str, tip: str, slot: Any, primary: bool = False) -> QPushButton:
        b = QPushButton(text)
        if icon_name:
            b.setIcon(icon(icon_name, "accent_text" if primary else None))
        b.setToolTip(tip)
        b.setAccessibleName(text.replace("…", ""))
        b.setAccessibleDescription(tip)
        if primary:
            b.setProperty("primary", True)
        b.clicked.connect(slot)
        self._machine_buttons.append(b)
        return b

    # ------------------------------------------------------------------ aba Posição e Z
    def _build_position(self) -> QWidget:
        self._machine_buttons: list[QPushButton] = []
        w = QWidget()
        lay = QVBoxLayout(w)
        lay.setContentsMargins(4, 4, 4, 4)
        lay.setSpacing(8)

        lay.addWidget(_section("Ajuste fino da profundidade (offset Z)",
                               "Corrige o zero Z sem sondar de novo: se a isolação ficou rasa (o cobre não foi "
                               "cortado), clique em “mais fundo”; se cortou demais, “mais raso”. Vale para "
                               "tudo que for enviado depois. Use com a máquina parada."))
        zrow = QHBoxLayout()
        zrow.setSpacing(4)
        for d in (-0.1, -0.05, -0.01, 0.01, 0.05, 0.1):
            label = f"{'+' if d > 0 else '−'}{abs(d):g}"
            b = self._btn(label, "z-down" if d > 0 else "z-up",
                          f"{'Mais fundo' if d > 0 else 'Mais raso'} {abs(d):g} mm "
                          f"(desloca o zero Z {'para baixo' if d > 0 else 'para cima'})",
                          lambda _=False, d=d: self._adjust_z(d))
            b.setMinimumWidth(56)
            zrow.addWidget(b)
        lay.addLayout(zrow)
        hint = QHBoxLayout()
        a = QLabel("↑ mais raso")
        a.setObjectName("Muted")
        bb = QLabel("mais fundo ↓")
        bb.setObjectName("Muted")
        hint.addWidget(a)
        hint.addStretch(1)
        self.z_total = QLabel("Ajuste acumulado nesta sessão: 0,000 mm")
        self.z_total.setAccessibleName("Ajuste de Z acumulado")
        hint.addWidget(self.z_total)
        hint.addStretch(1)
        hint.addWidget(bb)
        lay.addLayout(hint)

        lay.addWidget(_section("Definir posição atual",
                               "Diz à máquina quanto vale a posição atual da ferramenta, sem movê-la (G10 L20). "
                               "Ex.: ferramenta encostada no canto da placa → X0 Y0; ponta 1 mm acima de uma "
                               "régua → Z=1."))
        g = QGridLayout()
        g.setHorizontalSpacing(8)
        self.set_cb: dict[str, QCheckBox] = {}
        self.set_val: dict[str, DoubleSpin] = {}
        for r, ax in enumerate("XYZ"):
            cb = QCheckBox(ax)
            cb.setChecked(True)
            cb.setAccessibleName(f"Definir {ax}")
            sp = _spin(-10000, 10000, 0.0, 3, 0.1, f" {units.unit_label()}", f"Valor de {ax}")
            self.set_cb[ax] = cb
            self.set_val[ax] = sp
            g.addWidget(cb, r, 0)
            g.addWidget(sp, r, 1)
        quick = QGridLayout()
        quick.setSpacing(4)
        for i, axes in enumerate(("X", "Y", "Z", "XY", "XYZ")):
            zb = self._btn(f"Zero {axes}", "zero", f"Definir {axes} = 0 na posição atual",
                           lambda _=False, a=axes: self._run(f"zero{a}", "set_work_zero", a))
            zb.setMaximumWidth(140)
            quick.addWidget(zb, i // 3, i % 3)
        g.addLayout(quick, 0, 2, 3, 1)
        g.addWidget(self._btn("Definir", "check", "Aplicar os valores marcados à posição atual", self._set_pos),
                    3, 1)
        g.setColumnStretch(3, 1)
        lay.addLayout(g)

        lay.addWidget(_section("Ir para coordenada",
                               "Move a ferramenta em coordenadas de trabalho. Com “subir antes”, a ferramenta "
                               "sobe até o Z seguro antes de andar em X/Y, para não arrastar na placa."))
        g2 = QGridLayout()
        self.go_cb: dict[str, QCheckBox] = {}
        self.go_val: dict[str, DoubleSpin] = {}
        for r, ax in enumerate("XYZ"):
            cb = QCheckBox(ax)
            cb.setChecked(ax != "Z")
            sp = _spin(-10000, 10000, 0.0 if ax != "Z" else 5.0, 3, 1.0, f" {units.unit_label()}", f"Destino {ax}")
            self.go_cb[ax] = cb
            self.go_val[ax] = sp
            g2.addWidget(cb, r, 0)
            g2.addWidget(sp, r, 1)
        self.safe_cb = QCheckBox("Subir antes para Z seguro")
        self.safe_cb.setChecked(True)
        self.safe_z = _spin(0, 100, 5.0, 2, 1.0, f" {units.unit_label()}", "Z seguro")
        g2.addWidget(self.safe_cb, 0, 2)
        g2.addWidget(self.safe_z, 1, 2)
        g2.addWidget(self._btn("Ir", "goto-zero", "Mover para as coordenadas marcadas", self._goto), 3, 1)
        g2.setColumnStretch(2, 1)
        lay.addLayout(g2)

        lay.addWidget(_section("Sistema de coordenadas, spindle e refrigeração"))
        row = QHBoxLayout()
        self.wcs = Combo()
        for n in range(1, 7):
            self.wcs.addItem(f"G{53 + n}", n)
        self.wcs.setAccessibleName("Sistema de coordenadas de trabalho")
        self.wcs.setToolTip("G54 é o padrão. G55–G59 guardam outros zeros (ex.: duas placas na mesa).")
        row.addWidget(QLabel("Sistema"))
        row.addWidget(self.wcs)
        row.addWidget(self._btn("Ativar", "check", "Passa a usar o sistema escolhido (G54–G59)",
                                lambda: self._run("wcs", "select_wcs", self.wcs.currentData())))
        row.addSpacing(12)
        self.rpm = _spin(0, 60000, 10000, 0, 500, " RPM", "Rotação do spindle")
        row.addWidget(self.rpm)
        row.addWidget(self._btn("Ligar", "spindle", "Liga o spindle (M3) na rotação indicada",
                                lambda: self._run("spindle_on", "spindle", True, self.rpm.value())))
        row.addWidget(self._btn("Desligar", "stop", "Desliga o spindle/laser (M5)",
                                lambda: self._run("spindle_off", "spindle", False)))
        row.addSpacing(12)
        row.addWidget(self._btn("Ar on", "feed", "Liga refrigeração/ar (M8)",
                                lambda: self._run("cool_on", "coolant", True)))
        row.addWidget(self._btn("Ar off", "feed", "Desliga refrigeração/ar (M9)",
                                lambda: self._run("cool_off", "coolant", False)))
        row.addStretch(1)
        lay.addLayout(row)
        lay.addStretch(1)
        return w

    def _adjust_z(self, d: float) -> None:
        self._pending_adjust = d
        self._run("adjust_z", "adjust_work_z", d)

    def _set_pos(self) -> None:
        vals = {ax.lower(): units.from_display(self.set_val[ax].value()) for ax in "XYZ" if self.set_cb[ax].isChecked()}
        if not vals:
            self._say("Marque ao menos um eixo para definir.", error=True)
            return
        self._run("set_position", "set_position", **vals)

    def _goto(self) -> None:
        vals = {ax.lower(): units.from_display(self.go_val[ax].value()) for ax in "XYZ" if self.go_cb[ax].isChecked()}
        if not vals:
            self._say("Marque ao menos um eixo de destino.", error=True)
            return
        safe = units.from_display(self.safe_z.value()) if self.safe_cb.isChecked() else None
        self._run("goto", "goto", safe_z=safe, **vals)

    # ------------------------------------------------------------------ aba Sonda Z
    def _build_probe(self) -> QWidget:
        w = QWidget()
        lay = QVBoxLayout(w)
        lay.setContentsMargins(4, 4, 4, 4)
        lay.setSpacing(8)
        lay.addWidget(_section("Sondar e zerar Z",
                               "Prenda o jacaré na fresa e coloque a placa de toque (ou a própria PCB, se o cobre "
                               "estiver ligado à sonda) sob a ponta. A ferramenta desce devagar até encostar; o Z "
                               "de trabalho passa a ser a espessura da placa de toque nesse ponto."))
        g = QGridLayout()
        g.setHorizontalSpacing(10)
        self.pr_depth = _spin(0.5, 100, 10.0, 2, 1.0, f" {units.unit_label()}", "Descida máxima")
        self.pr_feed = _spin(5, 1000, 50.0, 0, 10, f" {units.unit_label()}/min", "Avanço da sonda")
        self.pr_plate = _spin(0, 50, 0.0, 3, 0.1, f" {units.unit_label()}", "Espessura da placa de toque")
        self.pr_retract = _spin(0, 20, 2.0, 2, 0.5, f" {units.unit_label()}", "Recuo após tocar")
        for r, (lab, sp, tip) in enumerate((
                ("Descida máxima", self.pr_depth, "Quanto a ferramenta pode descer procurando contato antes de "
                 "desistir (ALARM:5). Coloque a ponta a poucos mm da placa."),
                ("Avanço", self.pr_feed, "Velocidade de descida. Devagar (20–60 mm/min) protege fresas finas."),
                ("Placa de toque", self.pr_plate, "Espessura da plaquinha metálica usada como sonda. 0 se sondar "
                 "direto no cobre da PCB."),
                ("Recuo", self.pr_retract, "Quanto subir depois de tocar."))):
            l = QLabel(lab)
            l.setBuddy(sp)
            sp.setToolTip(tip)
            sp.setAccessibleDescription(tip)
            g.addWidget(l, r, 0)
            g.addWidget(sp, r, 1)
        g.setColumnStretch(2, 1)
        lay.addLayout(g)
        row = QHBoxLayout()
        row.addWidget(self._btn("Sondar e zerar Z", "probe", "G38.2 até tocar e define Z de trabalho", self._probe_zero,
                                primary=True))
        row.addWidget(self._btn("Só medir", "measure", "Sonda e mostra a altura, sem mudar o zero",
                                self._probe_measure))
        row.addStretch(1)
        lay.addLayout(row)
        self.probe_result = QLabel()
        self.probe_result.setAccessibleName("Resultado da sondagem")
        lay.addWidget(self.probe_result)
        lay.addStretch(1)
        return w

    def _probe_zero(self) -> None:
        self._run("probe_zero", "probe_z", depth=units.from_display(self.pr_depth.value()),
                  feed=units.from_display(self.pr_feed.value()), plate_thickness=units.from_display(self.pr_plate.value()),
                  retract=units.from_display(self.pr_retract.value()))

    def _probe_measure(self) -> None:
        self._run("probe_measure", "probe_point", depth=units.from_display(self.pr_depth.value()),
                  feed=units.from_display(self.pr_feed.value()))

    # ------------------------------------------------------------------ aba Heightmap
    def _build_heightmap(self) -> QWidget:
        w = QWidget()
        outer = QHBoxLayout(w)
        outer.setContentsMargins(4, 4, 4, 4)
        outer.setSpacing(12)
        left_w = QWidget()
        left = QVBoxLayout(left_w)
        left.setContentsMargins(0, 0, 0, 0)
        left.setSpacing(6)
        intro = QLabel("Mede a superfície real da placa numa grade de pontos e corrige o Z do programa, para a "
                       "fresa em V cortar sempre a mesma profundidade mesmo com a placa empenada. Zere X/Y e Z "
                       "(Sonda Z) antes; o mapa é aplicado relativo ao zero XY.")
        intro.setObjectName("Muted")
        intro.setWordWrap(True)
        left.addWidget(intro)
        g = QGridLayout()
        g.setHorizontalSpacing(8)
        g.setVerticalSpacing(4)
        self.hm_x0 = _spin(-5000, 5000, 0, 2, 1, f" {units.unit_label()}", "X inicial")
        self.hm_y0 = _spin(-5000, 5000, 0, 2, 1, f" {units.unit_label()}", "Y inicial")
        self.hm_x1 = _spin(-5000, 5000, 30, 2, 1, f" {units.unit_label()}", "X final")
        self.hm_y1 = _spin(-5000, 5000, 20, 2, 1, f" {units.unit_label()}", "Y final")
        g.addWidget(QLabel("Área X"), 0, 0)
        g.addWidget(self.hm_x0, 0, 1)
        g.addWidget(self.hm_x1, 0, 2)
        g.addWidget(QLabel("Área Y"), 1, 0)
        g.addWidget(self.hm_y0, 1, 1)
        g.addWidget(self.hm_y1, 1, 2)
        arow = QHBoxLayout()
        b1 = QPushButton("Limites do programa")
        b1.setToolTip("Usa a área percorrida pelo programa carregado no painel Máquina")
        b1.clicked.connect(lambda: self._area_from("program"))
        b2 = QPushButton("Limites da placa")
        b2.setToolTip("Usa a área da placa (contorno) do projeto")
        b2.clicked.connect(lambda: self._area_from("board"))
        arow.addWidget(b1)
        arow.addWidget(b2)
        g.addLayout(arow, 2, 1, 1, 2)
        self.hm_spacing = _spin(2, 100, 10, 1, 1, f" {units.unit_label()}", "Espaçamento entre pontos")
        self.hm_spacing.setToolTip("Distância máxima entre pontos sondados. 8–15 mm é bom para PCB; menos pontos = "
                                   "mais rápido, mais pontos = acompanha melhor ondulações.")
        g.addWidget(QLabel("Espaçamento"), 3, 0)
        g.addWidget(self.hm_spacing, 3, 1)
        self.hm_count = QLabel()
        self.hm_count.setObjectName("Muted")
        g.addWidget(self.hm_count, 3, 2)
        self.hm_clear = _spin(0.5, 20, 2.0, 2, 0.5, f" {units.unit_label()}", "Z seguro entre pontos")
        self.hm_clear.setToolTip("Altura (acima do zero Z) para andar entre pontos.")
        self.hm_depth = _spin(0.5, 20, 3.0, 2, 0.5, f" {units.unit_label()}", "Descida máxima")
        self.hm_depth.setToolTip("Quanto descer abaixo do zero procurando a placa antes de dar erro.")
        self.hm_feed = _spin(5, 1000, 60, 0, 10, f" {units.unit_label()}/min", "Avanço da sonda")
        g.addWidget(QLabel("Z seguro"), 4, 0)
        g.addWidget(self.hm_clear, 4, 1)
        g.addWidget(QLabel("Descida máx."), 5, 0)
        g.addWidget(self.hm_depth, 5, 1)
        g.addWidget(QLabel("Avanço sonda"), 6, 0)
        g.addWidget(self.hm_feed, 6, 1)
        self.hm_segment = _spin(0.2, 10, 1.0, 1, 0.5, f" {units.unit_label()}", "Segmento máximo")
        self.hm_segment.setToolTip("Ao aplicar, movimentos longos são divididos em trechos deste tamanho para "
                                   "acompanhar a superfície.")
        g.addWidget(QLabel("Segmento"), 7, 0)
        g.addWidget(self.hm_segment, 7, 1)
        left.addLayout(g)
        for sp in (self.hm_x0, self.hm_x1, self.hm_y0, self.hm_y1, self.hm_spacing):
            sp.valueChanged.connect(lambda _v: self._update_count())
        prow = QHBoxLayout()
        self.hm_start = self._btn("Iniciar sondagem", "probe", "Sonda todos os pontos da grade", self._start_grid,
                                  primary=True)
        self.hm_cancel = QPushButton("Cancelar")
        self.hm_cancel.setIcon(icon("cancel"))
        self.hm_cancel.clicked.connect(self._cancel_grid)
        self.hm_cancel.setEnabled(False)
        prow.addWidget(self.hm_start)
        prow.addWidget(self.hm_cancel)
        left.addLayout(prow)
        self.hm_progress = QProgressBar()
        self.hm_progress.setTextVisible(True)
        self.hm_progress.setAccessibleName("Progresso da sondagem")
        left.addWidget(self.hm_progress)
        frow = QHBoxLayout()
        for text, ic, slot in (("Salvar mapa…", "save", self._save_map), ("Abrir mapa…", "open", self._load_map)):
            b = QPushButton(text)
            b.setIcon(icon(ic))
            b.clicked.connect(slot)
            frow.addWidget(b)
        left.addLayout(frow)
        arow2 = QHBoxLayout()
        self.hm_apply = QPushButton("Aplicar ao programa")
        self.hm_apply.setIcon(icon("check"))
        self.hm_apply.setToolTip("Corrige o Z do programa carregado no painel Máquina pela superfície medida")
        self.hm_apply.clicked.connect(self._apply)
        self.hm_undo = QPushButton("Remover correção")
        self.hm_undo.setIcon(icon("undo"))
        self.hm_undo.clicked.connect(self._unapply)
        arow2.addWidget(self.hm_apply)
        arow2.addWidget(self.hm_undo)
        left.addLayout(arow2)
        left.addStretch(1)
        sl = self._scroll(left_w)
        sl.setMinimumWidth(380)
        outer.addWidget(sl, 0)
        right = QVBoxLayout()
        self.hm_view = HeightMapView()
        right.addWidget(self.hm_view, 1)
        self.hm_stats = QLabel()
        self.hm_stats.setWordWrap(True)
        self.hm_stats.setTextFormat(Qt.TextFormat.RichText)
        right.addWidget(self.hm_stats)
        outer.addLayout(right, 1)
        self._update_count()
        self._update_hm_ui()
        return w

    def _area_from(self, src: str) -> None:
        b = None
        if src == "program":
            text, _n = self.get_program()
            if text.strip():
                from brokcam.gcode.preview import parse_gcode
                pv = parse_gcode(text)
                if len(pv.feeds):
                    f = pv.feeds.reshape(-1, 3)
                    b = (float(f[:, 0].min()), float(f[:, 1].min()), float(f[:, 0].max()), float(f[:, 1].max()))
            if b is None:
                self._say("Nenhum programa carregado no painel Máquina.", error=True)
                return
        else:
            b = self.get_board_bounds()
            if b is None:
                self._say("O projeto não tem placa aberta.", error=True)
                return
        for sp, v in ((self.hm_x0, b[0]), (self.hm_y0, b[1]), (self.hm_x1, b[2]), (self.hm_y1, b[3])):
            sp.setValue(units.to_display(v))

    def _grid(self) -> tuple[np.ndarray, np.ndarray]:
        x0, y0 = units.from_display(self.hm_x0.value()), units.from_display(self.hm_y0.value())
        x1, y1 = units.from_display(self.hm_x1.value()), units.from_display(self.hm_y1.value())
        nx, ny = grid_counts_for_spacing(x0, y0, x1, y1, units.from_display(self.hm_spacing.value()))
        return grid_axes(x0, y0, x1, y1, nx, ny)

    def _update_count(self) -> None:
        xs, ys = self._grid()
        self.hm_count.setText(f"{len(xs)} × {len(ys)} = {len(xs) * len(ys)} pontos")

    def _start_grid(self) -> None:
        if self._probing:
            return
        xs, ys = self._grid()
        self.hm = HeightMap(xs, ys, np.full((len(ys), len(xs)), np.nan))
        order = probe_order(xs, ys)
        pts = [(float(xs[i]), float(ys[j])) for j, i in order]
        self._probing = True
        self._cancel = False
        self.hm_progress.setRange(0, len(pts))
        self.hm_progress.setValue(0)
        self.hm_view.set_map(self.hm, 0)

        def progress(i: int, n: int, z: float) -> None:
            j, k = order[i - 1]
            self.hm.z[j, k] = z  # type: ignore[union-attr]
            self.bridge.commandResult.emit("tools:hm_progress", (i, n))

        self._run("heightmap", "probe_grid", pts, clearance=units.from_display(self.hm_clear.value()),
                  depth=units.from_display(self.hm_depth.value()), feed=units.from_display(self.hm_feed.value()),
                  progress=progress, cancel=lambda: self._cancel)
        self._update_hm_ui()

    def _cancel_grid(self) -> None:
        self._cancel = True
        self._say("Cancelando após o ponto atual…")

    def _save_map(self) -> None:
        if self.hm is None:
            return
        path, _ = QFileDialog.getSaveFileName(self, "Salvar heightmap", "heightmap.json", "Heightmap (*.json)")
        if path:
            self.hm.save(path)

    def _load_map(self) -> None:
        path, _ = QFileDialog.getOpenFileName(self, "Abrir heightmap", "", "Heightmap (*.json)")
        if not path:
            return
        try:
            self.hm = HeightMap.load(path)
        except (OSError, ValueError, KeyError, json.JSONDecodeError) as exc:
            QMessageBox.warning(self, "Heightmap", f"Não foi possível abrir:\n{exc}")
            return
        self.hm_view.set_map(self.hm)
        self._update_hm_ui()

    def _apply(self) -> None:
        if self.hm is None or not self.hm.complete:
            self._say("Faça (ou abra) uma sondagem completa antes de aplicar.", error=True)
            return
        if self.streaming:
            self._say("Não é possível alterar o programa durante o envio.", error=True)
            return
        text, name = self.get_program()
        if not text.strip():
            self._say("Carregue um programa no painel Máquina (Enviar à máquina) antes de aplicar.", error=True)
            return
        if self._original_program is not None and "(BrokCAM autolevel" in text:
            text, name = self._original_program
        try:
            res = apply_heightmap(text, self.hm, segment=units.from_display(self.hm_segment.value()))
        except ValueError as exc:
            QMessageBox.warning(self, "Autolevel", str(exc))
            return
        self._original_program = (text, name)
        self.programChanged.emit(res.text, f"{name} (autolevel)")
        msg = (f"Autolevel aplicado: correção entre {res.min_correction:+.3f} e {res.max_correction:+.3f} mm; "
               f"{res.lines_in} → {res.lines_out} linhas.")
        if res.warnings:
            msg += " " + " ".join(res.warnings)
        self._say(msg)
        self._update_hm_ui()

    def _unapply(self) -> None:
        if self._original_program is None:
            return
        text, name = self._original_program
        self._original_program = None
        self.programChanged.emit(text, name)
        self._say("Correção removida: programa original restaurado.")
        self._update_hm_ui()

    def _update_hm_ui(self) -> None:
        hm = self.hm
        if hm is None:
            self.hm_stats.setText("")
        else:
            st = hm.stats()
            done = int(np.isfinite(hm.z).sum())
            col = theme.palette().warning if st["range"] > 0.15 else theme.palette().text
            self.hm_stats.setText(
                f"{done}/{hm.z.size} pontos · mín {st['min']:+.3f} · máx {st['max']:+.3f} · "
                f"<span style='color:{col}'><b>variação {st['range']:.3f} mm</b></span>"
                + ("<br>Variação acima de 0,15 mm: confira a fixação da placa." if st["range"] > 0.15 else ""))
        idle = self.connected and not self.streaming and not self._probing
        self.hm_start.setEnabled(idle)
        self.hm_cancel.setEnabled(self._probing)
        self.hm_apply.setEnabled(hm is not None and hm.complete and not self.streaming)
        self.hm_undo.setEnabled(self._original_program is not None and not self.streaming)

    # ------------------------------------------------------------------ aba Fins de curso e homing
    def _build_limits(self) -> QWidget:
        w = QWidget()
        lay = QVBoxLayout(w)
        lay.setContentsMargins(4, 4, 4, 4)
        lay.setSpacing(8)
        lay.addWidget(_section("Teste das chaves (ao vivo)",
                               "Com a máquina PARADA longe das chaves, nenhum indicador deve estar aceso. Depois "
                               "aperte cada chave com a mão: o indicador do eixo correspondente deve acender. Só "
                               "faça o homing quando esse teste passar."))
        row = QHBoxLayout()
        self.pin_chips: dict[str, QLabel] = {}
        for key, label in (("X", "Fim de curso X"), ("Y", "Fim de curso Y"), ("Z", "Fim de curso Z"),
                           ("P", "Sonda")):
            chip = QLabel()
            chip.setAlignment(Qt.AlignmentFlag.AlignCenter)
            chip.setMinimumSize(130, 44)
            chip.setAccessibleName(label)
            self.pin_chips[key] = chip
            row.addWidget(chip)
        row.addStretch(1)
        lay.addLayout(row)
        self.limit_diag = QLabel()
        self.limit_diag.setWordWrap(True)
        self.limit_diag.setTextFormat(Qt.TextFormat.RichText)
        self.limit_diag.setAccessibleName("Diagnóstico dos fins de curso")
        lay.addWidget(self.limit_diag)

        lay.addWidget(_section("Configuração de limites e homing na placa"))
        self.limit_cfg = QLabel("Clique em “Ler da máquina”.")
        self.limit_cfg.setWordWrap(True)
        self.limit_cfg.setTextFormat(Qt.TextFormat.RichText)
        lay.addWidget(self.limit_cfg)
        brow = QHBoxLayout()
        brow.addWidget(self._btn("Ler da máquina", "refresh", "Lê $5, $21, $22, $23… da placa", self._read_limits))
        self.inv_btn = self._btn("Inverter fins de curso ($5)", "swap",
                                 "Troca a lógica das chaves: NF (normalmente fechadas) precisam de $5=1; NA "
                                 "(normalmente abertas) de $5=0", self._toggle_invert)
        brow.addWidget(self.inv_btn)
        self.hard_btn = self._btn("Limite físico ($21)", "warning",
                                  "Liga/desliga a parada imediata da placa quando uma chave é acionada",
                                  self._toggle_hard)
        brow.addWidget(self.hard_btn)
        brow.addStretch(1)
        lay.addLayout(brow)
        hrow = QHBoxLayout()
        hrow.addWidget(self._btn("Testar homing ($H)", "home", "Faz o homing — só depois que o teste das chaves "
                                 "passar. Mantenha a mão no botão de emergência.", self._test_home, primary=True))
        hrow.addWidget(self._btn("Destravar ($X)", "unlock", "Sai do estado de alarme",
                                 lambda: self._run("unlock", "unlock")))
        hrow.addStretch(1)
        lay.addLayout(hrow)
        tips = QLabel(
            "<b>Se o homing vai para o lado errado</b>, inverta o eixo em $23 (ou $Homing/DirInvert na DLC32). "
            "<b>Se o motor estala sem andar</b>, a velocidade de busca $25 está alta para o motor — tente 300–500 "
            "mm/min — ou a chave já está acionada (veja o teste acima). <b>Se a máquina bate na chave sem parar</b>, "
            "a placa não está lendo a chave: faça o teste ao vivo e corrija $5 ou a ligação.")
        tips.setObjectName("Muted")
        tips.setWordWrap(True)
        tips.setTextFormat(Qt.TextFormat.RichText)
        lay.addWidget(tips)
        lay.addStretch(1)
        self._limit_settings: dict[int, float] = {}
        self._pins = ""
        self._state = ""
        self._update_pin_chips()
        return w

    def _update_pin_chips(self) -> None:
        pal = theme.palette()
        for key, chip in self.pin_chips.items():
            on = key in self._pins
            name = "Sonda" if key == "P" else f"Fim de curso {key}"
            col = pal.danger if (on and key != "P") else pal.warning if on else pal.success
            if not self.connected:
                col = pal.text_disabled
            txt = "—" if not self.connected else ("ACIONADA" if on else "solta")
            chip.setText(f"<b>{name}</b><br>{txt}")
            chip.setStyleSheet(f"QLabel {{ border: 2px solid {col}; border-radius: 8px; color: {col}; padding: 4px; }}")
            chip.setAccessibleDescription(f"{name}: {txt}")
        axes = "".join(a for a in "XYZ" if a in self._pins)
        if not self.connected:
            self.limit_diag.setText("")
        elif axes and self._state.split(":")[0] in ("Idle", "Alarm"):
            inv = self._limit_settings.get(5)
            fix = (" Hoje $5=%g." % inv) if inv is not None else ""
            self.limit_diag.setText(
                f"<span style='color:{pal.danger}'><b>⚠ {', '.join(axes)} aparecem ACIONADAS com a máquina parada.</b></span> "
                "Se a ferramenta não está encostada nessas chaves, a lógica está invertida — típico de chaves NF "
                f"(normalmente fechadas). Use <b>Inverter fins de curso ($5)</b> e repita o teste.{fix} Até "
                "corrigir, o homing fica bloqueado e o limite físico não protege a máquina.")
        else:
            self.limit_diag.setText(f"<span style='color:{pal.success}'>Nenhuma chave acionada em repouso — aperte "
                                    "cada chave com a mão e veja o indicador acender.</span>")

    def _read_limits(self) -> None:
        self._run("read_limits", "read_settings")

    def _show_limit_cfg(self, s: dict[int, float]) -> None:
        self._limit_settings = dict(s)

        def v(n: int) -> str:
            return f"{s[n]:g}" if n in s else "—"

        def onoff(n: int) -> str:
            return "ligado" if s.get(n) else "desligado"
        mask = int(s.get(23, 0))
        inv_axes = "".join(a for i, a in enumerate("XYZ") if mask & (1 << i)) or "nenhum"
        self.limit_cfg.setText(
            f"$5 inverter fins de curso = <b>{v(5)}</b> · $21 limite físico = <b>{onoff(21)}</b> · "
            f"$22 homing = <b>{onoff(22)}</b> · $20 limite por software = <b>{onoff(20)}</b><br>"
            f"$23 direção do homing invertida em: <b>{inv_axes}</b> · $24 avanço fino = <b>{v(24)}</b> mm/min · "
            f"$25 busca = <b>{v(25)}</b> mm/min · $26 debounce = <b>{v(26)}</b> ms · $27 recuo = <b>{v(27)}</b> mm")
        self.inv_btn.setText(f"Inverter fins de curso ($5 → {0 if s.get(5) else 1})")
        self.hard_btn.setText(f"{'Desligar' if s.get(21) else 'Ligar'} limite físico ($21 → {0 if s.get(21) else 1})")
        self._update_pin_chips()

    def _toggle_invert(self) -> None:
        cur = self._limit_settings.get(5)
        if cur is None:
            self._say("Leia a configuração da máquina primeiro.", error=True)
            return
        new = 0 if cur else 1
        if QMessageBox.question(
                self, "Inverter fins de curso",
                f"Gravar $5={new} na placa?\n\nDepois disso, com a máquina parada, nenhum fim de curso deve aparecer "
                "acionado, e cada chave deve acender só quando apertada. Se ficar pior, grave de volta.") \
                != QMessageBox.StandardButton.Yes:
            return
        self._run("write_limits", "write_setting", 5, new)

    def _toggle_hard(self) -> None:
        cur = self._limit_settings.get(21)
        if cur is None:
            self._say("Leia a configuração da máquina primeiro.", error=True)
            return
        new = 0 if cur else 1
        warn = ("" if new else "\n\nATENÇÃO: sem o limite físico, nada impede a máquina de forçar as chaves.")
        if QMessageBox.question(self, "Limite físico", f"Gravar $21={new} na placa?{warn}") \
                != QMessageBox.StandardButton.Yes:
            return
        self._run("write_limits", "write_setting", 21, new)

    def _test_home(self) -> None:
        if QMessageBox.question(
                self, "Testar homing",
                "A máquina vai se mover até os fins de curso. Mantenha a mão no botão de emergência e acompanhe. "
                "Continuar?") != QMessageBox.StandardButton.Yes:
            return
        self._run("home", "home")

    # ------------------------------------------------------------------ aba Macros
    def _build_macros(self) -> QWidget:
        w = QWidget()
        lay = QHBoxLayout(w)
        lay.setContentsMargins(4, 4, 4, 4)
        lay.setSpacing(10)
        left = QVBoxLayout()
        self.macro_list = QListWidget()
        self.macro_list.setAccessibleName("Macros")
        self.macro_list.currentRowChanged.connect(self._show_macro)
        self.macro_list.itemDoubleClicked.connect(lambda _it: self._run_macro())
        left.addWidget(self.macro_list, 1)
        brow = QHBoxLayout()
        for text, ic, slot in (("Nova", "plus", self._new_macro), ("Excluir", "trash", self._del_macro)):
            b = QPushButton(text)
            b.setIcon(icon(ic))
            b.clicked.connect(slot)
            brow.addWidget(b)
        left.addLayout(brow)
        lay.addLayout(left, 2)
        right = QVBoxLayout()
        note = QLabel("Sequências de comandos G-code do seu dia a dia. Cada linha é enviada e confirmada antes da "
                      "próxima. Duplo clique na lista executa.")
        note.setObjectName("Muted")
        note.setWordWrap(True)
        right.addWidget(note)
        self.macro_edit = QPlainTextEdit()
        self.macro_edit.setFont(theme.mono_font())
        self.macro_edit.setAccessibleName("G-code da macro")
        self.macro_edit.textChanged.connect(self._macro_edited)
        right.addWidget(self.macro_edit, 1)
        rrow = QHBoxLayout()
        rrow.addWidget(self._btn("Executar macro", "play", "Envia as linhas da macro à máquina", self._run_macro,
                                 primary=True))
        ren = QPushButton("Renomear…")
        ren.clicked.connect(self._rename_macro)
        rrow.addWidget(ren)
        rrow.addStretch(1)
        right.addLayout(rrow)
        lay.addLayout(right, 3)
        self.macros: list[dict[str, str]] = list(self.settings.get("machine_macros") or DEFAULT_MACROS)
        self._fill_macros()
        return w

    def _fill_macros(self, select: int = 0) -> None:
        self.macro_list.blockSignals(True)
        self.macro_list.clear()
        for m in self.macros:
            it = QListWidgetItem(icon("terminal"), m["name"])
            it.setToolTip(m["gcode"])
            self.macro_list.addItem(it)
        self.macro_list.blockSignals(False)
        if self.macros:
            self.macro_list.setCurrentRow(min(select, len(self.macros) - 1))
            self._show_macro(self.macro_list.currentRow())

    def _save_macros(self) -> None:
        self.settings.set("machine_macros", self.macros)
        self.settings.save()

    def _show_macro(self, row: int) -> None:
        self.macro_edit.blockSignals(True)
        self.macro_edit.setPlainText(self.macros[row]["gcode"] if 0 <= row < len(self.macros) else "")
        self.macro_edit.blockSignals(False)

    def _macro_edited(self) -> None:
        row = self.macro_list.currentRow()
        if 0 <= row < len(self.macros):
            self.macros[row]["gcode"] = self.macro_edit.toPlainText()
            self.macro_list.item(row).setToolTip(self.macros[row]["gcode"])
            self._save_macros()

    def _new_macro(self) -> None:
        name, ok = QInputDialog.getText(self, "Nova macro", "Nome da macro:")
        if ok and name.strip():
            self.macros.append({"name": name.strip(), "gcode": ""})
            self._save_macros()
            self._fill_macros(len(self.macros) - 1)
            self.macro_edit.setFocus()

    def _rename_macro(self) -> None:
        row = self.macro_list.currentRow()
        if 0 <= row < len(self.macros):
            name, ok = QInputDialog.getText(self, "Renomear macro", "Nome:", text=self.macros[row]["name"])
            if ok and name.strip():
                self.macros[row]["name"] = name.strip()
                self._save_macros()
                self._fill_macros(row)

    def _del_macro(self) -> None:
        row = self.macro_list.currentRow()
        if 0 <= row < len(self.macros):
            if QMessageBox.question(self, "Excluir macro", f"Excluir “{self.macros[row]['name']}”?") \
                    == QMessageBox.StandardButton.Yes:
                del self.macros[row]
                self._save_macros()
                self._fill_macros(row)

    def _run_macro(self) -> None:
        row = self.macro_list.currentRow()
        if 0 <= row < len(self.macros):
            self._run(f"macro:{self.macros[row]['name']}", "run_lines", self.macros[row]["gcode"])

    # ------------------------------------------------------------------ eventos do bridge
    def _on_connection(self, ok: bool) -> None:
        self.connected = ok
        if not ok:
            self._probing = False
            self._pins = ""
        self._update_enabled()
        self._update_pin_chips()
        if ok:
            self._read_limits()

    def _on_streaming(self, on: bool) -> None:
        self.streaming = on
        self._update_enabled()

    def _on_status(self, st: dict) -> None:
        w = st.get("wpos")
        if w:
            self.wpos = tuple(w)[:3]  # type: ignore[assignment]
        pins = str(st.get("pins") or "")
        state = str(st.get("state") or "")
        if pins != self._pins or state.split(":")[0] != self._state.split(":")[0]:
            self._pins, self._state = pins, state
            self._update_pin_chips()

    def _on_result(self, name: str, result: Any) -> None:
        if not name.startswith("tools:"):
            return
        name = name[6:]
        if name == "hm_progress":
            i, n = result
            self.hm_progress.setValue(i)
            self.hm_view.set_map(self.hm, i)
            self._update_hm_ui()
            return
        if name == "adjust_z":
            self.z_adjust_total += getattr(self, "_pending_adjust", 0.0)
            self.z_total.setText(f"Ajuste acumulado nesta sessão: {self.z_adjust_total:+.3f} mm "
                                 f"({'mais fundo' if self.z_adjust_total > 0 else 'mais raso' if self.z_adjust_total < 0 else 'nenhum'})")
            self._say(f"Zero Z ajustado em {self._pending_adjust:+.3f} mm.")
        elif name == "heightmap":
            self._probing = False
            self.hm_view.set_map(self.hm)
            self._say("Sondagem concluída. Confira o mapa e clique em “Aplicar ao programa”.")
        elif name == "probe_zero":
            self.probe_result.setText(f"Contato no Z de máquina {float(result):.3f} mm — zero Z definido.")
            self._say("Z zerado pela sonda.")
        elif name == "read_limits":
            self._show_limit_cfg(result or {})
            self._say("Configuração de limites lida.")
        elif name == "write_limits":
            self._say("Configuração gravada. Refaça o teste das chaves.")
            self._read_limits()
        elif name == "home":
            self._say("Homing concluído.")
        elif name == "probe_measure":
            self.probe_result.setText(f"Superfície em Z de trabalho {float(result):+.3f} mm (zero não alterado).")
        else:
            self._say(f"Comando concluído: {name.replace('_', ' ')}.")
        self._update_enabled()

    def _on_failed(self, name: str, msg: str) -> None:
        if not name.startswith("tools:"):
            return
        if name == "tools:heightmap":
            self._probing = False
            self.hm_view.set_map(self.hm)
        self._say(f"Falhou: {msg}", error=True)
        self._update_enabled()

    def _update_enabled(self) -> None:
        idle = self.connected and not self.streaming and not self._probing
        for b in self._machine_buttons:
            b.setEnabled(idle)
        if not self.connected:
            self._say("Desconectado — conecte-se à máquina no painel Máquina para usar estes comandos "
                      "(ou use a porta SIMULADOR para testar).")
        elif self.streaming:
            self._say("Programa em execução: os comandos ficam bloqueados até o fim do envio.")
        self._update_hm_ui()
