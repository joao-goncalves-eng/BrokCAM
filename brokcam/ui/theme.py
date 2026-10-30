"""Temas do BrokCAM: paletas escura/clara/alto contraste, QSS e tipografia.

Todos os componentes da interface leem cores de :func:`palette` (tokens semânticos)
em vez de usar cores fixas. A troca de tema emite :attr:`ThemeManager.changed` e os
widgets que desenham por conta própria (canvas, editor de G-code, ícones) se
atualizam ao receber o sinal.
"""
from __future__ import annotations

import hashlib
import tempfile
from dataclasses import dataclass
from pathlib import Path

from PySide6.QtCore import QObject, Qt, Signal
from PySide6.QtGui import QColor, QFont, QFontDatabase, QGuiApplication, QPalette
from PySide6.QtWidgets import QApplication

ICON_DIR = Path(__file__).with_name("icons")

BASE_POINT_SIZE = 9.5   # tamanho base em pt (100%)
SPACING = 8             # grade de espaçamento da interface (px)


@dataclass(frozen=True)
class Palette:
    """Tokens de cor semânticos. Todas as combinações texto/fundo atendem WCAG AA."""
    name: str
    dark: bool
    window: str          # fundo da janela / áreas vazias
    surface: str         # painéis (docks, formulários)
    surface_alt: str     # campos, linhas alternadas, cabeçalhos
    elevated: str        # menus, tooltips, popups
    hover: str
    border: str
    border_strong: str
    text: str
    text_muted: str
    text_disabled: str
    accent: str
    accent_hover: str
    accent_text: str     # texto sobre o accent
    selection: str       # fundo de item selecionado
    selection_text: str
    focus: str
    danger: str
    warning: str
    success: str
    info: str
    # canvas
    canvas_bg: str
    grid_minor: str
    grid_major: str
    axis_x: str
    axis_y: str
    cut: str
    rapid: str
    done: str
    tool: str
    measure: str
    highlight_line: str  # linha atual no editor de G-code


DARK = Palette(
    name="escuro", dark=True,
    window="#14161a", surface="#1b1e24", surface_alt="#22262d", elevated="#262a32",
    hover="#2b3039", border="#2e333c", border_strong="#434a56",
    text="#e7e9ee", text_muted="#a2a9b5", text_disabled="#6b7280",
    accent="#4d9bff", accent_hover="#6aabff", accent_text="#0b1220",
    selection="#24406b", selection_text="#ffffff", focus="#7cb6ff",
    danger="#ff6b6b", warning="#f2b84b", success="#4cc38a", info="#62b0ff",
    canvas_bg="#0f1115", grid_minor="#1a1d23", grid_major="#262a32",
    axis_x="#e5534b", axis_y="#46c26f",
    cut="#38d0e8", rapid="#d16ad8", done="#ffd24a", tool="#ffffff", measure="#ffd24a",
    highlight_line="#26344a",
)

LIGHT = Palette(
    name="claro", dark=False,
    window="#eceef2", surface="#ffffff", surface_alt="#f4f5f8", elevated="#ffffff",
    hover="#eaedf2", border="#d9dde4", border_strong="#b9c0cb",
    text="#1a1e26", text_muted="#555e6c", text_disabled="#9aa1ac",
    accent="#1764d8", accent_hover="#0f56c0", accent_text="#ffffff",
    selection="#d6e6ff", selection_text="#0b1f44", focus="#1764d8",
    danger="#c62828", warning="#9a6200", success="#1b7f4b", info="#1565c0",
    canvas_bg="#fbfbfc", grid_minor="#eff1f4", grid_major="#dde1e7",
    axis_x="#d32f2f", axis_y="#2e7d32",
    cut="#0b7fa8", rapid="#a23ab0", done="#d97706", tool="#111111", measure="#c2410c",
    highlight_line="#e3edff",
)

HIGH_CONTRAST = Palette(
    name="alto contraste", dark=True,
    window="#000000", surface="#000000", surface_alt="#0d0d0d", elevated="#000000",
    hover="#1f1f1f", border="#ffffff", border_strong="#ffffff",
    text="#ffffff", text_muted="#f0f0f0", text_disabled="#9a9a9a",
    accent="#ffd400", accent_hover="#ffe34d", accent_text="#000000",
    selection="#ffd400", selection_text="#000000", focus="#00e5ff",
    danger="#ff5c5c", warning="#ffd400", success="#3dff8b", info="#5cd6ff",
    canvas_bg="#000000", grid_minor="#1a1a1a", grid_major="#3a3a3a",
    axis_x="#ff4040", axis_y="#40ff70",
    cut="#00e5ff", rapid="#ff5cff", done="#ffd400", tool="#ffffff", measure="#ffd400",
    highlight_line="#333300",
)

# Cores padrão por papel de camada (boa leitura em fundo escuro e claro).
ROLE_COLORS: dict[str, str] = {
    "top_copper": "#e08a3c",
    "bottom_copper": "#4f9cf0",
    "outline": "#d4b62c",
    "drill": "#b07cf0",
    "silk": "#9aa3b0",
    "mask": "#3fae78",
    "paste": "#8d9399",
    "unknown": "#8fa6bf",
    "job": "#38d0e8",
}

ROLE_LABELS: dict[str, str] = {
    "top_copper": "Cobre superior",
    "bottom_copper": "Cobre inferior",
    "outline": "Contorno da placa",
    "drill": "Furação",
    "silk": "Serigrafia",
    "mask": "Máscara de solda",
    "paste": "Pasta de solda",
    "unknown": "Desconhecido",
}


def _first_available(families: list[str], fallback: str) -> str:
    available = set(QFontDatabase.families())
    for fam in families:
        if fam in available:
            return fam
    return fallback


def ui_font_family() -> str:
    return _first_available(
        ["Segoe UI Variable Text", "Segoe UI Variable", "Inter", "Segoe UI",
         "SF Pro Text", "Noto Sans", "Ubuntu", "DejaVu Sans"],
        QApplication.font().family() if QApplication.instance() else "Sans Serif")


def mono_font_family() -> str:
    return _first_available(
        ["Cascadia Mono", "Cascadia Code", "JetBrains Mono", "Consolas",
         "SF Mono", "DejaVu Sans Mono", "Courier New"], "Monospace")


def mono_font(scale: float = 1.0, delta: float = 0.0) -> QFont:
    f = QFont(mono_font_family())
    f.setStyleHint(QFont.StyleHint.Monospace)
    f.setPointSizeF((BASE_POINT_SIZE + delta) * scale)
    return f


def _colored_svg(name: str, color: str, cache_dir: Path) -> str:
    src = (ICON_DIR / f"{name}.svg").read_text(encoding="utf-8")
    out = cache_dir / f"{name}-{color.lstrip('#')}.svg"
    if not out.exists():
        out.write_text(src.replace("currentColor", color), encoding="utf-8")
    return out.as_posix()


def build_qss(p: Palette, scale: float) -> str:
    """Gera a folha de estilos para a paleta ``p``."""
    cache = Path(tempfile.gettempdir()) / "brokcam-theme" / hashlib.md5(
        p.name.encode()).hexdigest()[:8]
    cache.mkdir(parents=True, exist_ok=True)
    chev = _colored_svg("chevron-down", p.text_muted, cache)
    chev_r = _colored_svg("chevron-right", p.text_muted, cache)
    check = _colored_svg("check", p.accent_text, cache)
    s = lambda v: max(1, round(v * scale))  # noqa: E731
    hc = p.name == "alto contraste"
    bw = 2 if hc else 1
    up_svg = cache / f"chevron-up-{p.text_muted.lstrip('#')}.svg"
    if not up_svg.exists():
        up_svg.write_text((ICON_DIR / "chevron-down.svg").read_text(encoding="utf-8")
                          .replace("currentColor", p.text_muted)
                          .replace('d="m6 9 6 6 6-6"', 'd="m6 15 6-6 6 6"'), encoding="utf-8")
    up = up_svg.as_posix()
    return f"""
* {{ outline: none; }}
QWidget {{ color: {p.text}; selection-background-color: {p.selection}; selection-color: {p.selection_text}; }}
QMainWindow, QDialog, QWizard {{ background: {p.window}; }}
QMainWindow::separator {{ background: {p.window}; width: {s(4)}px; height: {s(4)}px; }}
QMainWindow::separator:hover {{ background: {p.accent}; }}

/* ---------- docks ---------- */
QDockWidget {{ titlebar-close-icon: none; color: {p.text_muted}; }}
QDockWidget::title {{ background: {p.surface}; padding: {s(8)}px {s(12)}px; border-bottom: {bw}px solid {p.border};
    text-align: left; }}
QDockWidget > QWidget {{ background: {p.surface}; }}
#Panel, #PanelBody {{ background: {p.surface}; }}
#SectionTitle {{ color: {p.text_muted}; font-weight: 600; letter-spacing: 0.4px; text-transform: uppercase;
    font-size: {s(8)}pt; padding-top: {s(4)}px; }}
#Heading {{ font-size: {s(13)}pt; font-weight: 600; }}
#SubHeading {{ color: {p.text_muted}; }}
#Muted {{ color: {p.text_muted}; }}
#Card {{ background: {p.surface_alt}; border: {bw}px solid {p.border}; border-radius: 8px; }}
#HelpPanel {{ background: {p.surface_alt}; border: {bw}px solid {p.border}; border-radius: 8px; }}
#HelpTitle {{ font-weight: 600; }}
#Badge {{ background: {p.surface_alt}; border: {bw}px solid {p.border}; border-radius: 9px; padding: 1px 8px;
    color: {p.text_muted}; font-size: {s(8)}pt; }}

/* ---------- barras ---------- */
QMenuBar {{ background: {p.surface}; border-bottom: {bw}px solid {p.border}; padding: 2px 4px; }}
QMenuBar::item {{ padding: 4px 10px; border-radius: 6px; background: transparent; }}
QMenuBar::item:selected {{ background: {p.hover}; }}
QMenu {{ background: {p.elevated}; border: {bw}px solid {p.border_strong}; border-radius: 8px; padding: 6px; }}
QMenu::item {{ padding: 6px 28px 6px 12px; border-radius: 6px; }}
QMenu::item:selected {{ background: {p.selection}; color: {p.selection_text}; }}
QMenu::item:disabled {{ color: {p.text_disabled}; }}
QMenu::separator {{ height: 1px; background: {p.border}; margin: 6px 8px; }}
QMenu::icon {{ padding-left: 8px; }}
QToolBar {{ background: {p.surface}; border: none; border-bottom: {bw}px solid {p.border}; padding: 4px 8px; spacing: 2px; }}
QToolBar::separator {{ background: {p.border}; width: 1px; margin: 6px 6px; }}
QToolButton {{ background: transparent; border: {bw}px solid transparent; border-radius: 6px; padding: 5px; }}
QToolButton:hover {{ background: {p.hover}; }}
QToolButton:pressed, QToolButton:checked {{ background: {p.selection}; color: {p.selection_text}; }}
QToolButton:focus {{ border: 2px solid {p.focus}; }}
QToolButton:disabled {{ color: {p.text_disabled}; }}
QToolButton[popupMode="1"] {{ padding-right: 14px; }}
/* ---------- faixa de comandos (ribbon) ---------- */
QToolBar#RibbonBar {{ padding: 0; border-bottom: {bw}px solid {p.border}; }}
#Ribbon {{ background: {p.surface}; }}
#RibbonGroupTitle {{ color: {p.text_muted}; font-size: {s(7)}pt; font-weight: 600; letter-spacing: 0.6px;
    padding: 1px 0 3px 0; }}
#RibbonSep {{ color: {p.border}; background: {p.border}; max-width: 1px; margin: 6px 6px 8px 6px; border: none; }}
QToolButton#RibbonButton {{ min-width: {s(50)}px; padding: {s(4)}px {s(4)}px {s(3)}px {s(4)}px; border-radius: 8px; }}
QToolButton#RibbonButton[accent="true"] {{ color: {p.accent}; font-weight: 600; }}
QToolButton#RibbonButton::menu-button {{ border: none; width: 14px; subcontrol-position: bottom right; }}
QToolButton#RibbonButton::menu-arrow {{ image: url({chev}); width: 9px; height: 9px; }}
QToolButton#RibbonSmall {{ padding: 2px 8px 2px 4px; text-align: left; border-radius: 6px; }}
#Segmented {{ background: {p.surface_alt}; border: {bw}px solid {p.border}; border-radius: 10px; }}
QPushButton#SegmentButton {{ background: transparent; border: {bw}px solid transparent; border-radius: 7px;
    padding: {s(4)}px {s(12)}px; color: {p.text_muted}; font-weight: 600; }}
QPushButton#SegmentButton:hover {{ color: {p.text}; background: {p.hover}; }}
QPushButton#SegmentButton:checked {{ background: {p.accent}; color: {p.accent_text}; border-color: {p.accent}; }}
QPushButton#SegmentButton:focus {{ border: 2px solid {p.focus}; }}
QToolButton#SectionHeader {{ color: {p.text_muted}; font-weight: 600; font-size: {s(8)}pt; letter-spacing: 0.4px;
    padding: 4px 2px; text-align: left; border-radius: 4px; background: transparent; }}
QToolButton#SectionHeader:hover {{ color: {p.text}; background: {p.hover}; }}
QToolButton#SectionHeader:checked {{ background: transparent; color: {p.text_muted}; }}
QToolButton#SectionHeader:checked:hover {{ color: {p.text}; background: {p.hover}; }}
#NavBar {{ background: {p.elevated}; border: {bw}px solid {p.border_strong}; border-radius: 10px; }}
#NavBar QToolButton {{ padding: 4px; border-radius: 6px; }}
#NavBar QLabel {{ color: {p.text_muted}; padding: 0 4px; font-size: {s(8)}pt; }}
#InspectorHeader {{ background: {p.surface}; border-bottom: {bw}px solid {p.border}; }}
#ToolInfo {{ background: {p.surface_alt}; border: {bw}px solid {p.border}; border-left: 3px solid {p.accent};
    border-radius: 6px; padding: 6px 10px; }}
#StatChip {{ background: {p.surface_alt}; border: {bw}px solid {p.border}; border-radius: 8px; }}
#StatValue {{ font-size: {s(11)}pt; font-weight: 600; }}
#StatLabel {{ color: {p.text_muted}; font-size: {s(8)}pt; }}
QStatusBar {{ background: {p.surface}; border-top: {bw}px solid {p.border}; color: {p.text_muted}; }}
QStatusBar::item {{ border: none; }}
QStatusBar QLabel {{ color: {p.text_muted}; padding: 0 8px; }}

/* ---------- botões ---------- */
QPushButton {{ background: {p.surface_alt}; border: {bw}px solid {p.border_strong}; border-radius: 6px;
    padding: {s(5)}px {s(14)}px; min-height: {s(18)}px; }}
QPushButton:hover {{ background: {p.hover}; }}
QPushButton:pressed {{ background: {p.border}; }}
QPushButton:focus {{ border: 2px solid {p.focus}; }}
QPushButton:disabled {{ color: {p.text_disabled}; border-color: {p.border}; }}
QPushButton:checked {{ background: {p.selection}; color: {p.selection_text}; border-color: {p.accent}; }}
QPushButton[primary="true"] {{ background: {p.accent}; color: {p.accent_text}; border: {bw}px solid {p.accent}; font-weight: 600; }}
QPushButton[primary="true"]:hover {{ background: {p.accent_hover}; }}
QPushButton[primary="true"]:focus {{ border: 2px solid {p.focus}; }}
QPushButton[primary="true"]:disabled {{ background: {p.surface_alt}; color: {p.text_disabled}; border-color: {p.border}; }}
QPushButton[danger="true"] {{ color: {p.danger}; border-color: {p.danger}; }}
QPushButton[flat="true"], QPushButton[link="true"] {{ background: transparent; border: {bw}px solid transparent; }}
QPushButton[flat="true"]:hover {{ background: {p.hover}; }}
QPushButton[link="true"] {{ color: {p.accent}; padding: 2px 4px; }}
QPushButton[link="true"]:hover {{ text-decoration: underline; }}
QPushButton[jog="true"] {{ min-width: {s(40)}px; min-height: {s(32)}px; padding: 4px; font-weight: 600; }}
QPushButton::menu-indicator {{ image: url({chev}); subcontrol-position: right center; right: 6px; width: 12px; }}

/* ---------- entradas ---------- */
QLineEdit, QAbstractSpinBox, QComboBox, QPlainTextEdit, QTextEdit {{
    background: {p.surface_alt}; border: {bw}px solid {p.border_strong}; border-radius: 6px; padding: 4px 8px;
    selection-background-color: {p.accent}; selection-color: {p.accent_text}; }}
QPlainTextEdit, QTextEdit {{ padding: 4px; }}
QLineEdit:hover, QAbstractSpinBox:hover, QComboBox:hover {{ border-color: {p.text_muted}; }}
QLineEdit:focus, QAbstractSpinBox:focus, QComboBox:focus, QPlainTextEdit:focus, QTextEdit:focus {{
    border: 2px solid {p.focus}; padding: 3px 7px; }}
QLineEdit:disabled, QAbstractSpinBox:disabled, QComboBox:disabled {{ color: {p.text_disabled}; background: {p.surface}; }}
QLineEdit[invalid="true"], QAbstractSpinBox[invalid="true"] {{ border: 2px solid {p.danger}; }}
QLineEdit[modified="true"], QAbstractSpinBox[modified="true"], QComboBox[modified="true"] {{ border-left: 3px solid {p.accent}; }}
QAbstractSpinBox {{ padding-right: 22px; }}
QLineEdit[locked="true"], QAbstractSpinBox[locked="true"] {{ background: {p.surface}; color: {p.text_muted};
    border-style: dashed; }}
QAbstractSpinBox[locked="true"] QLineEdit {{ background: transparent; color: {p.text_muted}; border: none;
    padding: 0; }}
QAbstractSpinBox[locked="true"]::up-arrow, QAbstractSpinBox[locked="true"]::down-arrow {{ image: none; }}
QAbstractSpinBox[locked="true"]::up-button:hover, QAbstractSpinBox[locked="true"]::down-button:hover {{
    background: transparent; }}
QAbstractSpinBox::up-button, QAbstractSpinBox::down-button {{ subcontrol-origin: border; width: 18px; border: none;
    background: transparent; }}
QAbstractSpinBox::up-button {{ subcontrol-position: top right; margin: 2px 2px 0 0; }}
QAbstractSpinBox::down-button {{ subcontrol-position: bottom right; margin: 0 2px 2px 0; }}
QAbstractSpinBox::up-button:hover, QAbstractSpinBox::down-button:hover {{ background: {p.hover}; border-radius: 3px; }}
QAbstractSpinBox::up-arrow {{ image: url({up}); width: 11px; height: 11px; }}
QAbstractSpinBox::down-arrow {{ image: url({chev}); width: 11px; height: 11px; }}
QComboBox {{ padding-right: 24px; }}
QComboBox::drop-down {{ border: none; width: 24px; }}
QComboBox::down-arrow {{ image: url({chev}); width: 12px; height: 12px; }}
QComboBox QAbstractItemView {{ background: {p.elevated}; border: {bw}px solid {p.border_strong}; border-radius: 6px;
    padding: 4px; outline: none; selection-background-color: {p.selection}; selection-color: {p.selection_text}; }}
QCheckBox, QRadioButton {{ spacing: 8px; padding: 2px 0; }}
QCheckBox::indicator, QRadioButton::indicator {{ width: {s(16)}px; height: {s(16)}px; border: {max(bw, 1) + (0 if hc else 0)}px solid {p.text_muted};
    background: {p.surface_alt}; border-radius: 4px; }}
QRadioButton::indicator {{ border-radius: {s(8)}px; }}
QListView::indicator, QTreeView::indicator {{ width: {s(16)}px; height: {s(16)}px; border: {max(bw, 1)}px solid {p.text_muted};
    background: {p.surface_alt}; border-radius: 4px; }}
QListView::indicator:checked, QTreeView::indicator:checked {{ background: {p.accent}; border-color: {p.accent}; image: url({check}); }}
QListView::indicator:hover, QTreeView::indicator:hover {{ border-color: {p.accent}; }}
QCheckBox::indicator:hover, QRadioButton::indicator:hover {{ border-color: {p.accent}; }}
QCheckBox::indicator:checked {{ background: {p.accent}; border-color: {p.accent}; image: url({check}); }}
QRadioButton::indicator:checked {{ background: {p.accent}; border: 4px solid {p.surface_alt}; outline: none; }}
QCheckBox:focus, QRadioButton:focus {{ color: {p.text}; }}
QCheckBox:focus::indicator, QRadioButton:focus::indicator {{ border: 2px solid {p.focus}; }}
QSlider::groove:horizontal {{ height: 4px; background: {p.border_strong}; border-radius: 2px; }}
QSlider::sub-page:horizontal {{ background: {p.accent}; border-radius: 2px; }}
QSlider::handle:horizontal {{ background: {p.text}; width: 14px; height: 14px; margin: -6px 0; border-radius: 7px; }}
QSlider:focus::handle:horizontal {{ background: {p.focus}; }}

/* ---------- listas / árvores / tabelas ---------- */
QTreeView, QListView, QTableView, QTableWidget {{ background: {p.surface}; alternate-background-color: {p.surface_alt};
    border: none; outline: none; }}
QTreeView {{ show-decoration-selected: 0; }}
QTreeView:focus, QListView:focus, QTableView:focus {{ border: 1px solid {p.focus}; }}
QTreeView::item, QListView::item {{ padding: 4px 4px; border-radius: 4px; }}
QTreeView::item:hover, QListView::item:hover {{ background: {p.hover}; }}
QTreeView::item:selected, QListView::item:selected, QTableView::item:selected {{
    background: {p.selection}; color: {p.selection_text}; }}
QTreeView::branch, QTreeView::branch:selected, QTreeView::branch:hover {{ background: transparent; }}
QTreeView::branch:has-children:closed {{ image: url({chev_r}); }}
QTreeView::branch:has-children:open {{ image: url({chev}); }}
QHeaderView::section {{ background: {p.surface_alt}; color: {p.text_muted}; border: none; border-bottom: {bw}px solid {p.border};
    border-right: 1px solid {p.border}; padding: 5px 8px; font-weight: 600; }}
QTableView {{ gridline-color: {p.border}; }}
QTableCornerButton::section {{ background: {p.surface_alt}; border: none; }}

/* ---------- abas ---------- */
QTabWidget::pane {{ border: none; border-top: {bw}px solid {p.border}; background: {p.surface}; top: -1px; }}
QTabBar {{ qproperty-drawBase: 0; background: transparent; }}
QTabBar::tab {{ background: transparent; color: {p.text_muted}; padding: {s(7)}px {s(14)}px; border: none;
    border-bottom: 2px solid transparent; margin-right: 2px; }}
QTabBar::tab:hover {{ color: {p.text}; background: {p.hover}; }}
QTabBar::tab:selected {{ color: {p.text}; border-bottom: 2px solid {p.accent}; }}
QTabBar::tab:focus {{ color: {p.text}; background: {p.hover}; border-bottom: 2px solid {p.focus}; }}
QDockWidget QTabBar::tab {{ padding: {s(6)}px {s(12)}px; }}

/* ---------- diversos ---------- */
QScrollArea, QScrollArea > QWidget > QWidget {{ background: transparent; border: none; }}
QScrollBar:vertical {{ background: transparent; width: {s(11)}px; margin: 0; }}
QScrollBar:horizontal {{ background: transparent; height: {s(11)}px; margin: 0; }}
QScrollBar::handle {{ background: {p.border_strong}; border-radius: 4px; min-height: 28px; min-width: 28px; margin: 2px; }}
QScrollBar::handle:hover {{ background: {p.text_muted}; }}
QScrollBar::add-line, QScrollBar::sub-line, QScrollBar::add-page, QScrollBar::sub-page {{ background: none; border: none; height: 0; width: 0; }}
QProgressBar {{ background: {p.surface_alt}; border: {bw}px solid {p.border}; border-radius: 5px; text-align: center;
    color: {p.text}; min-height: 10px; }}
QProgressBar::chunk {{ background: {p.accent}; border-radius: 4px; }}
QToolTip {{ background: {p.elevated}; color: {p.text}; border: {bw}px solid {p.border_strong}; padding: 6px 8px; border-radius: 6px; }}
QGroupBox {{ border: {bw}px solid {p.border}; border-radius: 8px; margin-top: 14px; padding: 10px 8px 8px 8px; }}
QGroupBox::title {{ subcontrol-origin: margin; left: 10px; padding: 0 4px; color: {p.text_muted}; font-weight: 600; }}
QSplitter::handle {{ background: {p.border}; }}
QSplitter::handle:horizontal {{ width: 1px; }}
QSplitter::handle:vertical {{ height: 1px; }}
QFrame[frameShape="4"], QFrame[frameShape="5"] {{ color: {p.border}; }}
QLabel:disabled {{ color: {p.text_disabled}; }}
QTextBrowser {{ background: {p.surface}; border: none; }}
QListWidget#CategoryList {{ background: {p.surface_alt}; border-right: {bw}px solid {p.border}; padding: 6px; }}
QListWidget#CategoryList::item {{ padding: 7px 10px; border-radius: 6px; }}
QListWidget#CategoryList::item:selected {{ background: {p.selection}; color: {p.selection_text}; }}
#WelcomeCard {{ background: {p.surface}; border: {bw}px solid {p.border}; border-radius: 14px; }}
#DropHint {{ border: 2px dashed {p.border_strong}; border-radius: 10px; color: {p.text_muted}; padding: 14px; }}
#StateChip {{ border-radius: 9px; padding: 2px 10px; font-weight: 600; }}
#Banner {{ border-radius: 8px; padding: 8px 10px; }}
#DroValue {{ font-size: {s(17)}pt; font-weight: 600; }}
#DroAxis {{ font-size: {s(12)}pt; font-weight: 700; color: {p.text_muted}; }}
#DroSecondary {{ color: {p.text_muted}; }}
#JogPad {{ background: {p.surface_alt}; border: {bw}px solid {p.border}; border-radius: 10px; }}
#JogPad:focus {{ border: 2px solid {p.focus}; }}
"""


def _qpalette(p: Palette) -> QPalette:
    pal = QPalette()
    c = QColor
    roles = {
        QPalette.ColorRole.Window: p.window,
        QPalette.ColorRole.WindowText: p.text,
        QPalette.ColorRole.Base: p.surface_alt,
        QPalette.ColorRole.AlternateBase: p.surface,
        QPalette.ColorRole.ToolTipBase: p.elevated,
        QPalette.ColorRole.ToolTipText: p.text,
        QPalette.ColorRole.PlaceholderText: p.text_muted,
        QPalette.ColorRole.Text: p.text,
        QPalette.ColorRole.Button: p.surface_alt,
        QPalette.ColorRole.ButtonText: p.text,
        QPalette.ColorRole.BrightText: p.danger,
        QPalette.ColorRole.Highlight: p.accent,
        QPalette.ColorRole.HighlightedText: p.accent_text,
        QPalette.ColorRole.Link: p.accent,
        QPalette.ColorRole.LinkVisited: p.accent,
        QPalette.ColorRole.Light: p.border_strong,
        QPalette.ColorRole.Midlight: p.border,
        QPalette.ColorRole.Mid: p.border,
        QPalette.ColorRole.Dark: p.window,
        QPalette.ColorRole.Shadow: "#000000",
    }
    for role, col in roles.items():
        pal.setColor(role, c(col))
    for role in (QPalette.ColorRole.Text, QPalette.ColorRole.WindowText, QPalette.ColorRole.ButtonText):
        pal.setColor(QPalette.ColorGroup.Disabled, role, c(p.text_disabled))
    return pal


def relative_luminance(hex_color: str) -> float:
    col = QColor(hex_color)
    def ch(v: float) -> float:
        return v / 12.92 if v <= 0.03928 else ((v + 0.055) / 1.055) ** 2.4
    return 0.2126 * ch(col.redF()) + 0.7152 * ch(col.greenF()) + 0.0722 * ch(col.blueF())


def contrast_ratio(a: str, b: str) -> float:
    la, lb = relative_luminance(a), relative_luminance(b)
    hi, lo = max(la, lb), min(la, lb)
    return (hi + 0.05) / (lo + 0.05)


class ThemeManager(QObject):
    """Mantém o tema atual e o aplica à aplicação."""

    changed = Signal(object)  # Palette

    def __init__(self) -> None:
        super().__init__()
        self.mode = "system"          # "system" | "dark" | "light"
        self.high_contrast = False
        self.font_scale = 1.0
        self._palette = DARK
        self._connected = False

    # ------------------------------------------------------------------ consulta
    @property
    def palette(self) -> Palette:
        return self._palette

    def system_is_dark(self) -> bool:
        app = QGuiApplication.instance()
        if app is None:
            return True
        try:
            scheme = QGuiApplication.styleHints().colorScheme()
            if scheme == Qt.ColorScheme.Light:
                return False
            if scheme == Qt.ColorScheme.Dark:
                return True
        except AttributeError:  # Qt < 6.5
            pass
        return True

    def resolve(self) -> Palette:
        if self.high_contrast:
            return HIGH_CONTRAST
        if self.mode == "dark":
            return DARK
        if self.mode == "light":
            return LIGHT
        return DARK if self.system_is_dark() else LIGHT

    # ------------------------------------------------------------------ aplicação
    def configure(self, mode: str | None = None, high_contrast: bool | None = None,
                  font_scale: float | None = None) -> None:
        if mode is not None:
            self.mode = mode
        if high_contrast is not None:
            self.high_contrast = high_contrast
        if font_scale is not None:
            self.font_scale = max(1.0, min(2.0, float(font_scale)))
        self.apply()

    def apply(self) -> None:
        app = QApplication.instance()
        if app is None:
            return
        if not self._connected:
            try:
                QGuiApplication.styleHints().colorSchemeChanged.connect(self._on_system_scheme)
            except AttributeError:
                pass
            self._connected = True
        p = self.resolve()
        self._palette = p
        app.setStyle("Fusion")
        font = QFont(ui_font_family())
        font.setPointSizeF(BASE_POINT_SIZE * self.font_scale)
        font.setHintingPreference(QFont.HintingPreference.PreferNoHinting)
        app.setFont(font)
        app.setPalette(_qpalette(p))
        app.setStyleSheet(build_qss(p, self.font_scale))
        self.changed.emit(p)

    def _on_system_scheme(self, *_args: object) -> None:
        if self.mode == "system" and not self.high_contrast:
            self.apply()


_manager: ThemeManager | None = None


def manager() -> ThemeManager:
    global _manager
    if _manager is None:
        _manager = ThemeManager()
    return _manager


class _ThemeHook(QObject):
    """Conecta ``fn`` a ``changed`` com vida útil presa a ``owner`` (desconecta ao destruir)."""

    def __init__(self, owner: QObject, fn) -> None:  # type: ignore[no-untyped-def]
        super().__init__(owner)
        self._fn = fn
        manager().changed.connect(self._fire)

    def _fire(self, p: object) -> None:
        self._fn(p)


def on_change(owner: QObject, fn) -> QObject:  # type: ignore[no-untyped-def]
    """Executa ``fn(palette)`` a cada troca de tema enquanto ``owner`` existir."""
    return _ThemeHook(owner, fn)


def palette() -> Palette:
    return manager().palette


def qcolor(token: str, alpha: int | None = None) -> QColor:
    """Cor de um token da paleta atual (ex.: ``qcolor('accent')``)."""
    c = QColor(getattr(palette(), token))
    if alpha is not None:
        c.setAlpha(alpha)
    return c


# Paleta categórica para diferenciar diâmetros de furo (baseada em Okabe-Ito, segura para
# daltonismo). Uma variante por tema: todas as cores têm contraste ≥ 3:1 (componente
# gráfico, WCAG 1.4.11) com o fundo do canvas e dos painéis. A ordem alterna tons quentes e
# frios para que diâmetros vizinhos (0,8 / 0,9 mm) fiquem bem diferentes. A 9ª em diante
# repete a lista — a tabela do inspetor sempre mostra o diâmetro em texto, além da cor.
DRILL_COLORS_DARK = ("#E69F00", "#56B4E9", "#2BB98A", "#F0E442", "#B18CFF", "#F07A2E", "#E08CC0", "#C8CCD4")
DRILL_COLORS_LIGHT = ("#B36B00", "#1F78B4", "#00805C", "#8C7A00", "#6B3FC0", "#C04A00", "#A8407E", "#4A4F57")
DRILL_COLORS_HC = ("#FFB000", "#5CC8FF", "#3DFF9E", "#FFF04D", "#C8A2FF", "#FF7A3D", "#FF8CDA", "#FFFFFF")


def drill_colors(p: Palette | None = None) -> tuple[str, ...]:
    """Lista de cores de furação para a paleta ``p`` (padrão: a atual)."""
    p = p or palette()
    if p is HIGH_CONTRAST or p.name == HIGH_CONTRAST.name:
        return DRILL_COLORS_HC
    return DRILL_COLORS_DARK if p.dark else DRILL_COLORS_LIGHT


def drill_color(index: int, p: Palette | None = None) -> str:
    """Cor do ``index``-ésimo diâmetro (0 = menor) — mesma no canvas e no inspetor."""
    cols = drill_colors(p)
    return cols[int(index) % len(cols)]


def set_property(widget, name: str, value) -> None:  # type: ignore[no-untyped-def]
    """Define uma propriedade dinâmica e força o QSS a reavaliar o widget."""
    widget.setProperty(name, value)
    st = widget.style()
    st.unpolish(widget)
    st.polish(widget)
    widget.update()


__all__ = [
    "Palette", "DARK", "LIGHT", "HIGH_CONTRAST", "ROLE_COLORS", "ROLE_LABELS", "ThemeManager",
    "manager", "on_change", "palette", "qcolor", "set_property", "mono_font", "ui_font_family",
    "contrast_ratio", "SPACING", "drill_color", "drill_colors",
]

