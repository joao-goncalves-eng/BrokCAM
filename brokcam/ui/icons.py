"""Ícones SVG monocromáticos que acompanham o tema.

Os arquivos em ``icons/`` usam ``currentColor``; o :class:`ThemedIconEngine` troca
essa cor pela cor do tema atual no momento da pintura (normal, desabilitado,
selecionado/ativo), então os ícones mudam junto com o tema sem recriar QIcons.
"""
from __future__ import annotations

from functools import lru_cache
from pathlib import Path

from PySide6.QtCore import QByteArray, QPoint, QRect, QRectF, QSize, Qt
from PySide6.QtGui import QIcon, QIconEngine, QPainter, QPixmap
from PySide6.QtSvg import QSvgRenderer

from . import theme

ICON_DIR = Path(__file__).with_name("icons")


@lru_cache(maxsize=None)
def _svg_source(name: str) -> str:
    path = ICON_DIR / f"{name}.svg"
    if not path.exists():
        path = ICON_DIR / "dot.svg"
    return path.read_text(encoding="utf-8")


@lru_cache(maxsize=2048)
def _renderer(name: str, color: str) -> QSvgRenderer:
    data = QByteArray(_svg_source(name).replace("currentColor", color).encode("utf-8"))
    return QSvgRenderer(data)


class ThemedIconEngine(QIconEngine):
    def __init__(self, name: str, token: str | None = None) -> None:
        super().__init__()
        self.name = name
        self.token = token  # cor fixa de um token (ex.: "danger"), se informado

    def _color(self, mode: QIcon.Mode, state: QIcon.State) -> str:
        p = theme.palette()
        if mode == QIcon.Mode.Disabled:
            return p.text_disabled
        if self.token:
            return getattr(p, self.token)
        if mode == QIcon.Mode.Selected:
            return p.selection_text
        return p.text

    def paint(self, painter: QPainter, rect: QRect, mode: QIcon.Mode, state: QIcon.State) -> None:
        _renderer(self.name, self._color(mode, state)).render(painter, QRectF(rect))

    def pixmap(self, size: QSize, mode: QIcon.Mode, state: QIcon.State) -> QPixmap:
        pm = QPixmap(size)
        pm.fill(Qt.GlobalColor.transparent)
        p = QPainter(pm)
        p.setRenderHint(QPainter.RenderHint.Antialiasing)
        self.paint(p, QRect(QPoint(0, 0), size), mode, state)
        p.end()
        return pm

    def scaledPixmap(self, size: QSize, mode: QIcon.Mode, state: QIcon.State, scale: float) -> QPixmap:  # noqa: N802
        px = QSize(max(1, round(size.width() * scale)), max(1, round(size.height() * scale)))
        pm = self.pixmap(px, mode, state)
        pm.setDevicePixelRatio(scale)
        return pm

    def clone(self) -> QIconEngine:
        return ThemedIconEngine(self.name, self.token)

    def key(self) -> str:
        return "brokcam-themed"


def icon(name: str, token: str | None = None) -> QIcon:
    """Retorna o ícone ``name`` (arquivo ``icons/<name>.svg``) recolorido pelo tema."""
    return QIcon(ThemedIconEngine(name, token))


def pixmap(name: str, size: int, color: str, dpr: float = 1.0) -> QPixmap:
    """Pixmap estático de um ícone numa cor específica."""
    px = max(1, round(size * dpr))
    pm = QPixmap(px, px)
    pm.fill(Qt.GlobalColor.transparent)
    p = QPainter(pm)
    p.setRenderHint(QPainter.RenderHint.Antialiasing)
    _renderer(name, color).render(p)
    p.end()
    pm.setDevicePixelRatio(dpr)
    return pm


def available() -> list[str]:
    return sorted(p.stem for p in ICON_DIR.glob("*.svg"))
