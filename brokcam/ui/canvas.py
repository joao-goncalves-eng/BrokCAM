"""Canvas 2D de alto desempenho para camadas de PCB e caminhos de ferramenta.

* ``QGraphicsView`` com viewport ``QOpenGLWidget`` opcional (fallback raster).
* Cada camada é UM item com um ``QPainterPath`` em cache, construído de forma
  vetorizada (:mod:`brokcam.ui.qpaths`); canetas cosméticas (largura em pixels).
* Cena em milímetros com Y para cima (a transformação da vista inverte o eixo).
* Zoom no cursor (roda), pan com botão do meio ou Espaço+arrastar, ajustar à tela,
  grade adaptativa 1-2-5, réguas, origem/eixos, régua de medição, seleção por clique.
* Caminhos de ferramenta (cortes contínuos, deslocamentos tracejados), simulação
  animada e posição da ferramenta ao vivo durante o envio ao GRBL.
"""
from __future__ import annotations

import math
import os
from typing import Any

import numpy as np
from PySide6.QtCore import QLineF, QPointF, QRectF, QSize, Qt, QTimer, Signal
from PySide6.QtGui import (
    QBrush, QColor, QFont, QFontMetrics, QKeyEvent, QLinearGradient, QMouseEvent, QPainter, QPainterPath,
    QPainterPathStroker, QPen, QSurfaceFormat, QTransform, QWheelEvent,
)
from PySide6.QtWidgets import (
    QComboBox, QFrame, QGraphicsItem, QGraphicsScene, QGraphicsView, QHBoxLayout, QLabel,
    QSlider, QStyleOptionGraphicsItem, QToolButton, QWidget,
)

from brokcam.core.model import DrillLayer, GeometryLayer, GerberLayer, Job

from . import qpaths, theme, units
from .icons import icon

SCENE_EXTENT = 20000.0       # mm, metade do lado da cena
MIN_SCALE = 0.02             # px/mm
MAX_SCALE = 5000.0
RULER = 22                   # px
PROGRAM_KEY = -2             # chave do item de pré-visualização de G-code (clicável)


def _nice_step(target: float) -> float:
    """Menor valor 1-2-5 × 10^k maior ou igual a ``target``."""
    if target <= 0:
        return 1.0
    exp = math.floor(math.log10(target))
    base = 10 ** exp
    for m in (1, 2, 5, 10):
        if m * base >= target - 1e-12:
            return m * base
    return 10 * base


def _cosmetic(color: QColor, width: float = 1.0, style: Qt.PenStyle = Qt.PenStyle.SolidLine) -> QPen:
    pen = QPen(color, width, style)
    pen.setCosmetic(True)
    pen.setCapStyle(Qt.PenCapStyle.RoundCap)
    pen.setJoinStyle(Qt.PenJoinStyle.RoundJoin)
    return pen


# =========================================================================== furos por diâmetro

class DrillGroup:
    """Furos de um mesmo diâmetro: um ``QPainterPath`` para os furos e um para as cruzes.

    ``rank`` = posição do diâmetro na ordem crescente (define a cor, ver
    :func:`theme.drill_color`). Um caminho por grupo (não por furo) mantém o desenho rápido.
    """

    __slots__ = ("rank", "dia", "fill", "marks", "count")

    def __init__(self, rank: int, dia: float, fill: QPainterPath, marks: QPainterPath, count: int) -> None:
        self.rank = rank
        self.dia = dia
        self.fill = fill
        self.marks = marks
        self.count = count


def drill_layer_groups(layer: DrillLayer, segments: int = 32, cross: float = 0.9) -> list[DrillGroup]:
    """Agrupa os furos/slots de uma camada Excellon por diâmetro (crescente).

    Cada furo é desenhado no diâmetro real e a cruz central tem ``cross`` × o diâmetro do
    próprio grupo (furos grandes, cruz grande; furos finos, cruz fina).
    """
    import shapely
    by: dict[float, list[Any]] = {}
    for tool in layer.tools.values():
        by.setdefault(round(float(tool.diameter), 4), []).append(tool)
    out: list[DrillGroup] = []
    for rank, dia in enumerate(sorted(by)):
        pts = [np.asarray(t.points, dtype=float).reshape(-1, 2) for t in by[dia]]
        pts = [p for p in pts if len(p)]
        slots = [np.asarray(t.slots, dtype=float).reshape(-1, 4) for t in by[dia]]
        slots = [s for s in slots if len(s)]
        fill = QPainterPath()
        marks = QPainterPath()
        n = 0
        if pts:
            c = np.vstack(pts)
            n += len(c)
            fill = qpaths.circles_to_path(c, dia / 2.0, max(8, segments))
            marks = qpaths.crosses_to_path(c, dia * cross)
        if slots:
            sl = np.vstack(slots)
            n += len(sl)
            geom = shapely.union_all(shapely.buffer(shapely.linestrings(sl.reshape(-1, 2, 2)), dia / 2.0,
                                                    quad_segs=8))
            fill.addPath(qpaths.geometry_to_path(geom))
        if n:
            out.append(DrillGroup(rank, dia, fill, marks, n))
    return out


def _paint_drill_groups(painter: QPainter, groups: list[DrillGroup], selected: bool, scale: float,
                        highlight: int | None, fill_alpha: float, cross_mult: float = 1.0) -> None:
    """Desenha grupos de furos com a cor categórica de cada diâmetro.

    Cada furo ganha um anel de contraste (escuro no tema escuro, claro no claro) por baixo do
    contorno colorido, para não sumir sobre um pad da mesma cor. Com um diâmetro destacado
    (tabela do inspetor), os demais caem para ~20% de opacidade e o destacado ganha contorno
    grosso — o destaque não depende só da cor.
    """
    pal = theme.palette()
    halo_c = QColor("#000000" if pal.dark else "#ffffff")
    # desenha os apagados primeiro: o destacado fica por cima
    hl = set(highlight) if isinstance(highlight, (list, tuple, set)) else (
        {highlight} if highlight is not None else set())
    for g in sorted(groups, key=lambda g: g.rank in hl):
        c = QColor(theme.drill_color(g.rank))
        dim = bool(hl) and g.rank not in hl
        hot = bool(hl) and g.rank in hl
        fill = QColor(c)
        fill.setAlphaF(0.05 if dim else (min(fill_alpha + 0.3, 0.85) if hot else fill_alpha))
        painter.setPen(Qt.PenStyle.NoPen)
        painter.setBrush(fill)
        painter.drawPath(g.fill)
        painter.setBrush(Qt.BrushStyle.NoBrush)
        edge = QColor(c)
        edge.setAlphaF(0.2 if dim else 1.0)
        width = 3.0 if hot else 1.3
        if not dim:
            halo = QColor(halo_c)
            halo.setAlphaF(0.85 if hot else 0.6)
            painter.setPen(_cosmetic(halo, width + (2.6 if hot else 1.6)))
            painter.drawPath(g.fill)
        if selected and not dim and not hot:
            painter.setPen(_cosmetic(QColor(pal.accent), 2.0))
            painter.drawPath(g.fill)
            painter.setPen(_cosmetic(edge, 1.2))
        else:
            painter.setPen(_cosmetic(edge, width))
        painter.drawPath(g.fill)
        if not g.marks.isEmpty() and g.dia * scale * cross_mult > 6:
            painter.setPen(_cosmetic(edge, 1.4 if hot else 1.0))
            painter.drawPath(g.marks)


# =========================================================================== itens

class PathItem(QGraphicsItem):
    """Item base: um ou mais caminhos em cache, desenhados com caneta cosmética."""

    def __init__(self, key: int) -> None:
        super().__init__()
        self.key = key
        self.color = QColor("#888888")
        self.selected = False
        self._rect = QRectF()
        self.setFlag(QGraphicsItem.GraphicsItemFlag.ItemUsesExtendedStyleOption, True)

    def boundingRect(self) -> QRectF:  # noqa: N802
        return self._rect

    def set_color(self, color: str | QColor) -> None:
        self.color = QColor(color)
        self.update()

    def set_selected(self, on: bool) -> None:
        if on != self.selected:
            self.selected = on
            self.update()

    def hit(self, p: QPointF, tol: float) -> bool:
        return self._rect.adjusted(-tol, -tol, tol, tol).contains(p)

    @staticmethod
    def _lod(option: QStyleOptionGraphicsItem, painter: QPainter) -> float:
        t = painter.worldTransform()
        return math.hypot(t.m11(), t.m12())


class LayerItem(PathItem):
    """Camada fonte (Gerber preenchido, furos, contorno/geometria)."""

    def __init__(self, key: int, layer: Any, color: str, circle_segments: int = 32) -> None:
        super().__init__(key)
        self.kind = getattr(layer, "kind", "geometry")
        self.color = QColor(color)
        self.fill = QPainterPath()
        self.stroke = QPainterPath()
        self.marks = QPainterPath()
        self.drill_groups: list[DrillGroup] = []
        self.highlight_rank: int | None = None      # diâmetro destacado (tabela do inspetor)
        self.set_layer(layer, circle_segments)

    def set_layer(self, layer: Any, circle_segments: int = 32) -> None:
        self.prepareGeometryChange()
        self.kind = getattr(layer, "kind", "geometry")
        self.fill = QPainterPath()
        self.stroke = QPainterPath()
        self.marks = QPainterPath()
        self.drill_groups = []
        if isinstance(layer, GerberLayer):
            self.fill = qpaths.geometry_to_path(layer.copper)
        elif isinstance(layer, DrillLayer):
            # um caminho por diâmetro (cor categórica); ``fill``/``marks`` = união (seleção, limites)
            self.drill_groups = drill_layer_groups(layer, circle_segments)
            for g in self.drill_groups:
                self.fill.addPath(g.fill)
                self.marks.addPath(g.marks)
        elif isinstance(layer, GeometryLayer):
            self.stroke = qpaths.geometry_to_path(layer.geometry)
        r = self.fill.boundingRect() | self.stroke.boundingRect() | self.marks.boundingRect()
        self._rect = r.adjusted(-0.5, -0.5, 0.5, 0.5)
        self.update()

    def hit(self, p: QPointF, tol: float) -> bool:
        if not super().hit(p, tol):
            return False
        box = QRectF(p.x() - tol, p.y() - tol, 2 * tol, 2 * tol)
        if not self.fill.isEmpty() and (self.fill.contains(p) or self.fill.intersects(box)):
            return True
        if not self.stroke.isEmpty():
            st = QPainterPathStroker()
            st.setWidth(2 * tol)
            return st.createStroke(self.stroke).contains(p)
        return False

    def paint(self, painter: QPainter, option: QStyleOptionGraphicsItem, widget: QWidget | None = None) -> None:
        pal = theme.palette()
        c = QColor(self.color)
        scale = self._lod(option, painter)
        if self._rect.width() * scale < 2 and self._rect.height() * scale < 2:
            painter.fillRect(self._rect, c)
            return
        if self.drill_groups:
            _paint_drill_groups(painter, self.drill_groups, self.selected, scale, self.highlight_rank, 0.45)
        elif not self.fill.isEmpty():
            fill = QColor(c)
            fill.setAlphaF(0.62 if self.kind == "gerber" else 0.5)
            painter.setPen(Qt.PenStyle.NoPen)
            painter.setBrush(fill)
            painter.drawPath(self.fill)
            painter.setBrush(Qt.BrushStyle.NoBrush)
            edge = QColor(c)
            if self.selected:
                painter.setPen(_cosmetic(QColor(pal.accent), 2.0))
            else:
                edge.setAlphaF(0.95)
                painter.setPen(_cosmetic(edge, 1.0))
            painter.drawPath(self.fill)
        if not self.drill_groups and not self.marks.isEmpty() and scale > 4:
            painter.setPen(_cosmetic(c.lighter(130) if pal.dark else c.darker(130), 1.0))
            painter.drawPath(self.marks)
        if not self.stroke.isEmpty():
            painter.setBrush(Qt.BrushStyle.NoBrush)
            painter.setPen(_cosmetic(QColor(pal.accent) if self.selected else c, 2.2 if self.selected else 1.6))
            painter.drawPath(self.stroke)


class JobItem(PathItem):
    """Caminhos de ferramenta de um Job: cortes, deslocamentos rápidos e furos."""

    def __init__(self, key: int, job: Job, color: str | None = None) -> None:
        super().__init__(key)
        self.show_rapids = True
        self.show_width = True
        self.swept: list[tuple[float, QPainterPath]] = []   # (diâmetro, caminhos) para a faixa de corte
        self.cut = QPainterPath()
        self.rapid = QPainterPath()
        self.holes = QPainterPath()
        self.hole_marks = QPainterPath()
        self.drill_groups: list[DrillGroup] = []
        self.highlight_rank: int | None = None
        self.custom_color = QColor(color) if color else None
        self.set_job(job)

    def set_job(self, job: Job) -> None:
        self.prepareGeometryChange()
        cuts: list[np.ndarray] = []
        rapids: list[np.ndarray] = []
        hole_c: list[np.ndarray] = []
        hole_r: list[np.ndarray] = []
        hole_groups: dict[int, list[Any]] = {}       # rank -> [diâmetro, [centros]]
        rank_of_tool = {int(b.get("tool", -1)): int(b.get("rank", 0))
                        for b in (job.settings or {}).get("bits", []) or [] if isinstance(b, dict)}
        dias = sorted({round(float(op.tool.diameter), 4) for op in job.ops if len(op.points)})
        by_dia: dict[float, list[np.ndarray]] = {}
        last: np.ndarray | None = None
        for op in job.ops:
            for p in op.paths:
                a = np.asarray(p, dtype=float)
                if len(a) < 1:
                    continue
                a = a[:, :2]
                if last is not None:
                    rapids.append(np.vstack([last, a[0]]))
                if len(a) >= 2:
                    cuts.append(a)
                    by_dia.setdefault(round(max(op.tool.diameter, 0.0), 4), []).append(a)
                last = a[-1]
            pts = np.asarray(op.points, dtype=float).reshape(-1, 2)
            if len(pts):
                seq = pts if last is None else np.vstack([last, pts])
                rapids.append(seq)
                hole_c.append(pts)
                hole_r.append(np.full(len(pts), max(op.tool.diameter, 0.05) / 2.0))
                d = round(float(op.tool.diameter), 4)
                rk = rank_of_tool.get(int(op.tool.number), dias.index(d) if d in dias else 0)
                hole_groups.setdefault(rk, [d, []])[1].append(pts)
                last = pts[-1]
        self.cut = qpaths.polylines_to_path(cuts)
        self.rapid = qpaths.polylines_to_path(rapids)
        self.swept = [(d, qpaths.polylines_to_path(ps)) for d, ps in sorted(by_dia.items()) if d > 0]
        if hole_c:
            c = np.vstack(hole_c)
            r = np.concatenate(hole_r)
            self.holes = qpaths.circles_to_path(c, r, 20)
            self.hole_marks = QPainterPath()
        else:
            self.holes = QPainterPath()
            self.hole_marks = QPainterPath()
        # furação: um grupo por broca, com cor própria e cruz proporcional ao diâmetro dela
        self.drill_groups = []
        if job.operation != "drilling":
            hole_groups = {}
        for rk in sorted(hole_groups):
            d, chunks = hole_groups[rk]
            cc = np.vstack(chunks)
            dd = max(d, 0.05)
            marks = qpaths.crosses_to_path(cc, dd * 1.2)
            self.hole_marks.addPath(marks)
            self.drill_groups.append(DrillGroup(rk, dd, qpaths.circles_to_path(cc, dd / 2.0, 20), marks, len(cc)))
        r = self.cut.boundingRect() | self.rapid.boundingRect() | self.holes.boundingRect()
        pad = max([d for d, _ in self.swept], default=0.0) / 2 + 1
        self._rect = r.adjusted(-pad, -pad, pad, pad)
        self.update()

    def hit(self, p: QPointF, tol: float) -> bool:
        if not super().hit(p, tol):
            return False
        st = QPainterPathStroker()
        st.setWidth(2 * tol)
        if not self.cut.isEmpty() and st.createStroke(self.cut).contains(p):
            return True
        return not self.holes.isEmpty() and self.holes.contains(p)

    def paint(self, painter: QPainter, option: QStyleOptionGraphicsItem, widget: QWidget | None = None) -> None:
        pal = theme.palette()
        cut_c = QColor(self.custom_color or pal.cut)
        if self.show_rapids and not self.rapid.isEmpty():
            rc = QColor(pal.rapid)
            rc.setAlphaF(0.75)
            painter.setPen(_cosmetic(rc, 1.0, Qt.PenStyle.DashLine))
            painter.drawPath(self.rapid)
        lod = self._lod(option, painter)
        wide = False
        if self.show_width and self.swept:
            # faixa real removida pela ferramenta (largura = diâmetro efetivo, em mm na cena)
            band = QColor(QColor(pal.accent) if self.selected else cut_c)
            band.setAlphaF(0.30)
            for dia, path in self.swept:
                if dia * lod < 1.5:          # mais fina que ~1,5 px: a linha central já basta
                    continue
                pen = QPen(band, dia)
                pen.setCapStyle(Qt.PenCapStyle.RoundCap)
                pen.setJoinStyle(Qt.PenJoinStyle.RoundJoin)
                painter.setPen(pen)
                painter.setBrush(Qt.BrushStyle.NoBrush)
                painter.drawPath(path)
                wide = True
        if not self.cut.isEmpty():
            painter.setPen(_cosmetic(QColor(pal.accent) if self.selected else cut_c,
                                     (1.6 if self.selected else 1.0) if wide else (2.2 if self.selected else 1.5)))
            painter.drawPath(self.cut)
        if self.drill_groups:
            _paint_drill_groups(painter, self.drill_groups, self.selected, lod, self.highlight_rank, 0.22,
                                cross_mult=1.2)
        elif not self.holes.isEmpty():
            # furos de outras operações (ex.: mouse bites do recorte): cor do caminho
            painter.setPen(_cosmetic(QColor(pal.accent) if self.selected else cut_c, 1.6))
            fill = QColor(cut_c)
            fill.setAlphaF(0.18)
            painter.setBrush(fill)
            painter.drawPath(self.holes)
            painter.setBrush(Qt.BrushStyle.NoBrush)
            painter.setPen(_cosmetic(cut_c, 1.0))
            painter.drawPath(self.hole_marks)


class GcodeItem(PathItem):
    """Pré-visualização de um programa G-code, com destaque do trecho já executado."""

    def __init__(self, key: int, preview: Any) -> None:
        super().__init__(key)
        self.preview = preview
        f = preview.feeds
        r = preview.rapids
        self.feed_path = qpaths.segments_to_path(f) if len(f) else QPainterPath()
        self.rapid_path = qpaths.segments_to_path(r) if len(r) else QPainterPath()
        self.done_path = QPainterPath()
        self.current = QPainterPath()
        self.show_rapids = True
        rect = self.feed_path.boundingRect() | self.rapid_path.boundingRect()
        self._rect = rect.adjusted(-1, -1, 1, 1)
        self.setZValue(20)

    def hit(self, p: QPointF, tol: float) -> bool:
        if not super().hit(p, tol):
            return False
        st = QPainterPathStroker()
        st.setWidth(2 * tol)
        return not self.feed_path.isEmpty() and st.createStroke(self.feed_path).contains(p)

    def set_done_line(self, line: int) -> None:
        """Destaca todos os cortes até a linha (0-based) ``line``."""
        pv = self.preview
        if line < 0 or not len(pv.feeds):
            self.done_path = QPainterPath()
            self.current = QPainterPath()
        else:
            k = int(np.searchsorted(pv.feed_lines, line, side="right"))
            self.done_path = qpaths.segments_to_path(pv.feeds[:k]) if k else QPainterPath()
            cur = [i for i in (k - 1,) if 0 <= i < len(pv.feeds)]
            self.current = qpaths.segments_to_path(pv.feeds[cur]) if cur else QPainterPath()
        self.update()

    def paint(self, painter: QPainter, option: QStyleOptionGraphicsItem, widget: QWidget | None = None) -> None:
        pal = theme.palette()
        if self.show_rapids and not self.rapid_path.isEmpty():
            rc = QColor(pal.rapid)
            rc.setAlphaF(0.7)
            painter.setPen(_cosmetic(rc, 1.0, Qt.PenStyle.DashLine))
            painter.drawPath(self.rapid_path)
        painter.setPen(_cosmetic(QColor(pal.cut), 1.4))
        painter.drawPath(self.feed_path)
        if not self.done_path.isEmpty():
            painter.setPen(_cosmetic(QColor(pal.done), 2.2))
            painter.drawPath(self.done_path)
        if not self.current.isEmpty():
            painter.setPen(_cosmetic(QColor(pal.tool), 3.0))
            painter.drawPath(self.current)


class ToolMarker(QGraphicsItem):
    """Posição da ferramenta (tamanho fixo em pixels)."""

    def __init__(self) -> None:
        super().__init__()
        self.setFlag(QGraphicsItem.GraphicsItemFlag.ItemIgnoresTransformations, True)
        self.setZValue(1000)
        self.live = False

    def boundingRect(self) -> QRectF:  # noqa: N802
        return QRectF(-14, -14, 28, 28)

    def paint(self, painter: QPainter, option: QStyleOptionGraphicsItem, widget: QWidget | None = None) -> None:
        pal = theme.palette()
        col = QColor(pal.done if self.live else pal.tool)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        halo = QColor(pal.canvas_bg)
        halo.setAlphaF(0.7)
        painter.setPen(QPen(halo, 4))
        painter.drawEllipse(QPointF(0, 0), 8, 8)
        painter.setPen(QPen(col, 2))
        painter.setBrush(Qt.BrushStyle.NoBrush)
        painter.drawEllipse(QPointF(0, 0), 8, 8)
        painter.drawLine(QLineF(-13, 0, -4, 0))
        painter.drawLine(QLineF(4, 0, 13, 0))
        painter.drawLine(QLineF(0, -13, 0, -4))
        painter.drawLine(QLineF(0, 4, 0, 13))
        painter.setBrush(col)
        painter.drawEllipse(QPointF(0, 0), 1.6, 1.6)


# =========================================================================== simulação

class Simulation:
    """Sequência temporal de um ou mais Jobs para animação."""

    CHUNK = 2048

    def __init__(self, jobs: list[Job], rapid_rate: float = 1000.0) -> None:
        pts: list[np.ndarray] = []
        kinds: list[np.ndarray] = []
        speeds: list[np.ndarray] = []
        holes: list[np.ndarray] = []
        hole_r: list[float] = []
        hole_idx: list[int] = []
        last: np.ndarray | None = None
        n = 0
        for job in jobs:
            for op in job.ops:
                feed = max(op.params.feed_xy, 1.0)
                for p in op.paths:
                    a = np.asarray(p, dtype=float)[:, :2]
                    if not len(a):
                        continue
                    if last is not None:
                        pts.append(a[:1])
                        kinds.append(np.zeros(1, dtype=np.int8))
                        speeds.append(np.full(1, rapid_rate))
                        n += 1
                    else:
                        pts.append(a[:1])
                        kinds.append(np.zeros(1, dtype=np.int8))
                        speeds.append(np.full(1, rapid_rate))
                        n += 1
                    if len(a) > 1:
                        pts.append(a[1:])
                        kinds.append(np.ones(len(a) - 1, dtype=np.int8))
                        speeds.append(np.full(len(a) - 1, feed))
                        n += len(a) - 1
                    last = a[-1]
                hp = np.asarray(op.points, dtype=float).reshape(-1, 2)
                for h in hp:
                    pts.append(h[None, :])
                    kinds.append(np.full(1, 2, dtype=np.int8))   # 2 = furo (rápido + mergulho)
                    speeds.append(np.full(1, rapid_rate))
                    holes.append(h[None, :])
                    hole_r.append(max(float(op.tool.diameter), 0.1) / 2.0)
                    hole_idx.append(n)
                    n += 1
                    last = h
        if pts:
            self.pts = np.vstack(pts)
            self.kind = np.concatenate(kinds)
            spd = np.concatenate(speeds)
        else:
            self.pts = np.zeros((0, 2))
            self.kind = np.zeros(0, dtype=np.int8)
            spd = np.zeros(0)
        # segmento i vai de pts[i-1] a pts[i]; o primeiro parte de pts[0]
        prev = np.vstack([self.pts[:1], self.pts[:-1]]) if len(self.pts) else self.pts
        self.prev = prev
        seg_len = np.hypot(*(self.pts - prev).T) if len(self.pts) else np.zeros(0)
        dur = np.where(spd > 0, seg_len / np.maximum(spd, 1e-9) * 60.0, 0.0)
        dur = dur + np.where(self.kind == 2, 1.2, 0.0)  # tempo típico de mergulho/retração
        self.t_end = np.cumsum(dur)
        self.total = float(self.t_end[-1]) if len(self.t_end) else 0.0
        self.holes = np.vstack(holes) if holes else np.zeros((0, 2))
        self.hole_r = np.asarray(hole_r, dtype=float)      # raio real de cada furo (mm)
        self.hole_seg = np.asarray(hole_idx, dtype=np.int64)
        cut_mask = self.kind == 1
        self.cut_idx = np.flatnonzero(cut_mask)
        self._chunks: dict[int, QPainterPath] = {}

    def index_at(self, t: float) -> int:
        return int(np.searchsorted(self.t_end, t, side="left"))

    def position(self, t: float) -> tuple[float, float]:
        if not len(self.pts):
            return (0.0, 0.0)
        i = min(self.index_at(t), len(self.pts) - 1)
        t0 = self.t_end[i - 1] if i > 0 else 0.0
        d = self.t_end[i] - t0
        f = 0.0 if d <= 0 else min(1.0, max(0.0, (t - t0) / d))
        a, b = self.prev[i], self.pts[i]
        p = a + (b - a) * f
        return float(p[0]), float(p[1])

    def done_paths(self, t: float) -> tuple[list[QPainterPath], QPainterPath, QPainterPath]:
        """(chunks completos em cache, chunk parcial, furos concluídos)."""
        i = self.index_at(t)
        k = int(np.searchsorted(self.cut_idx, i, side="left"))   # cortes concluídos
        full = k // self.CHUNK
        chunks = [self._chunk(c) for c in range(full)]
        part_idx = self.cut_idx[full * self.CHUNK:k]
        partial = self._segments(part_idx)
        nh = int(np.searchsorted(self.hole_seg, i, side="left"))
        holes = qpaths.circles_to_path(self.holes[:nh], self.hole_r[:nh], 16) if nh else QPainterPath()
        return chunks, partial, holes

    def _segments(self, idx: np.ndarray) -> QPainterPath:
        if not len(idx):
            return QPainterPath()
        segs = np.stack([self.prev[idx], self.pts[idx]], axis=1)
        return qpaths.segments_to_path(segs)

    def _chunk(self, c: int) -> QPainterPath:
        if c not in self._chunks:
            self._chunks[c] = self._segments(self.cut_idx[c * self.CHUNK:(c + 1) * self.CHUNK])
        return self._chunks[c]


class SimTrailItem(QGraphicsItem):
    def __init__(self, sim: Simulation) -> None:
        super().__init__()
        self.sim = sim
        self.t = 0.0
        self._chunks: list[QPainterPath] = []
        self._partial = QPainterPath()
        self._holes = QPainterPath()
        b = sim.pts
        if len(b):
            self._rect = QRectF(QPointF(b[:, 0].min(), b[:, 1].min()),
                                QPointF(b[:, 0].max(), b[:, 1].max())).adjusted(-2, -2, 2, 2)
        else:
            self._rect = QRectF()
        self.setZValue(30)

    def boundingRect(self) -> QRectF:  # noqa: N802
        return self._rect

    def set_time(self, t: float) -> None:
        self.t = t
        self._chunks, self._partial, self._holes = self.sim.done_paths(t)
        self.update()

    def paint(self, painter: QPainter, option: QStyleOptionGraphicsItem, widget: QWidget | None = None) -> None:
        pen = _cosmetic(QColor(theme.palette().done), 2.4)
        painter.setPen(pen)
        for c in self._chunks:
            painter.drawPath(c)
        painter.drawPath(self._partial)
        if not self._holes.isEmpty():
            painter.setBrush(QColor(theme.palette().done))
            painter.drawPath(self._holes)


class SimulationBar(QFrame):
    """Controles flutuantes da simulação (sobre o canvas)."""

    closed = Signal()

    def __init__(self, view: "CanvasView") -> None:
        super().__init__(view)
        self.view = view
        self.setObjectName("Card")
        self.setAccessibleName("Controles da simulação")
        lay = QHBoxLayout(self)
        lay.setContentsMargins(10, 6, 10, 6)
        lay.setSpacing(8)
        self.play = QToolButton()
        self.play.setIcon(icon("pause"))
        self.play.setToolTip("Pausar/continuar simulação (Espaço com o foco aqui)")
        self.play.setAccessibleName("Pausar ou continuar simulação")
        self.play.clicked.connect(view.sim_toggle)
        self.restart = QToolButton()
        self.restart.setIcon(icon("reset"))
        self.restart.setToolTip("Reiniciar simulação")
        self.restart.setAccessibleName("Reiniciar simulação")
        self.restart.clicked.connect(lambda: view.sim_seek(0.0))
        self.slider = QSlider(Qt.Orientation.Horizontal)
        self.slider.setRange(0, 1000)
        self.slider.setMinimumWidth(200)
        self.slider.setAccessibleName("Posição da simulação")
        self.slider.sliderMoved.connect(lambda v: view.sim_seek(v / 1000.0 * (view.sim.total if view.sim else 0)))
        self.time = QLabel("0 s / 0 s")
        self.time.setMinimumWidth(130)
        self.speed = QComboBox()
        self.speed.setAccessibleName("Velocidade da simulação")
        self.speed.setToolTip("Velocidade da simulação em relação ao tempo real estimado")
        for f in (1, 2, 5, 10, 25, 50, 100, 250):
            self.speed.addItem(f"{f}×", f)
        self.speed.setCurrentIndex(4)
        self.close_btn = QToolButton()
        self.close_btn.setIcon(icon("close"))
        self.close_btn.setToolTip("Encerrar simulação (Esc)")
        self.close_btn.setAccessibleName("Encerrar simulação")
        self.close_btn.clicked.connect(view.sim_stop)
        for w in (self.play, self.restart, self.slider, self.time, self.speed, self.close_btn):
            lay.addWidget(w)
        self.hide()

    def speed_factor(self) -> float:
        return float(self.speed.currentData() or 1)

    def sync(self, t: float, total: float, running: bool) -> None:
        self.play.setIcon(icon("pause" if running else "play"))
        if not self.slider.isSliderDown():
            self.slider.setValue(int(1000 * t / total) if total > 0 else 0)
        self.time.setText(f"{units.fmt_time(t)} / {units.fmt_time(total)}")


# =========================================================================== edição

class EditOverlay(QGraphicsItem):
    """Destaque dos caminhos selecionados/sob o mouse no modo de edição."""

    def __init__(self) -> None:
        super().__init__()
        self.sel_path = QPainterPath()
        self.sel_holes = QPainterPath()
        self.hover_path = QPainterPath()
        self.hover_holes = QPainterPath()
        self._rect = QRectF()
        self.setZValue(1000)

    def boundingRect(self) -> QRectF:  # noqa: N802
        return self._rect

    @staticmethod
    def _build(ops: list, refs: Any) -> tuple[QPainterPath, QPainterPath]:
        lines, centers, radii = [], [], []
        for oi, kind, i in refs:
            if not (0 <= oi < len(ops)):
                continue
            op = ops[oi]
            if kind == "path" and i < len(op.paths):
                a = np.asarray(op.paths[i], dtype=float)[:, :2]
                if len(a) >= 2:
                    lines.append(a)
            elif kind == "point":
                pts = np.asarray(op.points, dtype=float).reshape(-1, 2)
                if i < len(pts):
                    centers.append(pts[i])
                    radii.append(max(op.tool.diameter, 0.05) / 2.0)
        lp = qpaths.polylines_to_path(lines) if lines else QPainterPath()
        hp = qpaths.circles_to_path(np.array(centers), np.array(radii), 20) if centers else QPainterPath()
        return lp, hp

    def set_state(self, ops: list, selection: Any, hover: Any) -> None:
        self.prepareGeometryChange()
        self.sel_path, self.sel_holes = self._build(ops, selection)
        self.hover_path, self.hover_holes = self._build(ops, [hover] if hover else [])
        r = (self.sel_path.boundingRect() | self.sel_holes.boundingRect()
             | self.hover_path.boundingRect() | self.hover_holes.boundingRect())
        self._rect = r.adjusted(-2, -2, 2, 2)
        self.update()

    def paint(self, painter: QPainter, option: QStyleOptionGraphicsItem, widget: QWidget | None = None) -> None:
        pal = theme.palette()
        if not self.hover_path.isEmpty() or not self.hover_holes.isEmpty():
            painter.setPen(_cosmetic(QColor(pal.warning), 4.0))
            painter.setBrush(Qt.BrushStyle.NoBrush)
            painter.drawPath(self.hover_path)
            painter.drawPath(self.hover_holes)
        sel = QColor(pal.danger)
        painter.setPen(_cosmetic(sel, 3.0))
        painter.setBrush(Qt.BrushStyle.NoBrush)
        painter.drawPath(self.sel_path)
        if not self.sel_holes.isEmpty():
            fill = QColor(sel)
            fill.setAlphaF(0.35)
            painter.setBrush(fill)
            painter.drawPath(self.sel_holes)


class EditBar(QFrame):
    """Barra flutuante do modo de edição de caminhos."""

    def __init__(self, view: "CanvasView") -> None:
        super().__init__(view)
        self.view = view
        self.setObjectName("Card")
        self.setAccessibleName("Ferramentas de edição de caminhos")
        lay = QHBoxLayout(self)
        lay.setContentsMargins(10, 6, 10, 6)
        lay.setSpacing(6)
        self.title = QLabel()
        self.title.setObjectName("Heading")
        lay.addWidget(self.title)

        def btn(name: str, text: str, tip: str, slot: Any, checkable: bool = False) -> QToolButton:
            b = QToolButton()
            b.setIcon(icon(name))
            b.setText(text)
            b.setToolButtonStyle(Qt.ToolButtonStyle.ToolButtonTextBesideIcon)
            b.setToolTip(tip)
            b.setAccessibleName(text)
            b.setAccessibleDescription(tip)
            b.setCheckable(checkable)
            b.setAutoRaise(True)
            if checkable:
                b.toggled.connect(slot)
            else:
                b.clicked.connect(lambda _=False: slot())
            lay.addWidget(b)
            return b

        self.select_btn = btn("pointer", "Selecionar",
                              "Clique num caminho ou furo para selecionar; arraste para selecionar por área "
                              "(da esquerda p/ direita: só o que está totalmente dentro; da direita p/ esquerda: "
                              "tudo que tocar). Shift/Ctrl somam à seleção. (S)",
                              lambda on: on and view.edit_set_tool("select"), True)
        self.trim_btn = btn("scissors", "Trecho",
                            "Arraste um retângulo: apaga só a parte dos caminhos (e os furos) que fica dentro "
                            "dele, mantendo o resto. (T)",
                            lambda on: on and view.edit_set_tool("trim"), True)
        self.select_btn.setAutoExclusive(True)
        self.trim_btn.setAutoExclusive(True)
        sep = QFrame()
        sep.setFrameShape(QFrame.Shape.VLine)
        lay.addWidget(sep)
        self.count = QLabel()
        self.count.setObjectName("Muted")
        lay.addWidget(self.count)
        self.del_btn = btn("trash", "Excluir", "Remove os itens selecionados (Delete)",
                           lambda: view.edit_request("delete"))
        self.keep_btn = btn("keep", "Manter só", "Remove tudo que NÃO está selecionado",
                            lambda: view.edit_request("keep"))
        self.rev_btn = btn("swap", "Inverter",
                           "Inverte o sentido dos caminhos selecionados (troca fresamento concordante ↔ "
                           "convencional) (R)", lambda: view.edit_request("reverse"))
        self.opt_btn = btn("reorder", "Reotimizar",
                           "Reordena os cortes restantes para reduzir o deslocamento em vazio",
                           lambda: view.edit_request("reoptimize"))
        self.done_btn = btn("check", "Concluir", "Sair do modo de edição (Esc ou Enter)", view.edit_stop)
        self.buttons = [self.select_btn, self.trim_btn, self.del_btn, self.keep_btn, self.rev_btn,
                        self.opt_btn, self.done_btn]
        self.hide()

    def set_compact(self, on: bool) -> None:
        """Só ícones quando o canvas é estreito (os textos continuam no tooltip/nome acessível)."""
        style = Qt.ToolButtonStyle.ToolButtonIconOnly if on else Qt.ToolButtonStyle.ToolButtonTextBesideIcon
        for b in self.buttons:
            b.setToolButtonStyle(style)

    def sync(self, name: str, n_sel: int, n_paths: int, n_holes: int, tool: str) -> None:
        self.title.setText("Editando")
        self.title.setToolTip(name)
        self.setAccessibleDescription(f"Editando os caminhos de {name}")
        tot = f"{n_paths} caminhos" + (f" · {n_holes} furos" if n_holes else "")
        self.count.setText(f"{n_sel} sel. / {tot}" if n_sel else tot)
        for b in (self.del_btn, self.keep_btn, self.rev_btn):
            b.setEnabled(n_sel > 0)
        for b, key in ((self.select_btn, "select"), (self.trim_btn, "trim")):
            b.blockSignals(True)
            b.setChecked(tool == key)
            b.blockSignals(False)


class NavBar(QFrame):
    """Controles de navegação flutuantes no canto do canvas (estilo Figma/Fusion)."""

    def __init__(self, view: "CanvasView") -> None:
        super().__init__(view)
        self.setObjectName("NavBar")
        self.setAccessibleName("Navegação do canvas")
        self.lay = QHBoxLayout(self)
        self.lay.setContentsMargins(4, 3, 4, 3)
        self.lay.setSpacing(1)
        self.zoom = QLabel()
        self.zoom.setAccessibleName("Escala atual")
        self.zoom.setToolTip("Escala da vista (pixels por milímetro)")

    def set_actions(self, groups: list[list[Any]]) -> None:
        """Grupos de ``QAction``; ``"zoom"`` insere o indicador de escala."""
        for gi, grp in enumerate(groups):
            if gi:
                sep = QFrame()
                sep.setFrameShape(QFrame.Shape.VLine)
                sep.setStyleSheet("color: transparent; max-width: 6px;")
                self.lay.addWidget(sep)
            for a in grp:
                if a == "zoom":
                    self.lay.addWidget(self.zoom)
                    continue
                b = QToolButton()
                b.setDefaultAction(a)
                b.setAutoRaise(True)
                b.setIconSize(QSize(16, 16))
                b.setAccessibleName(a.text().replace("&", ""))
                self.lay.addWidget(b)
        self.adjustSize()


# =========================================================================== vista

class CanvasView(QGraphicsView):
    cursorMoved = Signal(float, float)
    itemClicked = Signal(int)            # chave do item (id de camada ou job); -1 = nada
    measureChanged = Signal(object)      # dict com dx, dy, dist ou None
    zoomChanged = Signal(float)
    editAction = Signal(int, str, object)   # job_id, ação ("delete","keep","trim","reverse","reoptimize"), dados
    editModeChanged = Signal(bool)

    def __init__(self, parent: QWidget | None = None, *, opengl: bool = False) -> None:
        super().__init__(parent)
        self.setObjectName("Canvas")
        self.setAccessibleName("Canvas de visualização da placa")
        self.setAccessibleDescription(
            "Mostra as camadas e os caminhos de ferramenta. Roda do mouse: zoom; botão do meio ou "
            "Espaço+arrastar: mover; tecla 1 ou F: ajustar à tela; M: medir; Esc: cancelar.")
        sc = QGraphicsScene(self)
        sc.setSceneRect(-SCENE_EXTENT, -SCENE_EXTENT, 2 * SCENE_EXTENT, 2 * SCENE_EXTENT)
        sc.setItemIndexMethod(QGraphicsScene.ItemIndexMethod.BspTreeIndex)
        self.setScene(sc)
        self.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        self.setVerticalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        self.setTransformationAnchor(QGraphicsView.ViewportAnchor.NoAnchor)
        self.setResizeAnchor(QGraphicsView.ViewportAnchor.AnchorViewCenter)
        self.setDragMode(QGraphicsView.DragMode.NoDrag)
        self.setOptimizationFlags(QGraphicsView.OptimizationFlag.DontSavePainterState
                                  | QGraphicsView.OptimizationFlag.DontAdjustForAntialiasing)
        self.setFrameShape(QFrame.Shape.NoFrame)
        self.setMouseTracking(True)
        self.setFocusPolicy(Qt.FocusPolicy.StrongFocus)
        self.setTransform(QTransform.fromScale(4.0, -4.0))

        self.items_by_key: dict[int, PathItem] = {}
        self.gcode_item: GcodeItem | None = None
        self.tool_marker = ToolMarker()
        self.tool_marker.hide()
        sc.addItem(self.tool_marker)
        self.show_grid = True
        self.show_rulers = True
        self.show_rapids = True
        self.show_tool_width = True
        self.antialias = True
        self.circle_segments = 32
        self.opengl = False
        self.mode = "select"             # "select" | "measure"
        self._pan_from: QPointF | None = None
        self._space = False
        self._press_pos: QPointF | None = None
        self._measure: list[QPointF] = []
        self._measure_hover: QPointF | None = None
        self._cursor_scene = QPointF()
        self._selected_key: int | None = None

        # simulação
        self.sim: Simulation | None = None
        self.sim_item: SimTrailItem | None = None
        self.sim_t = 0.0
        self.sim_running = False
        self.sim_keys: set[int] = set()
        self.sim_timer = QTimer(self)
        self.sim_timer.setInterval(16)
        self.sim_timer.timeout.connect(self._sim_tick)
        self.sim_bar = SimulationBar(self)

        # edição de caminhos
        self.edit_key: int | None = None
        self.edit_name = ""
        self.edit_ops: list = []
        self.edit_index: Any = None
        self.edit_sel: set = set()
        self.edit_hover: Any = None
        self.edit_tool = "select"
        self._band: tuple[QPointF, QPointF] | None = None
        self.edit_overlay = EditOverlay()
        self.edit_overlay.hide()
        sc.addItem(self.edit_overlay)
        self.edit_bar = EditBar(self)
        self.nav_bar = NavBar(self)
        self.zoomChanged.connect(lambda _s: self._place_nav_bar())

        self.set_opengl(opengl)
        self._apply_render_hints()
        theme.on_change(self, self._on_theme)

    # ------------------------------------------------------------------ configuração
    def set_opengl(self, on: bool) -> bool:
        """Liga/desliga viewport OpenGL. Retorna o estado efetivo."""
        if on and os.environ.get("QT_QPA_PLATFORM", "") in ("offscreen", "minimal"):
            on = False
        if on == self.opengl and self.viewport() is not None:
            return self.opengl
        if on:
            try:
                from PySide6.QtGui import QOpenGLContext
                from PySide6.QtOpenGLWidgets import QOpenGLWidget
                ctx = QOpenGLContext()
                if not ctx.create():
                    raise RuntimeError("sem contexto OpenGL")
                gl = QOpenGLWidget()
                fmt = QSurfaceFormat()
                fmt.setSamples(4)
                gl.setFormat(fmt)
                self.setViewport(gl)
                self.setViewportUpdateMode(QGraphicsView.ViewportUpdateMode.FullViewportUpdate)
                self.opengl = True
            except Exception:
                on = False
        if not on:
            self.setViewport(QWidget())
            self.setViewportUpdateMode(QGraphicsView.ViewportUpdateMode.MinimalViewportUpdate)
            self.opengl = False
        self.viewport().setMouseTracking(True)
        for it in self.items_by_key.values():
            self._apply_cache(it)
        self._apply_render_hints()
        return self.opengl

    def _apply_cache(self, it: QGraphicsItem) -> None:
        it.setCacheMode(QGraphicsItem.CacheMode.NoCache if self.opengl
                        else QGraphicsItem.CacheMode.DeviceCoordinateCache)

    def _apply_render_hints(self) -> None:
        self.setRenderHint(QPainter.RenderHint.Antialiasing, self.antialias)
        self.setRenderHint(QPainter.RenderHint.TextAntialiasing, True)

    def set_antialias(self, on: bool) -> None:
        self.antialias = on
        self._apply_render_hints()
        self._invalidate_items()

    def set_show_grid(self, on: bool) -> None:
        self.show_grid = on
        self.resetCachedContent()
        self.viewport().update()

    def set_show_rulers(self, on: bool) -> None:
        self.show_rulers = on
        self.viewport().update()

    def set_show_rapids(self, on: bool) -> None:
        self.show_rapids = on
        for it in self.items_by_key.values():
            if isinstance(it, JobItem):
                it.show_rapids = on
                it.update()
        if self.gcode_item:
            self.gcode_item.show_rapids = on
            self.gcode_item.update()
        self._invalidate_items()

    def _invalidate_items(self) -> None:
        for it in self.items_by_key.values():
            if it.cacheMode() != QGraphicsItem.CacheMode.NoCache:
                it.setCacheMode(QGraphicsItem.CacheMode.NoCache)
                self._apply_cache(it)
            it.update()
        self.viewport().update()

    def _on_theme(self, _p: object) -> None:
        self._invalidate_items()

    # ------------------------------------------------------------------ itens
    def add_layer(self, key: int, layer: Any, color: str, z: float = 0.0) -> LayerItem:
        self.remove_item(key)
        it = LayerItem(key, layer, color, self.circle_segments)
        it.setZValue(z)
        self._apply_cache(it)
        self.scene().addItem(it)
        self.items_by_key[key] = it
        return it

    def update_layer(self, key: int, layer: Any) -> None:
        it = self.items_by_key.get(key)
        if isinstance(it, LayerItem):
            it.set_layer(layer, self.circle_segments)
            self._invalidate_items()

    def set_show_tool_width(self, on: bool) -> None:
        self.show_tool_width = on
        for it in self.items_by_key.values():
            if isinstance(it, JobItem):
                it.show_width = on
        self._invalidate_items()

    def add_job(self, key: int, job: Job, z: float = 10.0) -> JobItem:
        self.remove_item(key)
        it = JobItem(key, job)
        it.show_rapids = self.show_rapids
        it.show_width = self.show_tool_width
        it.setZValue(z)
        self._apply_cache(it)
        self.scene().addItem(it)
        self.items_by_key[key] = it
        return it

    def remove_item(self, key: int) -> None:
        if key in self.sim_keys:
            self.sim_stop()          # a simulação era deste caminho: não pode ficar "fantasma" no canvas
        it = self.items_by_key.pop(key, None)
        if it is not None:
            self.scene().removeItem(it)
        if self._selected_key == key:
            self._selected_key = None

    def highlight_drill(self, key: int, rank: int | list[int] | None) -> None:
        """Destaca no canvas os furos de um diâmetro (``rank``, ou lista deles) da camada/job ``key``."""
        for k, it in self.items_by_key.items():
            if isinstance(it, (LayerItem, JobItem)) and it.drill_groups:
                it.highlight_rank = rank if k == key else None
        self._invalidate_items()

    def set_item_visible(self, key: int, on: bool) -> None:
        it = self.items_by_key.get(key)
        if it is not None:
            it.setVisible(on)

    def set_item_color(self, key: int, color: str) -> None:
        it = self.items_by_key.get(key)
        if it is not None:
            if isinstance(it, JobItem):
                it.custom_color = QColor(color)
            it.set_color(color)
            self._invalidate_items()

    def set_selected(self, key: int | None | set[int]) -> None:
        keys = key if isinstance(key, set) else ({key} if key is not None else set())
        for k, it in self.items_by_key.items():
            it.set_selected(k in keys)
        self._selected_key = key if not isinstance(key, set) else next(iter(key), None)
        self._invalidate_items()

    def clear_items(self) -> None:
        for k in list(self.items_by_key):
            self.remove_item(k)
        self.set_gcode_preview(None)
        self.sim_stop()

    def set_gcode_preview(self, preview: Any | None) -> GcodeItem | None:
        if self.gcode_item is not None:
            self.scene().removeItem(self.gcode_item)
            self.gcode_item = None
        if preview is not None:
            self.gcode_item = GcodeItem(PROGRAM_KEY, preview)
            self.gcode_item.show_rapids = self.show_rapids
            self.scene().addItem(self.gcode_item)
        return self.gcode_item

    def set_tool_position(self, x: float | None, y: float | None = None, live: bool = True) -> None:
        if x is None or y is None:
            self.tool_marker.hide()
            return
        self.tool_marker.live = live
        self.tool_marker.setPos(x, y)
        self.tool_marker.show()

    # ------------------------------------------------------------------ zoom / pan
    def scale_factor(self) -> float:
        t = self.transform()
        return abs(t.m11())

    def zoom_by(self, factor: float, anchor: QPointF | None = None) -> None:
        cur = self.scale_factor()
        new = min(MAX_SCALE, max(MIN_SCALE, cur * factor))
        factor = new / cur
        if abs(factor - 1.0) < 1e-9:
            return
        vp = anchor if anchor is not None else QPointF(self.viewport().rect().center())
        before = self.mapToScene(vp.toPoint())
        self.scale(factor, factor)
        self._recenter_scrollbars(before, vp)
        self.zoomChanged.emit(self.scale_factor())
        self.viewport().update()

    def _recenter_scrollbars(self, scene_pt: QPointF, vp: QPointF) -> None:
        # Com âncora NoAnchor, translate() é ignorado quando há barras de rolagem;
        # corrigimos movendo as barras para manter o ponto sob o cursor.
        now = self.mapFromScene(scene_pt)
        dx = now.x() - vp.x()
        dy = now.y() - vp.y()
        h, v = self.horizontalScrollBar(), self.verticalScrollBar()
        h.setValue(h.value() + round(dx))
        v.setValue(v.value() + round(dy))

    def fit_rect(self, rect: QRectF, margin: float = 0.08) -> None:
        if rect.isNull() or rect.width() <= 0 and rect.height() <= 0:
            rect = QRectF(-10, -10, 120, 90)
        w = max(rect.width(), 1e-3)
        h = max(rect.height(), 1e-3)
        vp = self.viewport().rect()
        ruler = RULER if self.show_rulers else 0
        avail_w = max(50, vp.width() - ruler - 16)
        avail_h = max(50, vp.height() - ruler - 16)
        s = min(avail_w / (w * (1 + 2 * margin)), avail_h / (h * (1 + 2 * margin)))
        s = min(MAX_SCALE, max(MIN_SCALE, s))
        self.setTransform(QTransform.fromScale(s, -s))
        c = rect.center()
        # compensa as réguas para centralizar na área útil
        off = ruler / 2.0 / s
        self.centerOn(QPointF(c.x() - off, c.y() + off))
        self.zoomChanged.emit(s)
        self.viewport().update()

    def visible_items_rect(self) -> QRectF:
        r = QRectF()
        for it in self.items_by_key.values():
            if it.isVisible():
                br = it.sceneBoundingRect()
                r = br if r.isNull() else r | br
        if self.gcode_item is not None and self.gcode_item.isVisible():
            br = self.gcode_item.sceneBoundingRect()
            r = br if r.isNull() else r | br
        return r

    def fit_all(self) -> None:
        self.fit_rect(self.visible_items_rect())

    # ------------------------------------------------------------------ ferramentas
    def set_mode(self, mode: str) -> None:
        self.mode = mode
        self._measure = []
        self._measure_hover = None
        self.viewport().setCursor(self._mode_cursor())
        self.measureChanged.emit(None)
        self.viewport().update()

    # ------------------------------------------------------------------ eventos
    def wheelEvent(self, ev: QWheelEvent) -> None:  # noqa: N802
        dy = ev.angleDelta().y() or ev.pixelDelta().y()
        if not dy:
            return
        if ev.modifiers() & Qt.KeyboardModifier.ShiftModifier:
            self.horizontalScrollBar().setValue(self.horizontalScrollBar().value() - dy)
            return
        factor = 1.0015 ** dy
        self.zoom_by(factor, ev.position())
        ev.accept()

    def mousePressEvent(self, ev: QMouseEvent) -> None:  # noqa: N802
        self.setFocus(Qt.FocusReason.MouseFocusReason)
        if ev.button() == Qt.MouseButton.MiddleButton or (
                ev.button() == Qt.MouseButton.LeftButton and self._space):
            self._pan_from = ev.position()
            self.viewport().setCursor(Qt.CursorShape.ClosedHandCursor)
            ev.accept()
            return
        if ev.button() == Qt.MouseButton.LeftButton:
            self._press_pos = ev.position()
            if self.mode == "edit":
                self._band = None
                ev.accept()
                return
            if self.mode == "measure":
                p = self._snap(self.mapToScene(ev.position().toPoint()))
                if len(self._measure) >= 2:
                    self._measure = []
                self._measure.append(p)
                self._emit_measure()
                self.viewport().update()
            ev.accept()
            return
        if ev.button() == Qt.MouseButton.RightButton and self.mode == "measure":
            self.set_mode("select")
            return
        super().mousePressEvent(ev)

    def mouseMoveEvent(self, ev: QMouseEvent) -> None:  # noqa: N802
        pos = ev.position()
        if self._pan_from is not None:
            d = pos - self._pan_from
            self._pan_from = pos
            h, v = self.horizontalScrollBar(), self.verticalScrollBar()
            h.setValue(h.value() - round(d.x()))
            v.setValue(v.value() - round(d.y()))
            self.viewport().update()
            return
        sp = self.mapToScene(pos.toPoint())
        self._cursor_scene = sp
        self.cursorMoved.emit(sp.x(), sp.y())
        if self.mode == "edit":
            if self._press_pos is not None and ev.buttons() & Qt.MouseButton.LeftButton:
                if (pos - self._press_pos).manhattanLength() >= 5:
                    self._band = (self._press_pos, pos)
                    self.viewport().update()
            else:
                hv = self._edit_pick(pos)
                if hv != self.edit_hover:
                    self.edit_hover = hv
                    self._edit_refresh(bar=False)
            if self.show_rulers:
                self.viewport().update(0, 0, self.viewport().width(), RULER)
                self.viewport().update(0, 0, RULER, self.viewport().height())
            return
        if self.mode == "measure" and len(self._measure) == 1:
            self._measure_hover = self._snap(sp)
            self._emit_measure()
            self.viewport().update()
        elif self.show_rulers:
            # atualiza o indicador de posição nas réguas
            self.viewport().update(0, 0, self.viewport().width(), RULER)
            self.viewport().update(0, 0, RULER, self.viewport().height())
        super().mouseMoveEvent(ev)

    def mouseReleaseEvent(self, ev: QMouseEvent) -> None:  # noqa: N802
        if self._pan_from is not None and ev.button() in (Qt.MouseButton.MiddleButton, Qt.MouseButton.LeftButton):
            self._pan_from = None
            self.viewport().setCursor(Qt.CursorShape.OpenHandCursor if self._space else (
                self._mode_cursor()))
            return
        if ev.button() == Qt.MouseButton.LeftButton and self.mode == "edit" and self._press_pos is not None:
            self._edit_release(ev)
            return
        if ev.button() == Qt.MouseButton.LeftButton and self.mode == "select" and self._press_pos is not None:
            if (ev.position() - self._press_pos).manhattanLength() < 5:
                self.itemClicked.emit(self.pick(ev.position()))
            self._press_pos = None
            return
        super().mouseReleaseEvent(ev)

    def mouseDoubleClickEvent(self, ev: QMouseEvent) -> None:  # noqa: N802
        if ev.button() == Qt.MouseButton.MiddleButton:
            self.fit_all()
            return
        super().mouseDoubleClickEvent(ev)

    def keyPressEvent(self, ev: QKeyEvent) -> None:  # noqa: N802
        k = ev.key()
        if self.mode == "edit" and self._edit_key(ev):
            return
        if k == Qt.Key.Key_Space and not ev.isAutoRepeat():
            if self.sim is not None and self.sim_bar.isVisible() and ev.modifiers() & Qt.KeyboardModifier.ControlModifier:
                self.sim_toggle()
                return
            self._space = True
            self.viewport().setCursor(Qt.CursorShape.OpenHandCursor)
            return
        if k in (Qt.Key.Key_1, Qt.Key.Key_F) and not ev.modifiers():
            self.fit_all()
            return
        if k in (Qt.Key.Key_Plus, Qt.Key.Key_Equal):
            self.zoom_by(1.25)
            return
        if k == Qt.Key.Key_Minus:
            self.zoom_by(0.8)
            return
        if k == Qt.Key.Key_M and not ev.modifiers() and self.mode != "edit":
            self.set_mode("select" if self.mode == "measure" else "measure")
            return
        if k == Qt.Key.Key_Escape:
            if self.mode == "measure":
                self.set_mode("select")
            elif self.sim is not None:
                self.sim_stop()
            else:
                self.itemClicked.emit(-1)
            return
        step = 60
        arrows = {Qt.Key.Key_Left: (-step, 0), Qt.Key.Key_Right: (step, 0),
                  Qt.Key.Key_Up: (0, -step), Qt.Key.Key_Down: (0, step)}
        if k in arrows:
            dx, dy = arrows[k]
            h, v = self.horizontalScrollBar(), self.verticalScrollBar()
            h.setValue(h.value() + dx)
            v.setValue(v.value() + dy)
            return
        super().keyPressEvent(ev)

    def keyReleaseEvent(self, ev: QKeyEvent) -> None:  # noqa: N802
        if ev.key() == Qt.Key.Key_Space and not ev.isAutoRepeat():
            self._space = False
            self.viewport().setCursor(self._mode_cursor())
            return
        super().keyReleaseEvent(ev)

    def leaveEvent(self, ev: Any) -> None:  # noqa: N802
        self.viewport().update()
        super().leaveEvent(ev)

    def resizeEvent(self, ev: Any) -> None:  # noqa: N802
        super().resizeEvent(ev)
        self._place_sim_bar()
        self._place_edit_bar()
        self._place_nav_bar()

    # ------------------------------------------------------------------ seleção / medida
    def pick(self, vp_pos: QPointF) -> int:
        sp = self.mapToScene(vp_pos.toPoint())
        tol = 5.0 / max(self.scale_factor(), 1e-9)
        area = QRectF(sp.x() - tol, sp.y() - tol, 2 * tol, 2 * tol)
        cands = [it for it in self.scene().items(area) if isinstance(it, PathItem) and it.isVisible()
                 and (it.key >= 0 or it.key == PROGRAM_KEY)]
        cands.sort(key=lambda it: -it.zValue())
        for it in cands:
            if it.hit(sp, tol):
                return it.key
        return -1

    def _snap(self, p: QPointF) -> QPointF:
        return p

    def _emit_measure(self) -> None:
        pts = list(self._measure)
        if len(pts) == 1 and self._measure_hover is not None:
            pts.append(self._measure_hover)
        if len(pts) < 2:
            self.measureChanged.emit({"a": (pts[0].x(), pts[0].y())} if pts else None)
            return
        a, b = pts[0], pts[1]
        dx, dy = b.x() - a.x(), b.y() - a.y()
        self.measureChanged.emit({"a": (a.x(), a.y()), "b": (b.x(), b.y()), "dx": dx, "dy": dy,
                                  "dist": math.hypot(dx, dy),
                                  "angle": math.degrees(math.atan2(dy, dx))})

    # ------------------------------------------------------------------ desenho
    def scrollContentsBy(self, dx: int, dy: int) -> None:  # noqa: N802
        super().scrollContentsBy(dx, dy)
        if theme.manager().canvas_gradient() is not None and not self.opengl:
            # o degradê é fixo na tela: a cópia de pixels da rolagem o deslocaria
            self.viewport().update()

    def drawBackground(self, painter: QPainter, rect: QRectF) -> None:  # noqa: N802
        pal = theme.palette()
        grad = theme.manager().canvas_gradient()
        if grad is not None:
            # degradê vertical preso à TELA (não à cena): não "anda" com o zoom/deslocamento
            painter.save()
            painter.resetTransform()
            vp = self.viewport().rect()
            g = QLinearGradient(0, 0, 0, max(1, vp.height()))
            g.setColorAt(0.0, QColor(grad[0]))
            g.setColorAt(1.0, QColor(grad[1]))
            painter.fillRect(vp, QBrush(g))
            painter.restore()
            # grade translúcida: legível tanto no topo claro quanto na base escura do degradê
            ink = QColor(pal.text)
            g_minor, g_major = QColor(ink), QColor(ink)
            g_minor.setAlpha(16 if pal.dark else 20)
            g_major.setAlpha(34 if pal.dark else 42)
        else:
            painter.fillRect(rect, QColor(pal.canvas_bg))
            g_minor, g_major = QColor(pal.grid_minor), QColor(pal.grid_major)
        if not self.show_grid:
            self._draw_axes_lines(painter, rect)
            return
        s = self.scale_factor()
        unit = units.MM_PER_IN if units.display_units() == "in" else 1.0
        minor = _nice_step(12.0 / s / unit) * unit
        mant = round(minor / unit / 10 ** math.floor(math.log10(minor / unit) + 1e-9))
        major = minor * {1: 10, 2: 5, 5: 2}.get(mant, 10)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing, False)
        for step, col in ((minor, g_minor), (major, g_major)):
            if step * s < 6:
                continue
            x0 = math.floor(rect.left() / step) * step
            y0 = math.floor(rect.top() / step) * step
            nx = int((rect.right() - x0) / step) + 1
            ny = int((rect.bottom() - y0) / step) + 1
            if nx > 2000 or ny > 2000:
                continue
            lines = [QLineF(x0 + i * step, rect.top(), x0 + i * step, rect.bottom()) for i in range(nx)]
            lines += [QLineF(rect.left(), y0 + j * step, rect.right(), y0 + j * step) for j in range(ny)]
            painter.setPen(_cosmetic(QColor(col), 1.0))
            painter.drawLines(lines)
        self._draw_axes_lines(painter, rect)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing, self.antialias)

    def _draw_axes_lines(self, painter: QPainter, rect: QRectF) -> None:
        pal = theme.palette()
        cx, cy = QColor(pal.axis_x), QColor(pal.axis_y)
        cx.setAlphaF(0.45)
        cy.setAlphaF(0.45)
        painter.setPen(_cosmetic(cx, 1.0))
        painter.drawLine(QLineF(rect.left(), 0, rect.right(), 0))
        painter.setPen(_cosmetic(cy, 1.0))
        painter.drawLine(QLineF(0, rect.top(), 0, rect.bottom()))

    def drawForeground(self, painter: QPainter, rect: QRectF) -> None:  # noqa: N802
        painter.save()
        painter.resetTransform()
        painter.setRenderHint(QPainter.RenderHint.Antialiasing, True)
        self._draw_origin(painter)
        if self._measure:
            self._draw_measure(painter)
        if self.mode == "edit" and self._band is not None:
            self._draw_band(painter)
        if self.show_rulers:
            self._draw_rulers(painter)
        painter.restore()

    def _draw_origin(self, painter: QPainter) -> None:
        pal = theme.palette()
        o = self.mapFromScene(QPointF(0, 0))
        vp = self.viewport().rect().adjusted(-60, -60, 60, 60)
        if not vp.contains(o):
            return
        L = 34
        for col, (dx, dy), lab in ((pal.axis_x, (L, 0), "X"), (pal.axis_y, (0, -L), "Y")):
            c = QColor(col)
            painter.setPen(QPen(c, 2, Qt.PenStyle.SolidLine, Qt.PenCapStyle.RoundCap))
            end = QPointF(o.x() + dx, o.y() + dy)
            painter.drawLine(QPointF(o), end)
            ang = math.atan2(dy, dx)
            for da in (2.6, -2.6):
                painter.drawLine(end, QPointF(end.x() + 7 * math.cos(ang + da), end.y() + 7 * math.sin(ang + da)))
            f = painter.font()
            f.setPointSizeF(max(7.0, f.pointSizeF() * 0.85))
            f.setBold(True)
            painter.setFont(f)
            painter.drawText(QPointF(end.x() + (6 if dx else -4), end.y() + (4 if dx else -6)), lab)
        painter.setPen(QPen(QColor(pal.text_muted), 1.5))
        painter.setBrush(QColor(pal.canvas_bg))
        painter.drawEllipse(QPointF(o), 4, 4)

    def _draw_measure(self, painter: QPainter) -> None:
        pal = theme.palette()
        pts = list(self._measure)
        if len(pts) == 1 and self._measure_hover is not None:
            pts.append(self._measure_hover)
        col = QColor(pal.measure)
        vp_pts = [QPointF(self.mapFromScene(p)) for p in pts]
        painter.setPen(QPen(col, 1.6, Qt.PenStyle.DashLine))
        if len(vp_pts) == 2:
            painter.drawLine(vp_pts[0], vp_pts[1])
        painter.setPen(QPen(col, 2))
        painter.setBrush(QColor(pal.canvas_bg))
        for p in vp_pts:
            painter.drawEllipse(p, 4, 4)
        if len(pts) == 2:
            dx, dy = pts[1].x() - pts[0].x(), pts[1].y() - pts[0].y()
            d = math.hypot(dx, dy)
            txt = f"{units.fmt_len(d)}   ΔX {units.fmt_len(dx, with_unit=False)}  ΔY {units.fmt_len(dy, with_unit=False)}"
            mid = (vp_pts[0] + vp_pts[1]) / 2
            fm = QFontMetrics(painter.font())
            w = fm.horizontalAdvance(txt) + 16
            h = fm.height() + 8
            box = QRectF(mid.x() - w / 2, mid.y() - h - 10, w, h)
            painter.setPen(QPen(col, 1))
            painter.setBrush(QColor(pal.elevated))
            painter.drawRoundedRect(box, 6, 6)
            painter.setPen(QColor(pal.text))
            painter.drawText(box, Qt.AlignmentFlag.AlignCenter, txt)

    def _draw_rulers(self, painter: QPainter) -> None:
        pal = theme.palette()
        vp = self.viewport().rect()
        bg = QColor(pal.surface)
        painter.setPen(Qt.PenStyle.NoPen)
        painter.setBrush(bg)
        painter.drawRect(QRectF(0, 0, vp.width(), RULER))
        painter.drawRect(QRectF(0, 0, RULER, vp.height()))
        painter.setPen(QPen(QColor(pal.border), 1))
        painter.drawLine(QPointF(0, RULER - 0.5), QPointF(vp.width(), RULER - 0.5))
        painter.drawLine(QPointF(RULER - 0.5, 0), QPointF(RULER - 0.5, vp.height()))

        s = self.scale_factor()
        unit = units.MM_PER_IN if units.display_units() == "in" else 1.0
        major = _nice_step(70.0 / s / unit) * unit
        minor = major / 10 if major * s / 10 >= 5 else major / 5 if major * s / 5 >= 5 else major / 2
        f = QFont(painter.font())
        f.setPointSizeF(max(6.5, f.pointSizeF() * 0.78))
        painter.setFont(f)
        fm = QFontMetrics(f)
        tl = self.mapToScene(0, 0)
        br = self.mapToScene(vp.width(), vp.height())
        x_min, x_max = min(tl.x(), br.x()), max(tl.x(), br.x())
        y_min, y_max = min(tl.y(), br.y()), max(tl.y(), br.y())
        tick = QColor(pal.text_muted)
        dec = max(0, -int(math.floor(math.log10(major / unit) + 1e-9)))

        def label(v: float) -> str:
            val = v / unit
            if abs(val) < 1e-9:
                val = 0.0
            return f"{val:.{dec}f}".replace(".", ",")

        # régua horizontal
        painter.setPen(QPen(tick, 1))
        x = math.floor(x_min / minor) * minor
        while x <= x_max:
            px = self.mapFromScene(QPointF(x, 0)).x()
            is_major = abs(x / major - round(x / major)) < 1e-6
            if px > RULER:
                painter.drawLine(QPointF(px + 0.5, RULER - (9 if is_major else 4)), QPointF(px + 0.5, RULER))
                if is_major:
                    painter.drawText(QPointF(px + 3, fm.ascent() + 2), label(x))
            x += minor
        # régua vertical (texto girado)
        y = math.floor(y_min / minor) * minor
        while y <= y_max:
            py = self.mapFromScene(QPointF(0, y)).y()
            is_major = abs(y / major - round(y / major)) < 1e-6
            if py > RULER:
                painter.drawLine(QPointF(RULER - (9 if is_major else 4), py + 0.5), QPointF(RULER, py + 0.5))
                if is_major:
                    painter.save()
                    painter.translate(fm.ascent() + 2, py - 3)
                    painter.rotate(-90)
                    painter.drawText(QPointF(0, 0), label(y))
                    painter.restore()
            y += minor
        # indicador do cursor
        cur = self.mapFromScene(self._cursor_scene)
        acc = QColor(pal.accent)
        painter.setPen(QPen(acc, 1.5))
        if cur.x() > RULER:
            painter.drawLine(QPointF(cur.x() + 0.5, 0), QPointF(cur.x() + 0.5, RULER))
        if cur.y() > RULER:
            painter.drawLine(QPointF(0, cur.y() + 0.5), QPointF(RULER, cur.y() + 0.5))
        # canto com a unidade
        painter.setPen(Qt.PenStyle.NoPen)
        painter.setBrush(bg)
        painter.drawRect(QRectF(0, 0, RULER, RULER))
        painter.setPen(QColor(pal.text_muted))
        painter.drawText(QRectF(0, 0, RULER, RULER), Qt.AlignmentFlag.AlignCenter, units.unit_label())


    # ------------------------------------------------------------------ edição de caminhos
    def _mode_cursor(self) -> Qt.CursorShape:
        if self.mode == "measure":
            return Qt.CursorShape.CrossCursor
        if self.mode == "edit":
            return Qt.CursorShape.CrossCursor if self.edit_tool == "trim" else Qt.CursorShape.PointingHandCursor
        return Qt.CursorShape.ArrowCursor

    def edit_start(self, key: int, name: str, ops: list) -> None:
        """Entra no modo de edição dos caminhos do job ``key``."""
        from brokcam.cam import edit as cam_edit
        self.sim_stop()
        self.edit_key = key
        self.edit_name = name
        self.edit_ops = list(ops)
        self.edit_index = cam_edit.SegmentIndex.build(self.edit_ops)
        self.edit_sel = set()
        self.edit_hover = None
        self.edit_tool = "select"
        self.set_mode("edit")
        self.edit_overlay.show()
        self.edit_bar.show()
        self._edit_refresh()
        self.setFocus(Qt.FocusReason.OtherFocusReason)
        self.editModeChanged.emit(True)

    def edit_set_ops(self, ops: list, keep_selection: bool = False) -> None:
        """Atualiza os caminhos em edição (após uma alteração ou desfazer)."""
        from brokcam.cam import edit as cam_edit
        if self.edit_key is None:
            return
        self.edit_ops = list(ops)
        self.edit_index = cam_edit.SegmentIndex.build(self.edit_ops)
        if not keep_selection:
            self.edit_sel = set()
        self.edit_hover = None
        self._edit_refresh()

    def edit_stop(self) -> None:
        if self.edit_key is None:
            return
        self.edit_key = None
        self.edit_ops = []
        self.edit_index = None
        self.edit_sel = set()
        self.edit_hover = None
        self._band = None
        self.edit_overlay.set_state([], set(), None)
        self.edit_overlay.hide()
        self.edit_bar.hide()
        self.set_mode("select")
        self.editModeChanged.emit(False)

    def edit_set_tool(self, tool: str) -> None:
        self.edit_tool = tool
        self._band = None
        self.edit_hover = None
        self.viewport().setCursor(self._mode_cursor())
        self._edit_refresh()

    def edit_select_all(self) -> None:
        from brokcam.cam import edit as cam_edit
        self.edit_sel = set(cam_edit.all_refs(self.edit_ops))
        self._edit_refresh()

    def edit_invert(self) -> None:
        from brokcam.cam import edit as cam_edit
        self.edit_sel = set(cam_edit.all_refs(self.edit_ops)) - self.edit_sel
        self._edit_refresh()

    def edit_request(self, action: str, payload: Any = None) -> None:
        if self.edit_key is None:
            return
        if action in ("delete", "keep", "reverse") and not self.edit_sel:
            return
        self.editAction.emit(self.edit_key, action, payload if payload is not None else set(self.edit_sel))

    def _edit_pick(self, vp_pos: QPointF) -> Any:
        from brokcam.cam import edit as cam_edit
        if not self.edit_ops or self.edit_tool != "select":
            return None
        sp = self.mapToScene(vp_pos.toPoint())
        tol = 6.0 / max(self.scale_factor(), 1e-9)
        return cam_edit.pick(self.edit_ops, sp.x(), sp.y(), tol, self.edit_index)

    def _edit_release(self, ev: QMouseEvent) -> None:
        from brokcam.cam import edit as cam_edit
        add = bool(ev.modifiers() & (Qt.KeyboardModifier.ShiftModifier | Qt.KeyboardModifier.ControlModifier))
        band = self._band
        self._band = None
        self._press_pos = None
        if band is None:
            if self.edit_tool != "select":
                return
            ref = self._edit_pick(ev.position())
            if ref is None:
                if not add:
                    self.edit_sel = set()
            elif add:
                self.edit_sel ^= {ref}
            else:
                self.edit_sel = {ref}
            self._edit_refresh()
            return
        a = self.mapToScene(band[0].toPoint())
        b = self.mapToScene(band[1].toPoint())
        self.viewport().update()
        if self.edit_tool == "trim":
            self.edit_request("trim", (a.x(), a.y(), b.x(), b.y()))
            return
        mode = "inside" if band[1].x() >= band[0].x() else "touch"
        refs = cam_edit.select_rect(self.edit_ops, a.x(), a.y(), b.x(), b.y(), mode)
        self.edit_sel = (self.edit_sel | refs) if add else refs
        self._edit_refresh()

    def _edit_key(self, ev: QKeyEvent) -> bool:
        k = ev.key()
        mods = ev.modifiers()
        if k in (Qt.Key.Key_Delete, Qt.Key.Key_Backspace):
            self.edit_request("delete")
        elif k == Qt.Key.Key_A and mods & Qt.KeyboardModifier.ControlModifier:
            self.edit_select_all()
        elif k == Qt.Key.Key_I and not mods:
            self.edit_invert()
        elif k == Qt.Key.Key_T and not mods:
            self.edit_set_tool("select" if self.edit_tool == "trim" else "trim")
        elif k == Qt.Key.Key_S and not mods:
            self.edit_set_tool("select")
        elif k == Qt.Key.Key_R and not mods:
            self.edit_request("reverse")
        elif k == Qt.Key.Key_Escape:
            if self._band is not None:
                self._band = None
                self.viewport().update()
            elif self.edit_sel:
                self.edit_sel = set()
                self._edit_refresh()
            else:
                self.edit_stop()
        elif k in (Qt.Key.Key_Return, Qt.Key.Key_Enter):
            self.edit_stop()
        else:
            return False
        return True

    def _edit_refresh(self, bar: bool = True) -> None:
        self.edit_overlay.set_state(self.edit_ops, self.edit_sel, self.edit_hover)
        if bar:
            n_paths = sum(len(op.paths) for op in self.edit_ops)
            n_holes = sum(len(np.asarray(op.points).reshape(-1, 2)) for op in self.edit_ops)
            self.edit_bar.sync(self.edit_name, len(self.edit_sel), n_paths, n_holes, self.edit_tool)
            self._place_edit_bar()
        self.viewport().update()

    def _place_nav_bar(self) -> None:
        nb = self.nav_bar
        nb.zoom.setText(f"{self.scale_factor():.1f} px/mm")
        nb.adjustSize()
        sz = nb.sizeHint()
        nb.setGeometry(self.width() - sz.width() - 12, self.height() - sz.height() - 12, sz.width(), sz.height())
        nb.raise_()

    def _place_edit_bar(self) -> None:
        if not self.edit_bar.isVisible():
            return
        self.edit_bar.set_compact(False)
        self.edit_bar.adjustSize()
        if self.edit_bar.sizeHint().width() > self.width() - 32:
            self.edit_bar.set_compact(True)
            self.edit_bar.adjustSize()
        sz: QSize = self.edit_bar.sizeHint()
        w = min(sz.width(), self.width() - 32)
        top = RULER + 10 if self.show_rulers else 10
        self.edit_bar.setGeometry((self.width() - w) // 2, top, w, sz.height())

    def _draw_band(self, painter: QPainter) -> None:
        assert self._band is not None
        pal = theme.palette()
        a, b = self._band
        r = QRectF(a, b).normalized()
        if self.edit_tool == "trim":
            col, style, txt = QColor(pal.danger), Qt.PenStyle.SolidLine, "Apagar trecho"
        elif b.x() >= a.x():
            col, style, txt = QColor(pal.accent), Qt.PenStyle.SolidLine, "Totalmente dentro"
        else:
            col, style, txt = QColor(pal.success), Qt.PenStyle.DashLine, "Tocando"
        fill = QColor(col)
        fill.setAlphaF(0.12)
        painter.setPen(QPen(col, 1.5, style))
        painter.setBrush(fill)
        painter.drawRect(r)
        painter.setPen(col)
        painter.drawText(r.topLeft() + QPointF(4, -6), txt)

    # ------------------------------------------------------------------ simulação
    def sim_start(self, jobs: list[Job], rapid_rate: float = 1000.0) -> bool:
        self.sim_stop()
        sim = Simulation(jobs, rapid_rate)
        if sim.total <= 0:
            return False
        self.sim = sim
        self.sim_keys = {j.id for j in jobs}
        self.sim_item = SimTrailItem(sim)
        self.scene().addItem(self.sim_item)
        self.sim_t = 0.0
        self.sim_running = True
        self.sim_bar.show()
        self._place_sim_bar()
        self.sim_timer.start()
        self._sim_apply()
        return True

    def sim_toggle(self) -> None:
        if self.sim is None:
            return
        if self.sim_t >= self.sim.total:
            self.sim_t = 0.0
        self.sim_running = not self.sim_running
        if self.sim_running:
            self.sim_timer.start()
        else:
            self.sim_timer.stop()
        self._sim_apply()

    def sim_seek(self, t: float) -> None:
        if self.sim is None:
            return
        self.sim_t = max(0.0, min(self.sim.total, t))
        self._sim_apply()

    def sim_stop(self) -> None:
        self.sim_timer.stop()
        self.sim_running = False
        if self.sim_item is not None:
            self.scene().removeItem(self.sim_item)
        self.sim_item = None
        self.sim = None
        self.sim_keys = set()
        self.sim_bar.hide()
        self.tool_marker.hide()

    def _sim_tick(self) -> None:
        if self.sim is None:
            self.sim_timer.stop()
            return
        self.sim_t += self.sim_timer.interval() / 1000.0 * self.sim_bar.speed_factor()
        if self.sim_t >= self.sim.total:
            self.sim_t = self.sim.total
            self.sim_running = False
            self.sim_timer.stop()
        self._sim_apply()

    def _sim_apply(self) -> None:
        if self.sim is None or self.sim_item is None:
            return
        self.sim_item.set_time(self.sim_t)
        x, y = self.sim.position(self.sim_t)
        self.set_tool_position(x, y, live=False)
        self.sim_bar.sync(self.sim_t, self.sim.total, self.sim_running)

    def _place_sim_bar(self) -> None:
        if not self.sim_bar.isVisible():
            return
        self.sim_bar.adjustSize()
        sz: QSize = self.sim_bar.sizeHint()
        w = min(sz.width(), self.width() - 32)
        self.sim_bar.setGeometry((self.width() - w) // 2, self.height() - sz.height() - 16, w, sz.height())

