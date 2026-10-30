"""Tela inicial exibida quando o projeto está vazio."""
from __future__ import annotations

import os
from pathlib import Path

from PySide6.QtCore import QSize, Qt, Signal
from PySide6.QtWidgets import (
    QFrame, QHBoxLayout, QLabel, QPushButton, QSizePolicy, QVBoxLayout, QWidget,
)

from . import theme
from .icons import icon, pixmap

EXAMPLE_EXTS = {".gbr", ".ger", ".gtl", ".gbl", ".gko", ".gm1", ".gml", ".drl", ".xln", ".exc",
                ".cmp", ".sol", ".gto", ".gbo", ".gts", ".gbs", ".txt", ".drd", ".art", ".pho"}


def example_dir() -> Path:
    return Path(__file__).resolve().parents[2] / "tests" / "data"


def example_files() -> list[str]:
    d = example_dir()
    if not d.is_dir():
        return []
    files = [p for p in d.rglob("*") if p.is_file() and p.suffix.lower() in EXAMPLE_EXTS]
    # prefere um conjunto coerente de uma única placa (ex.: board-*.gbr/drl do KiCad)
    groups: dict[str, list[Path]] = {}
    for f in files:
        groups.setdefault(f.stem.split("-")[0].split("_")[0].lower(), []).append(f)
    best = max(groups.values(), key=len) if groups else files
    return [str(p) for p in sorted(best)]


class WelcomeView(QWidget):
    openRequested = Signal()
    wizardRequested = Signal()
    exampleRequested = Signal()
    recentRequested = Signal(str)

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setObjectName("Welcome")
        self.setAutoFillBackground(True)
        outer = QVBoxLayout(self)
        outer.setContentsMargins(24, 24, 24, 24)
        outer.addStretch(1)
        row = QHBoxLayout()
        row.addStretch(1)
        card = QFrame()
        card.setObjectName("WelcomeCard")
        card.setMaximumWidth(620)
        card.setSizePolicy(QSizePolicy.Policy.Preferred, QSizePolicy.Policy.Maximum)
        lay = QVBoxLayout(card)
        lay.setContentsMargins(36, 32, 36, 28)
        lay.setSpacing(14)

        head = QHBoxLayout()
        head.setSpacing(14)
        self.logo = QLabel()
        head.addWidget(self.logo, 0, Qt.AlignmentFlag.AlignTop)
        tcol = QVBoxLayout()
        tcol.setSpacing(2)
        title = QLabel("Bem-vindo ao BrokCAM")
        title.setObjectName("Heading")
        title.setStyleSheet("font-size: 18pt;")
        sub = QLabel("Da placa (Gerber/Excellon) ao G-code GRBL: isolamento, furação e recorte.")
        sub.setObjectName("Muted")
        sub.setWordWrap(True)
        tcol.addWidget(title)
        tcol.addWidget(sub)
        head.addLayout(tcol, 1)
        lay.addLayout(head)

        self.drop = QLabel("Arraste e solte aqui arquivos Gerber, Excellon ou um projeto .bcam")
        self.drop.setObjectName("DropHint")
        self.drop.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.drop.setWordWrap(True)
        self.drop.setMinimumHeight(84)
        lay.addWidget(self.drop)

        btns = QHBoxLayout()
        btns.setSpacing(8)
        self.open_btn = QPushButton("&Abrir arquivos…")
        self.open_btn.setIcon(icon("open", "accent_text"))
        self.open_btn.setProperty("primary", True)
        self.open_btn.setMinimumHeight(36)
        self.open_btn.setToolTip("Abrir arquivos Gerber, Excellon ou projeto (Ctrl+O)")
        self.open_btn.clicked.connect(self.openRequested.emit)
        self.wizard_btn = QPushButton("Assis&tente em 4 passos")
        self.wizard_btn.setIcon(icon("wizard"))
        self.wizard_btn.setMinimumHeight(36)
        self.wizard_btn.setToolTip("Importa, configura, gera todos os caminhos e exporta o G-code (Ctrl+Shift+W)")
        self.wizard_btn.clicked.connect(self.wizardRequested.emit)
        self.example_btn = QPushButton("Abrir &exemplo")
        self.example_btn.setIcon(icon("board"))
        self.example_btn.setMinimumHeight(36)
        self.example_btn.clicked.connect(self.exampleRequested.emit)
        for b in (self.open_btn, self.wizard_btn, self.example_btn):
            b.setAccessibleName(b.text().replace("&", "").replace("…", ""))
            btns.addWidget(b)
        lay.addLayout(btns)

        self.recent_title = QLabel("RECENTES")
        self.recent_title.setObjectName("SectionTitle")
        lay.addWidget(self.recent_title)
        self.recent_box = QVBoxLayout()
        self.recent_box.setSpacing(0)
        lay.addLayout(self.recent_box)

        tip = QLabel("Dica: pressione F1 para o guia rápido. Toda opção tem explicação — "
                     "foque um campo e leia o quadro de ajuda.")
        tip.setObjectName("Muted")
        tip.setWordWrap(True)
        lay.addWidget(tip)

        row.addWidget(card)
        row.addStretch(1)
        outer.addLayout(row)
        outer.addStretch(2)
        theme.on_change(self, lambda _p: self._refresh_theme())
        self._refresh_theme()
        self.set_recent([])
        self.update_example()

    def _refresh_theme(self) -> None:
        p = theme.palette()
        self.logo.setPixmap(pixmap("board", 44, p.accent, self.devicePixelRatioF()))
        self.setStyleSheet(f"#Welcome {{ background: {p.canvas_bg}; }}")

    def update_example(self) -> None:
        files = example_files()
        self.example_btn.setEnabled(bool(files))
        self.example_btn.setToolTip(
            f"Carregar os {len(files)} arquivos de exemplo de tests/data" if files
            else "Nenhum arquivo de exemplo encontrado em tests/data")

    def set_recent(self, paths: list[str]) -> None:
        while self.recent_box.count():
            it = self.recent_box.takeAt(0)
            if it.widget():
                it.widget().deleteLater()
        paths = [p for p in paths if os.path.exists(p)][:5]
        self.recent_title.setVisible(bool(paths))
        for p in paths:
            b = QPushButton(f"{os.path.basename(p)}   —   {os.path.dirname(p)}")
            b.setProperty("link", True)
            b.setIcon(icon("gcode" if p.lower().endswith((".nc", ".gcode")) else
                           "layers" if p.lower().endswith((".bcam", ".tcam")) else "copper"))
            b.setIconSize(QSize(16, 16))
            b.setToolTip(p)
            b.setAccessibleName(f"Abrir recente {os.path.basename(p)}")
            b.setStyleSheet("text-align: left;")
            b.clicked.connect(lambda _=False, path=p: self.recentRequested.emit(path))
            self.recent_box.addWidget(b)

    def set_drag_active(self, on: bool) -> None:
        p = theme.palette()
        self.drop.setStyleSheet(f"#DropHint {{ border-color: {p.accent}; color: {p.text}; }}" if on else "")
