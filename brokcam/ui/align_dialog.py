"""Diálogo "Posicionar placa": leva a placa inteira para o zero de trabalho.

Move rigidamente todas as camadas e caminhos juntos (o registro entre cobre, furos e
contorno nunca se perde). O usuário escolhe qual ponto da placa (canto, centro…) vai
para qual coordenada, uma margem e um giro em passos de 90° ou livre.
"""
from __future__ import annotations

from PySide6.QtCore import QPointF, QRectF, Qt
from PySide6.QtGui import QColor, QPainter, QPen
from PySide6.QtWidgets import (
    QButtonGroup, QDialog, QDialogButtonBox, QFrame, QGridLayout, QHBoxLayout, QLabel, QPushButton,
    QSizePolicy, QVBoxLayout, QWidget,
)

from . import theme, units
from .param_form import Combo, DoubleSpin, HelpPanel
from .project import ANCHORS, Project, alignment_delta

ANCHOR_LABELS = {
    "tl": "Canto superior esquerdo", "tc": "Meio da borda superior", "tr": "Canto superior direito",
    "ml": "Meio da borda esquerda", "c": "Centro da placa", "mr": "Meio da borda direita",
    "bl": "Canto inferior esquerdo", "bc": "Meio da borda inferior", "br": "Canto inferior direito",
}

HELP = {
    "anchor": ("Ponto de referência",
               "Qual ponto da placa vai para a coordenada de destino. O mais comum é o canto inferior "
               "esquerdo: zere a máquina (X0 Y0) no canto da placa virgem presa na mesa e a placa inteira "
               "fica em X/Y positivos. Use o centro se você costuma zerar no meio da placa."),
    "target": ("Coordenada de destino",
               "Onde o ponto de referência deve ficar no sistema de trabalho (G54). Normalmente 0,0 — o "
               "ponto onde você zera a máquina. Valores diferentes permitem aproveitar uma sobra da "
               "placa virgem sem precisar mudar o zero."),
    "margin": ("Margem",
               "Distância extra somada ao destino para afastar a placa do zero. Use pelo menos o raio da "
               "fresa de recorte (ex.: 1 mm para fresa de 2 mm) para que o recorte não passe da borda da "
               "placa virgem nem fique em coordenada negativa."),
    "rotation": ("Girar placa",
                 "Gira a placa inteira antes de posicionar. 90° é útil quando a placa é mais comprida que o "
                 "curso de um eixo ou para alinhar com o lado maior da placa virgem. Camadas, furos e "
                 "caminhos giram juntos; o sentido de fresamento é preservado."),
    "reference": ("Medir a placa por",
                  "Contorno: usa os limites da camada de contorno (Edge.Cuts) — a medida real da placa. "
                  "Todas as camadas: usa tudo que está no projeto; necessário se não houver contorno."),
}


class _Preview(QWidget):
    """Mini desenho: área positiva da máquina, placa antes (tracejada) e depois."""

    def __init__(self) -> None:
        super().__init__()
        self.before: tuple[float, float, float, float] | None = None
        self.after: tuple[float, float, float, float] | None = None
        self.anchor = (0.0, 0.0)
        self.setMinimumSize(260, 200)
        self.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Expanding)
        self.setAccessibleName("Pré-visualização do posicionamento")

    def set_boxes(self, before, after, anchor_pt) -> None:  # type: ignore[no-untyped-def]
        self.before, self.after, self.anchor = before, after, anchor_pt
        self.update()

    def paintEvent(self, _ev) -> None:  # type: ignore[no-untyped-def]  # noqa: N802
        pal = theme.palette()
        p = QPainter(self)
        p.setRenderHint(QPainter.RenderHint.Antialiasing)
        p.fillRect(self.rect(), QColor(pal.canvas_bg))
        boxes = [b for b in (self.before, self.after) if b]
        xs = [0.0] + [v for b in boxes for v in (b[0], b[2])]
        ys = [0.0] + [v for b in boxes for v in (b[1], b[3])]
        x0, x1, y0, y1 = min(xs), max(xs), min(ys), max(ys)
        w, h = max(x1 - x0, 1e-6), max(y1 - y0, 1e-6)
        pad = 22.0
        s = min((self.width() - 2 * pad) / w, (self.height() - 2 * pad) / h)

        def m(x: float, y: float) -> QPointF:
            return QPointF(pad + (x - x0) * s, self.height() - pad - (y - y0) * s)

        # quadrante positivo (área de trabalho)
        o = m(0, 0)
        pos = QRectF(o, QPointF(self.width(), 0)).normalized()
        fill = QColor(pal.success)
        fill.setAlphaF(0.07)
        p.fillRect(pos, fill)
        ax = QColor(pal.axis_x)
        ay = QColor(pal.axis_y)
        p.setPen(QPen(ax, 1.5))
        p.drawLine(QPointF(0, o.y()), QPointF(self.width(), o.y()))
        p.setPen(QPen(ay, 1.5))
        p.drawLine(QPointF(o.x(), 0), QPointF(o.x(), self.height()))
        p.setPen(QColor(pal.text_muted))
        p.drawText(o + QPointF(4, -4), "0,0")

        def rect(b) -> QRectF:  # type: ignore[no-untyped-def]
            return QRectF(m(b[0], b[3]), m(b[2], b[1]))

        if self.before:
            p.setPen(QPen(QColor(pal.text_muted), 1.2, Qt.PenStyle.DashLine))
            p.setBrush(Qt.BrushStyle.NoBrush)
            p.drawRect(rect(self.before))
        if self.after:
            c = QColor(pal.accent)
            f = QColor(c)
            f.setAlphaF(0.18)
            p.setPen(QPen(c, 2))
            p.setBrush(f)
            p.drawRect(rect(self.after))
            p.setBrush(c)
            p.setPen(Qt.PenStyle.NoPen)
            p.drawEllipse(m(*self.anchor), 4, 4)
        p.end()


class AlignDialog(QDialog):
    """Escolhe giro + translação. Resultado em ``rotation``, ``dx``, ``dy``."""

    def __init__(self, project: Project, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setWindowTitle("Posicionar placa no zero de trabalho")
        self.setAccessibleName("Posicionar placa no zero de trabalho")
        self.project = project
        self.rotation = 0.0
        self.dx = 0.0
        self.dy = 0.0

        root = QVBoxLayout(self)
        root.setContentsMargins(16, 16, 16, 16)
        root.setSpacing(12)
        intro = QLabel("Move a placa inteira — cobre, furos, contorno e caminhos já gerados — para perto do "
                       "zero da máquina. Tudo se move junto, então o alinhamento entre as camadas é mantido.")
        intro.setWordWrap(True)
        intro.setObjectName("Muted")
        root.addWidget(intro)

        body = QHBoxLayout()
        body.setSpacing(16)
        root.addLayout(body, 1)
        form = QGridLayout()
        form.setHorizontalSpacing(10)
        form.setVerticalSpacing(10)
        body.addLayout(form, 3)
        self.help = HelpPanel()

        r = 0
        lab = QLabel("&Medir a placa por")
        self.reference = Combo()
        self.reference.addItem("Contorno da placa", "outline")
        self.reference.addItem("Todas as camadas", "all")
        if not project.layers_by_role("outline"):
            self.reference.setCurrentIndex(1)
        lab.setBuddy(self.reference)
        self._bind(self.reference, "reference")
        form.addWidget(lab, r, 0)
        form.addWidget(self.reference, r, 1)
        r += 1

        lab = QLabel("Ponto de &referência")
        form.addWidget(lab, r, 0, Qt.AlignmentFlag.AlignTop)
        grid_host = QFrame()
        grid_host.setObjectName("Card")
        g = QGridLayout(grid_host)
        g.setContentsMargins(8, 8, 8, 8)
        g.setSpacing(4)
        self.anchor_group = QButtonGroup(self)
        self.anchor_group.setExclusive(True)
        layout_keys = [["tl", "tc", "tr"], ["ml", "c", "mr"], ["bl", "bc", "br"]]
        glyph = {"tl": "◤", "tc": "▲", "tr": "◥", "ml": "◀", "c": "●", "mr": "▶", "bl": "◣", "bc": "▼", "br": "◢"}
        self._anchor_keys: list[str] = []
        for i, row in enumerate(layout_keys):
            for j, key in enumerate(row):
                b = QPushButton(glyph[key])
                b.setCheckable(True)
                b.setFixedSize(40, 32)
                b.setToolTip(ANCHOR_LABELS[key])
                b.setAccessibleName(ANCHOR_LABELS[key])
                self.anchor_group.addButton(b, len(self._anchor_keys))
                self._anchor_keys.append(key)
                g.addWidget(b, i, j)
                b.installEventFilter(self)
                b.setProperty("help_key", "anchor")
        self.anchor_group.button(self._anchor_keys.index("bl")).setChecked(True)
        self.anchor_group.idToggled.connect(lambda _i, on: on and self._update())
        self.anchor_label = QLabel()
        self.anchor_label.setObjectName("Muted")
        g.addWidget(self.anchor_label, 3, 0, 1, 3)
        lab.setBuddy(self.anchor_group.button(0))
        form.addWidget(grid_host, r, 1)
        r += 1

        lab = QLabel("&Destino")
        self.tx = DoubleSpin()
        self.ty = DoubleSpin()
        tl = QHBoxLayout()
        for sp, ax in ((self.tx, "X"), (self.ty, "Y")):
            sp.setRange(-10000, 10000)
            sp.setDecimals(3)
            sp.setPrefix(f"{ax} ")
            sp.setSuffix(f" {units.unit_label()}")
            sp.setAccessibleName(f"Destino em {ax}")
            self._bind(sp, "target")
            tl.addWidget(sp)
        lab.setBuddy(self.tx)
        form.addWidget(lab, r, 0)
        form.addLayout(tl, r, 1)
        r += 1

        lab = QLabel("M&argem")
        self.margin = DoubleSpin()
        self.margin.setRange(0, 1000)
        self.margin.setDecimals(2)
        self.margin.setSingleStep(0.5)
        self.margin.setSuffix(f" {units.unit_label()}")
        self.margin.setAccessibleName("Margem")
        self._bind(self.margin, "margin")
        lab.setBuddy(self.margin)
        form.addWidget(lab, r, 0)
        form.addWidget(self.margin, r, 1)
        r += 1

        lab = QLabel("&Girar placa")
        self.rot = Combo()
        for deg, txt in ((0, "Não girar"), (90, "90° anti-horário"), (180, "180°"), (270, "90° horário")):
            self.rot.addItem(txt, deg)
        self.rot.setAccessibleName("Girar placa")
        self._bind(self.rot, "rotation")
        lab.setBuddy(self.rot)
        form.addWidget(lab, r, 0)
        form.addWidget(self.rot, r, 1)
        r += 1

        self.info = QLabel()
        self.info.setWordWrap(True)
        self.info.setTextFormat(Qt.TextFormat.RichText)
        form.addWidget(self.info, r, 0, 1, 2)
        r += 1
        form.setRowStretch(r, 1)
        form.setColumnStretch(1, 1)

        right = QVBoxLayout()
        self.preview = _Preview()
        right.addWidget(self.preview, 1)
        right.addWidget(self.help)
        body.addLayout(right, 4)

        bb = QDialogButtonBox(QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel)
        bb.button(QDialogButtonBox.StandardButton.Ok).setText("&Posicionar")
        bb.button(QDialogButtonBox.StandardButton.Ok).setProperty("primary", True)
        bb.accepted.connect(self.accept)
        bb.rejected.connect(self.reject)
        self.ok_btn = bb.button(QDialogButtonBox.StandardButton.Ok)
        root.addWidget(bb)

        for w in (self.reference, self.rot):
            w.currentIndexChanged.connect(lambda _i: self._update())
        for w in (self.tx, self.ty, self.margin):
            w.valueChanged.connect(lambda _v: self._update())
        self.help.set_default("Posicionar placa", "Passe o mouse ou dê foco a um campo para ver a explicação.")
        self.resize(820, 520)
        self._update()

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
                title, text = HELP[key]
                self.help.set_default(title, text)
                self.help.show_default()
        return super().eventFilter(obj, ev)

    # ------------------------------------------------------------------ cálculo
    def anchor_key(self) -> str:
        return self._anchor_keys[max(0, self.anchor_group.checkedId())]

    def _update(self) -> None:
        b = self.project.board_bounds(self.reference.currentData())
        key = self.anchor_key()
        self.anchor_label.setText(ANCHOR_LABELS[key])
        if b is None:
            self.info.setText("O projeto está vazio.")
            self.ok_btn.setEnabled(False)
            return
        m = units.from_display(self.margin.value())
        tx = units.from_display(self.tx.value())
        ty = units.from_display(self.ty.value())
        fx, fy = ANCHORS[key]
        # a margem afasta a placa do destino, para dentro do quadrante da placa
        tgt = (tx + (m if fx < 0.5 else -m if fx > 0.5 else 0.0),
               ty + (m if fy < 0.5 else -m if fy > 0.5 else 0.0))
        rot = float(self.rot.currentData() or 0)
        dx, dy, nb = alignment_delta(b, rot, key, tgt)
        self.rotation, self.dx, self.dy = rot, dx, dy
        ax = nb[0] + fx * (nb[2] - nb[0])
        ay = nb[1] + fy * (nb[3] - nb[1])
        self.preview.set_boxes(b, nb, (ax, ay))
        f = lambda v: units.fmt_len(v, 2, False)  # noqa: E731
        size = f"{f(nb[2] - nb[0])} × {f(nb[3] - nb[1])} {units.unit_label()}"
        txt = (f"Placa: <b>{size}</b><br>Hoje: X {f(b[0])}…{f(b[2])} · Y {f(b[1])}…{f(b[3])}<br>"
               f"Depois: X <b>{f(nb[0])}…{f(nb[2])}</b> · Y <b>{f(nb[1])}…{f(nb[3])}</b>")
        if nb[0] < -1e-6 or nb[1] < -1e-6:
            txt += (f"<br><span style='color:{theme.palette().warning}'>⚠ Parte da placa fica em coordenada "
                    "negativa. Se a máquina usa limites/homing com área positiva, prefira o canto inferior "
                    "esquerdo.</span>")
        self.info.setText(txt)
        moved = abs(dx) > 1e-9 or abs(dy) > 1e-9 or rot
        self.ok_btn.setEnabled(bool(moved))
