"""Temas do BrokCAM: paletas, cor de destaque, QSS e tipografia.

Visual inspirado em CAD profissional (Fusion 360 / Inventor): cinzas neutros, pouca
moldura, cantos discretos, faixa de comandos com grupos, área de trabalho em degradê.

O tema final é a combinação de:

* **base** (:data:`BASES`): ``escuro``, ``claro``, ``grafite``, ``azul-aço``, ``meia-noite``
  — ou ``alto contraste``, que tem prioridade e ignora as outras escolhas de cor;
* **cor de destaque** (:data:`ACCENTS` ou uma cor ``#rrggbb`` qualquer). Botões principais,
  seleção, foco e links são DERIVADOS dela por :func:`derive_palette`, sempre ajustados para
  contraste AA (4,5:1) — qualquer cor escolhida continua legível;
* **estilo** (:class:`Style`): fundo do canvas em degradê ou sólido, cantos (suaves,
  arredondados, retos), densidade (confortável/compacta) e ícones coloridos.

Todos os componentes leem cores de :func:`palette` (tokens semânticos) em vez de usar cores
fixas. A troca de tema emite :attr:`ThemeManager.changed` e os widgets que desenham por conta
própria (canvas, editor de G-code, ícones) se atualizam ao receber o sinal.
"""
from __future__ import annotations

import colorsys
import hashlib
import tempfile
from dataclasses import dataclass, replace
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
    accent: str          # preenchimento de botões principais / marcações
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
    # ---- acrescentados na interface estilo CAD (padrão = derivado em ``_finish``)
    accent_fg: str = ""      # destaque usado como COR DE TEXTO (links, rótulos): ≥ 4,5:1 sobre os painéis
    ribbon: str = ""         # fundo da faixa de comandos
    header: str = ""         # fundo das barras de título dos painéis
    canvas_top: str = ""     # degradê do canvas (topo → base)
    canvas_bottom: str = ""
    label: str = ""          # nome exibido ao usuário


# --------------------------------------------------------------------------- cores (utilitários)

def _rgb(c: str) -> tuple[float, float, float]:
    q = QColor(c)
    return q.redF(), q.greenF(), q.blueF()


def _hex(r: float, g: float, b: float) -> str:
    return "#{:02x}{:02x}{:02x}".format(*(max(0, min(255, round(v * 255))) for v in (r, g, b)))


def mix(a: str, b: str, t: float) -> str:
    """Mistura ``a`` e ``b`` (``t`` = peso de ``a``, 0..1)."""
    ra, ga, ba = _rgb(a)
    rb, gb, bb = _rgb(b)
    return _hex(ra * t + rb * (1 - t), ga * t + gb * (1 - t), ba * t + bb * (1 - t))


def shade(c: str, dl: float) -> str:
    """Clareia (``dl`` > 0) ou escurece (``dl`` < 0) em luminosidade HLS."""
    h, l, s = colorsys.rgb_to_hls(*_rgb(c))
    return _hex(*colorsys.hls_to_rgb(h, max(0.0, min(1.0, l + dl)), s))


def ensure_contrast(color: str, bg: str, ratio: float = 4.5) -> str:
    """Ajusta a luminosidade de ``color`` (mantendo o matiz) até ``ratio`` contra ``bg``."""
    if contrast_ratio(color, bg) >= ratio:
        return color
    step = -0.02 if relative_luminance(bg) > 0.35 else 0.02
    c = color
    for _ in range(60):
        c = shade(c, step)
        if contrast_ratio(c, bg) >= ratio:
            return c
    return "#000000" if step < 0 else "#ffffff"


# --------------------------------------------------------------------------- bases

DARK = Palette(
    name="escuro", label="Escuro", dark=True,
    window="#17181a", surface="#212225", surface_alt="#292b2f", elevated="#2e3034",
    hover="#34363b", border="#323439", border_strong="#4a4d54",
    text="#e8e9eb", text_muted="#a7aab0", text_disabled="#6c7077",
    accent="#0696d7", accent_hover="#22a6e3", accent_text="#0b1220",
    selection="#1d3f57", selection_text="#ffffff", focus="#3fb3ec",
    danger="#ff6b6b", warning="#f2b84b", success="#4cc38a", info="#62b0ff",
    canvas_bg="#25282c", grid_minor="#2c2f34", grid_major="#383c42",
    axis_x="#e5534b", axis_y="#46c26f",
    cut="#38d0e8", rapid="#d16ad8", done="#ffd24a", tool="#ffffff", measure="#ffd24a",
    highlight_line="#26344a",
    ribbon="#25272a", header="#1c1d20", canvas_top="#3b3f45", canvas_bottom="#1b1d20",
)

LIGHT = Palette(
    name="claro", label="Claro", dark=False,
    window="#e3e5e8", surface="#f6f7f8", surface_alt="#ffffff", elevated="#ffffff",
    hover="#e8ebee", border="#d4d8dd", border_strong="#b0b6be",
    text="#1f2328", text_muted="#565c64", text_disabled="#9aa0a8",
    accent="#0679b0", accent_hover="#05699a", accent_text="#ffffff",
    selection="#d3e9f6", selection_text="#0b1f33", focus="#0679b0",
    danger="#c62828", warning="#9a6200", success="#1b7f4b", info="#1565c0",
    canvas_bg="#f1f3f5", grid_minor="#e6e9ed", grid_major="#d3d8de",
    axis_x="#d32f2f", axis_y="#2e7d32",
    cut="#0b7fa8", rapid="#a23ab0", done="#d97706", tool="#111111", measure="#c2410c",
    highlight_line="#e3edff",
    ribbon="#fbfbfc", header="#eceef1", canvas_top="#ffffff", canvas_bottom="#d7dde5",
)

GRAPHITE = Palette(
    name="grafite", label="Grafite", dark=True,
    window="#2a2c30", surface="#35383d", surface_alt="#3e4147", elevated="#43464c",
    hover="#4a4e55", border="#484b52", border_strong="#61656e",
    text="#f2f3f5", text_muted="#c3c7cd", text_disabled="#80858d",
    accent="#3aa0e8", accent_hover="#57b0ee", accent_text="#0b1220",
    selection="#2f4f6b", selection_text="#ffffff", focus="#63b8f0",
    danger="#ff7b7b", warning="#f5c25a", success="#5fd09a", info="#74bcff",
    canvas_bg="#43474d", grid_minor="#4b4f56", grid_major="#585d65",
    axis_x="#ff6a61", axis_y="#5bd884",
    cut="#4fe0f5", rapid="#e27fe8", done="#ffd84f", tool="#ffffff", measure="#ffd84f",
    highlight_line="#3a4a60",
    ribbon="#393c41", header="#2f3135", canvas_top="#5f656d", canvas_bottom="#2d3034",
)

STEEL = Palette(
    name="azul-aço", label="Azul-aço", dark=False,
    window="#b7c5d4", surface="#dce5ee", surface_alt="#f3f6f9", elevated="#f8fafc",
    hover="#cad6e2", border="#a9b9ca", border_strong="#8597ab",
    text="#15202b", text_muted="#3a4654", text_disabled="#7d8b9b",
    accent="#1f6fb5", accent_hover="#1a5f9c", accent_text="#ffffff",
    selection="#bcd3ea", selection_text="#0c1d30", focus="#1f6fb5",
    danger="#a3221b", warning="#7a4f00", success="#17663d", info="#12508f",
    canvas_bg="#c5d3e1", grid_minor="#b9c8d7", grid_major="#a6b8ca",
    axis_x="#c62828", axis_y="#2e7d32",
    cut="#0a4f7a", rapid="#8a2b9c", done="#b45309", tool="#111111", measure="#b03a0a",
    highlight_line="#d5e3f2",
    ribbon="#e5ecf3", header="#ccd8e4", canvas_top="#9fb5cb", canvas_bottom="#e9eff5",
)

MIDNIGHT = Palette(
    name="meia-noite", label="Meia-noite", dark=True,
    window="#0b1322", surface="#121c31", surface_alt="#19253f", elevated="#1e2b49",
    hover="#243357", border="#253454", border_strong="#3a4c73",
    text="#e9eefb", text_muted="#a5b3d1", text_disabled="#63739a",
    accent="#5aa9ff", accent_hover="#7bbaff", accent_text="#0a1222",
    selection="#23406e", selection_text="#ffffff", focus="#8cc3ff",
    danger="#ff7070", warning="#f4bf4f", success="#52d08f", info="#6cb8ff",
    canvas_bg="#141f38", grid_minor="#1a2742", grid_major="#243556",
    axis_x="#ff5f56", axis_y="#4fd27c",
    cut="#3ad7ef", rapid="#da78e0", done="#ffd24a", tool="#ffffff", measure="#ffd24a",
    highlight_line="#1f3358",
    ribbon="#142038", header="#0f182b", canvas_top="#21345b", canvas_bottom="#0a111f",
)

HIGH_CONTRAST = Palette(
    name="alto contraste", label="Alto contraste", dark=True,
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
    accent_fg="#ffd400", ribbon="#000000", header="#000000", canvas_top="#000000", canvas_bottom="#000000",
)

# Bases escolhíveis (chave da preferência "theme" → paleta). "system" = claro/escuro do Windows.
BASES: dict[str, Palette] = {"dark": DARK, "light": LIGHT, "graphite": GRAPHITE, "steel": STEEL,
                             "midnight": MIDNIGHT}

# Cores de destaque: (chave, rótulo, cor-semente). ``None`` = a do próprio tema / personalizada.
ACCENTS: tuple[tuple[str, str, str | None], ...] = (
    ("theme", "Padrão do tema", None),
    ("blue", "Azul (Autodesk)", "#0696d7"),
    ("royal", "Azul royal", "#2f6fed"),
    ("cyan", "Ciano", "#00a5b5"),
    ("green", "Verde", "#2e9e5b"),
    ("orange", "Laranja", "#f07c1b"),
    ("red", "Vermelho", "#d64545"),
    ("pink", "Rosa", "#d6458f"),
    ("purple", "Roxo", "#7b5cd6"),
    ("amber", "Âmbar", "#e0a100"),
    ("slate", "Grafite", "#7a828c"),
    ("custom", "Personalizada…", None),
)
ACCENT_SEEDS = {k: v for k, _l, v in ACCENTS if v}


def _finish(p: Palette) -> Palette:
    """Preenche os tokens novos que uma paleta não define (compatibilidade)."""
    upd: dict[str, str] = {}
    if not p.accent_fg:
        fg = p.accent
        for bg in (p.surface, p.surface_alt, p.window, p.elevated):
            fg = ensure_contrast(fg, bg, 4.5)
        upd["accent_fg"] = fg
    if not p.ribbon:
        upd["ribbon"] = p.surface
    if not p.header:
        upd["header"] = p.surface
    if not p.canvas_top:
        upd["canvas_top"] = p.canvas_bg
    if not p.canvas_bottom:
        upd["canvas_bottom"] = p.canvas_bg
    if not p.label:
        upd["label"] = p.name.capitalize()
    return replace(p, **upd) if upd else p


def derive_palette(base: Palette, seed: str | None) -> Palette:
    """``base`` com a cor de destaque ``seed`` (``None`` = a do tema), tudo ajustado para AA.

    * ``accent`` (preenchimento) recebe texto branco ou quase preto — o de maior contraste — e
      é escurecido/clareado até 4,5:1 com ele;
    * ``accent_fg`` (destaque como texto) atinge 4,5:1 sobre janela, painéis e campos;
    * ``selection`` é uma mistura suave do destaque com o painel, com texto legível.
    """
    if base is HIGH_CONTRAST or base.name == HIGH_CONTRAST.name:
        return _finish(base)
    if not seed:
        return _finish(base)
    seed = QColor(seed).name()
    white, black = "#ffffff", "#0d1117"
    txt = white if contrast_ratio(white, seed) >= contrast_ratio(black, seed) else black
    fill = ensure_contrast(seed, txt, 4.5)
    hover = shade(fill, 0.06 if base.dark else -0.06)
    if contrast_ratio(hover, txt) < 4.5:
        hover = shade(fill, -0.06 if base.dark else 0.06)
        if contrast_ratio(hover, txt) < 4.5:
            hover = fill
    fg = seed
    for bg in (base.surface, base.surface_alt, base.window, base.elevated, base.header, base.ribbon or base.surface):
        fg = ensure_contrast(fg, bg, 4.5)
    sel = mix(seed, base.surface, 0.30 if base.dark else 0.20)
    sel_text = base.text if contrast_ratio(base.text, sel) >= 4.5 else (
        white if contrast_ratio(white, sel) >= contrast_ratio(black, sel) else black)
    focus = fg
    hl = mix(seed, base.surface_alt, 0.16 if base.dark else 0.12)
    return _finish(replace(base, accent=fill, accent_hover=hover, accent_text=txt, accent_fg=fg,
                           selection=sel, selection_text=sel_text, focus=focus, highlight_line=hl))


@dataclass(frozen=True)
class Style:
    """Escolhas de estilo independentes das cores."""
    canvas: str = "gradient"      # "gradient" | "solid"
    corners: str = "soft"         # "soft" | "round" | "square"
    density: str = "comfortable"  # "comfortable" | "compact"
    colored_icons: bool = True

    def radii(self) -> tuple[int, int, int]:
        """(pequeno, médio, grande) em px."""
        return {"round": (6, 8, 12), "square": (0, 0, 2)}.get(self.corners, (3, 4, 6))

    def pad(self) -> float:
        return 0.72 if self.density == "compact" else 1.0


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


def build_qss(p: Palette, scale: float, style: Style | None = None) -> str:
    """Gera a folha de estilos para a paleta ``p`` e o estilo ``style``."""
    st = style or Style()
    p = _finish(p)
    cache = Path(tempfile.gettempdir()) / "brokcam-theme" / hashlib.md5(
        p.name.encode()).hexdigest()[:8]
    cache.mkdir(parents=True, exist_ok=True)
    chev = _colored_svg("chevron-down", p.text_muted, cache)
    chev_r = _colored_svg("chevron-right", p.text_muted, cache)
    check = _colored_svg("check", p.accent_text, cache)
    s = lambda v: max(1, round(v * scale))  # noqa: E731   (fontes/tamanhos)
    d = st.pad()
    P = lambda v: max(1, round(v * scale * d))  # noqa: E731   (espaçamentos internos)
    r1, r2, r3 = st.radii()
    hc = p.name == HIGH_CONTRAST.name
    bw = 2 if hc else 1
    up_svg = cache / f"chevron-up-{p.text_muted.lstrip('#')}.svg"
    if not up_svg.exists():
        up_svg.write_text((ICON_DIR / "chevron-down.svg").read_text(encoding="utf-8")
                          .replace("currentColor", p.text_muted)
                          .replace('d="m6 9 6 6 6-6"', 'd="m6 15 6-6 6 6"'), encoding="utf-8")
    up = up_svg.as_posix()
    if st.canvas == "gradient" and not hc:
        welcome_bg = (f"qlineargradient(x1:0, y1:0, x2:0, y2:1, stop:0 {p.canvas_top}, "
                      f"stop:1 {p.canvas_bottom})")
    else:
        welcome_bg = p.canvas_bg
    return f"""
* {{ outline: none; }}
QWidget {{ color: {p.text}; selection-background-color: {p.selection}; selection-color: {p.selection_text}; }}
QMainWindow, QDialog, QWizard {{ background: {p.window}; }}
QMainWindow::separator {{ background: {p.window}; width: {s(3)}px; height: {s(3)}px; }}
QMainWindow::separator:hover {{ background: {p.accent}; }}

/* ---------- docks (painéis) ---------- */
QDockWidget {{ titlebar-close-icon: none; color: {p.text_muted}; }}
QDockWidget::title {{ background: {p.header}; padding: {P(6)}px {P(10)}px; border-bottom: {bw}px solid {p.border};
    text-align: left; }}
QDockWidget > QWidget {{ background: {p.surface}; }}
#DockTitleBar {{ background: {p.header}; border-bottom: {bw}px solid {p.border}; }}
#DockTitle {{ color: {p.text_muted}; font-weight: 700; font-size: {s(8)}pt; letter-spacing: 0.8px; }}
#Panel, #PanelBody {{ background: {p.surface}; }}
#SectionTitle {{ color: {p.text_muted}; font-weight: 600; letter-spacing: 0.6px;
    font-size: {s(8)}pt; padding-top: {s(4)}px; }}
#Heading {{ font-size: {s(13)}pt; font-weight: 600; }}
#SubHeading {{ color: {p.text_muted}; }}
#Muted {{ color: {p.text_muted}; }}
#Card {{ background: {p.surface_alt}; border: {bw}px solid {p.border}; border-radius: {r2}px; }}
#HelpPanel {{ background: {p.surface_alt}; border: {bw}px solid {p.border}; border-radius: {r2}px; }}
#HelpTitle {{ font-weight: 600; }}
#Badge {{ background: {p.surface_alt}; border: {bw}px solid {p.border}; border-radius: {max(r2, 8)}px; padding: 1px 8px;
    color: {p.text_muted}; font-size: {s(8)}pt; }}

/* ---------- barras ---------- */
QMenuBar {{ background: {p.header}; border-bottom: {bw}px solid {p.border}; padding: 1px 4px; }}
QMenuBar::item {{ padding: {P(4)}px {P(10)}px; border-radius: {r1}px; background: transparent; }}
QMenuBar::item:selected {{ background: {p.hover}; }}
QMenu {{ background: {p.elevated}; border: {bw}px solid {p.border_strong}; border-radius: {r2}px; padding: {P(5)}px; }}
QMenu::item {{ padding: {P(6)}px {P(28)}px {P(6)}px {P(12)}px; border-radius: {r1}px; }}
QMenu::item:selected {{ background: {p.selection}; color: {p.selection_text}; }}
QMenu::item:disabled {{ color: {p.text_disabled}; }}
QMenu::separator {{ height: 1px; background: {p.border}; margin: {P(5)}px 8px; }}
QMenu::icon {{ padding-left: 8px; }}
QMenu::indicator {{ width: {s(14)}px; height: {s(14)}px; left: 6px; }}
QToolBar {{ background: {p.ribbon}; border: none; border-bottom: {bw}px solid {p.border}; padding: 4px 8px; spacing: 2px; }}
QToolBar::separator {{ background: {p.border}; width: 1px; margin: 6px 6px; }}
QToolButton {{ background: transparent; border: {bw}px solid transparent; border-radius: {r1}px; padding: {P(5)}px; }}
QToolButton:hover {{ background: {p.hover}; }}
QToolButton:pressed, QToolButton:checked {{ background: {p.selection}; color: {p.selection_text}; }}
QToolButton:focus {{ border: 2px solid {p.focus}; }}
QToolButton:disabled {{ color: {p.text_disabled}; }}
QToolButton[popupMode="1"] {{ padding-right: 14px; }}
QToolButton[popupMode="2"] {{ padding-right: {P(16)}px; }}
QToolButton[popupMode="2"]::menu-indicator {{ image: url({chev}); width: 9px; height: 9px;
    subcontrol-position: right center; subcontrol-origin: padding; right: 3px; }}
/* ---------- faixa de comandos (ribbon) ---------- */
QToolBar#RibbonBar {{ padding: 0; border-bottom: {bw}px solid {p.border}; background: {p.ribbon}; }}
#Ribbon {{ background: {p.ribbon}; }}
#RibbonGroup {{ background: transparent; }}
#RibbonGroupTitle {{ color: {p.text_muted}; font-size: {s(7)}pt; font-weight: 700; letter-spacing: 0.8px;
    padding: 1px 0 3px 0; }}
#RibbonSep {{ color: {p.border}; background: {p.border}; max-width: 1px; margin: 8px 4px 10px 4px; border: none; }}
QToolButton#RibbonButton {{ min-width: {s(50)}px; padding: {P(4)}px {P(4)}px {P(3)}px {P(4)}px; border-radius: {r2}px; }}
QToolButton#RibbonButton:hover {{ background: {p.hover}; border-color: {p.border}; }}
QToolButton#RibbonButton[accent="true"] {{ color: {p.accent_fg}; font-weight: 600; }}
QToolButton#RibbonButton::menu-button {{ border: none; width: 14px; subcontrol-position: bottom right; }}
QToolButton#RibbonButton::menu-arrow {{ image: url({chev}); width: 9px; height: 9px; }}
QToolButton#RibbonSmall {{ padding: 2px 8px 2px 4px; text-align: left; border-radius: {r1}px; }}
QToolButton#RibbonSmall::menu-button {{ border: none; width: 12px; }}
QToolButton#RibbonSmall::menu-arrow {{ image: url({chev}); width: 8px; height: 8px; }}
/* alternador de espaço de trabalho (à esquerda da faixa, como o seletor do Fusion) */
#Segmented {{ background: {p.surface_alt}; border: {bw}px solid {p.border}; border-radius: {r2 + 2}px; }}
#Segmented[vertical="true"] {{ background: transparent; border: none; }}
QPushButton#SegmentButton {{ background: transparent; border: {bw}px solid transparent; border-radius: {max(r2, 1)}px;
    padding: {P(4)}px {P(12)}px; color: {p.text_muted}; font-weight: 600; text-align: left; min-height: {s(18)}px; }}
QPushButton#SegmentButton:hover {{ color: {p.text}; background: {p.hover}; }}
QPushButton#SegmentButton:checked {{ background: {p.accent}; color: {p.accent_text}; border-color: {p.accent}; }}
QPushButton#SegmentButton:focus {{ border: 2px solid {p.focus}; }}
#Segmented[vertical="true"] QPushButton#SegmentButton {{ border: {bw}px solid {p.border}; background: {p.surface_alt};
    padding: {P(3)}px {P(10)}px; }}
#Segmented[vertical="true"] QPushButton#SegmentButton:hover {{ background: {p.hover}; color: {p.text}; }}
#Segmented[vertical="true"] QPushButton#SegmentButton:checked {{ background: {p.accent}; color: {p.accent_text};
    border-color: {p.accent}; }}
QToolButton#SectionHeader {{ color: {p.text_muted}; font-weight: 700; font-size: {s(8)}pt; letter-spacing: 0.6px;
    padding: {P(4)}px 2px; text-align: left; border-radius: {r1}px; background: transparent; }}
QToolButton#SectionHeader:hover {{ color: {p.text}; background: {p.hover}; }}
QToolButton#SectionHeader:checked {{ background: transparent; color: {p.text_muted}; }}
QToolButton#SectionHeader:checked:hover {{ color: {p.text}; background: {p.hover}; }}
#NavBar {{ background: {p.elevated}; border: {bw}px solid {p.border_strong}; border-radius: {r3 + 2}px; }}
#NavBar QToolButton {{ padding: 4px; border-radius: {r1}px; }}
#NavBar QLabel {{ color: {p.text_muted}; padding: 0 4px; font-size: {s(8)}pt; }}
#InspectorHeader {{ background: {p.surface}; border-bottom: {bw}px solid {p.border}; }}
#ToolInfo {{ background: {p.surface_alt}; border: {bw}px solid {p.border}; border-left: 3px solid {p.accent};
    border-radius: {r1}px; padding: {P(6)}px {P(10)}px; }}
#StatChip {{ background: {p.surface_alt}; border: {bw}px solid {p.border}; border-radius: {r2}px; }}
#StatValue {{ font-size: {s(11)}pt; font-weight: 600; }}
#StatLabel {{ color: {p.text_muted}; font-size: {s(8)}pt; }}
QStatusBar {{ background: {p.header}; border-top: {bw}px solid {p.border}; color: {p.text_muted}; }}
QStatusBar::item {{ border: none; }}
QStatusBar QLabel {{ color: {p.text_muted}; padding: 0 8px; }}

/* ---------- botões ---------- */
QPushButton {{ background: {p.surface_alt}; border: {bw}px solid {p.border_strong}; border-radius: {r1}px;
    padding: {P(5)}px {P(14)}px; min-height: {s(18)}px; }}
QPushButton:hover {{ background: {p.hover}; border-color: {p.text_muted}; }}
QPushButton:pressed {{ background: {p.border}; }}
QPushButton:focus {{ border: 2px solid {p.focus}; }}
QPushButton:disabled {{ color: {p.text_disabled}; border-color: {p.border}; }}
QPushButton:checked {{ background: {p.selection}; color: {p.selection_text}; border-color: {p.accent}; }}
QPushButton[primary="true"] {{ background: {p.accent}; color: {p.accent_text}; border: {bw}px solid {p.accent}; font-weight: 600; }}
QPushButton[primary="true"]:hover {{ background: {p.accent_hover}; border-color: {p.accent_hover}; }}
QPushButton[primary="true"]:focus {{ border: 2px solid {p.focus}; }}
QPushButton[primary="true"]:disabled {{ background: {p.surface_alt}; color: {p.text_disabled}; border-color: {p.border}; }}
QPushButton[danger="true"] {{ color: {p.danger}; border-color: {p.danger}; }}
QPushButton[flat="true"], QPushButton[link="true"] {{ background: transparent; border: {bw}px solid transparent; }}
QPushButton[flat="true"]:hover {{ background: {p.hover}; }}
QPushButton[link="true"] {{ color: {p.accent_fg}; padding: 2px 4px; }}
QPushButton[link="true"]:hover {{ text-decoration: underline; }}
QPushButton[jog="true"] {{ min-width: {s(40)}px; min-height: {s(32)}px; padding: 4px; font-weight: 600; }}
QPushButton::menu-indicator {{ image: url({chev}); subcontrol-position: right center; right: 6px; width: 12px; }}

/* ---------- entradas ---------- */
QLineEdit, QAbstractSpinBox, QComboBox, QPlainTextEdit, QTextEdit {{
    background: {p.surface_alt}; border: {bw}px solid {p.border_strong}; border-radius: {r1}px; padding: {P(4)}px {P(8)}px;
    selection-background-color: {p.accent}; selection-color: {p.accent_text}; }}
QPlainTextEdit, QTextEdit {{ padding: 4px; }}
QLineEdit:hover, QAbstractSpinBox:hover, QComboBox:hover {{ border-color: {p.text_muted}; }}
QLineEdit:focus, QAbstractSpinBox:focus, QComboBox:focus, QPlainTextEdit:focus, QTextEdit:focus {{
    border: 2px solid {p.focus}; padding: {max(0, P(4) - 1)}px {max(0, P(8) - 1)}px; }}
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
QAbstractSpinBox::up-button:hover, QAbstractSpinBox::down-button:hover {{ background: {p.hover}; border-radius: {min(r1, 3)}px; }}
QAbstractSpinBox::up-arrow {{ image: url({up}); width: 11px; height: 11px; }}
QAbstractSpinBox::down-arrow {{ image: url({chev}); width: 11px; height: 11px; }}
QComboBox {{ padding-right: 24px; }}
QComboBox::drop-down {{ border: none; width: 24px; }}
QComboBox::down-arrow {{ image: url({chev}); width: 12px; height: 12px; }}
QComboBox QAbstractItemView {{ background: {p.elevated}; border: {bw}px solid {p.border_strong}; border-radius: {r1}px;
    padding: 4px; outline: none; selection-background-color: {p.selection}; selection-color: {p.selection_text}; }}
QCheckBox, QRadioButton {{ spacing: 8px; padding: 2px 0; }}
QCheckBox::indicator, QRadioButton::indicator {{ width: {s(16)}px; height: {s(16)}px; border: {max(bw, 1)}px solid {p.text_muted};
    background: {p.surface_alt}; border-radius: {max(2, min(r1, 4))}px; }}
QRadioButton::indicator {{ border-radius: {s(8)}px; }}
QListView::indicator, QTreeView::indicator {{ width: {s(16)}px; height: {s(16)}px; border: {max(bw, 1)}px solid {p.text_muted};
    background: {p.surface_alt}; border-radius: {max(2, min(r1, 4))}px; }}
QListView::indicator:checked, QTreeView::indicator:checked {{ background: {p.accent}; border-color: {p.accent}; image: url({check}); }}
QListView::indicator:hover, QTreeView::indicator:hover {{ border-color: {p.accent}; }}
QCheckBox::indicator:hover, QRadioButton::indicator:hover {{ border-color: {p.accent}; }}
QCheckBox::indicator:checked {{ background: {p.accent}; border-color: {p.accent}; image: url({check}); }}
QRadioButton::indicator:checked {{ background: {p.accent}; border: 4px solid {p.surface_alt}; outline: none; }}
QCheckBox:focus, QRadioButton:focus {{ color: {p.text}; }}
QCheckBox::indicator:focus, QRadioButton::indicator:focus {{ border: 2px solid {p.focus}; }}
QSlider::groove:horizontal {{ height: 4px; background: {p.border_strong}; border-radius: 2px; }}
QSlider::sub-page:horizontal {{ background: {p.accent}; border-radius: 2px; }}
QSlider::handle:horizontal {{ background: {p.text}; width: 14px; height: 14px; margin: -6px 0; border-radius: 7px; }}
QSlider:focus::handle:horizontal {{ background: {p.focus}; }}

/* ---------- listas / árvores / tabelas ---------- */
QTreeView, QListView, QTableView, QTableWidget {{ background: {p.surface}; alternate-background-color: {p.surface_alt};
    border: none; outline: none; }}
QTreeView {{ show-decoration-selected: 0; }}
QTreeView:focus, QListView:focus, QTableView:focus {{ border: 1px solid {p.focus}; }}
QTreeView::item, QListView::item {{ padding: {P(4)}px 4px; border-radius: {min(r1, 4)}px; }}
QTreeView::item:hover, QListView::item:hover {{ background: {p.hover}; }}
QTreeView::item:selected, QListView::item:selected, QTableView::item:selected {{
    background: {p.selection}; color: {p.selection_text}; }}
QTreeView::branch, QTreeView::branch:selected, QTreeView::branch:hover {{ background: transparent; }}
QTreeView::branch:has-children:closed {{ image: url({chev_r}); }}
QTreeView::branch:has-children:open {{ image: url({chev}); }}
QHeaderView::section {{ background: {p.header}; color: {p.text_muted}; border: none; border-bottom: {bw}px solid {p.border};
    border-right: 1px solid {p.border}; padding: {P(5)}px {P(8)}px; font-weight: 600; }}
QTableView {{ gridline-color: {p.border}; }}
QTableCornerButton::section {{ background: {p.header}; border: none; }}

/* ---------- abas ---------- */
QTabWidget::pane {{ border: none; border-top: {bw}px solid {p.border}; background: {p.surface}; top: -1px; }}
QTabBar {{ qproperty-drawBase: 0; background: transparent; }}
QTabBar::tab {{ background: transparent; color: {p.text_muted}; padding: {P(7)}px {P(14)}px; border: none;
    border-bottom: 2px solid transparent; margin-right: 2px; font-weight: 600; }}
QTabBar::tab:hover {{ color: {p.text}; background: {p.hover}; }}
QTabBar::tab:selected {{ color: {p.text}; border-bottom: 2px solid {p.accent}; }}
QTabBar::tab:focus {{ color: {p.text}; background: {p.hover}; border-bottom: 2px solid {p.focus}; }}
QDockWidget QTabBar::tab, QMainWindow > QTabBar::tab {{ padding: {P(6)}px {P(12)}px; background: {p.header}; }}
QMainWindow > QTabBar::tab:selected {{ background: {p.surface}; }}

/* ---------- diversos ---------- */
QScrollArea, QScrollArea > QWidget > QWidget {{ background: transparent; border: none; }}
QScrollBar:vertical {{ background: transparent; width: {s(10)}px; margin: 0; }}
QScrollBar:horizontal {{ background: transparent; height: {s(10)}px; margin: 0; }}
QScrollBar::handle {{ background: {p.border_strong}; border-radius: {min(r1 + 1, 4)}px; min-height: 28px; min-width: 28px; margin: 2px; }}
QScrollBar::handle:hover {{ background: {p.text_muted}; }}
QScrollBar::add-line, QScrollBar::sub-line, QScrollBar::add-page, QScrollBar::sub-page {{ background: none; border: none; height: 0; width: 0; }}
QProgressBar {{ background: {p.surface_alt}; border: {bw}px solid {p.border}; border-radius: {min(r1 + 1, 5)}px; text-align: center;
    color: {p.text}; min-height: 10px; }}
QProgressBar::chunk {{ background: {p.accent}; border-radius: {min(r1, 4)}px; }}
QToolTip {{ background: {p.elevated}; color: {p.text}; border: {bw}px solid {p.border_strong}; padding: 6px 8px; border-radius: {r1}px; }}
QGroupBox {{ border: {bw}px solid {p.border}; border-radius: {r2}px; margin-top: 14px; padding: 10px 8px 8px 8px; }}
QGroupBox::title {{ subcontrol-origin: margin; left: 10px; padding: 0 4px; color: {p.text_muted}; font-weight: 600; }}
QSplitter::handle {{ background: {p.border}; }}
QSplitter::handle:horizontal {{ width: 1px; }}
QSplitter::handle:vertical {{ height: 1px; }}
QFrame[frameShape="4"], QFrame[frameShape="5"] {{ color: {p.border}; }}
QLabel:disabled {{ color: {p.text_disabled}; }}
QTextBrowser {{ background: {p.surface}; border: none; }}
QListWidget#CategoryList {{ background: {p.header}; border-right: {bw}px solid {p.border}; padding: 6px; }}
QListWidget#CategoryList::item {{ padding: {P(7)}px {P(10)}px; border-radius: {r1}px; }}
QListWidget#CategoryList::item:selected {{ background: {p.selection}; color: {p.selection_text}; }}
#Welcome {{ background: {welcome_bg}; }}
#WelcomeCard {{ background: {p.surface}; border: {bw}px solid {p.border}; border-radius: {r3 + 4}px; }}
#DropHint {{ border: 2px dashed {p.border_strong}; border-radius: {r3}px; color: {p.text_muted}; padding: 14px; }}
#StateChip {{ border-radius: {max(r2, 6)}px; padding: 2px 10px; font-weight: 600; }}
#Banner {{ border-radius: {r2}px; padding: 8px 10px; }}
#DroValue {{ font-size: {s(17)}pt; font-weight: 600; }}
#DroAxis {{ font-size: {s(12)}pt; font-weight: 700; color: {p.text_muted}; }}
#DroSecondary {{ color: {p.text_muted}; }}
#JogPad {{ background: {p.surface_alt}; border: {bw}px solid {p.border}; border-radius: {r3}px; }}
#JogPad:focus {{ border: 2px solid {p.focus}; }}
#AccentSwatch {{ border: {bw}px solid {p.border_strong}; border-radius: {max(r1, 2)}px; min-width: 18px; min-height: 18px; }}
"""


def relative_luminance(hex_color: str) -> float:
    col = QColor(hex_color)
    def ch(v: float) -> float:
        return v / 12.92 if v <= 0.03928 else ((v + 0.055) / 1.055) ** 2.4
    return 0.2126 * ch(col.redF()) + 0.7152 * ch(col.greenF()) + 0.0722 * ch(col.blueF())


def contrast_ratio(a: str, b: str) -> float:
    la, lb = relative_luminance(a), relative_luminance(b)
    hi, lo = max(la, lb), min(la, lb)
    return (hi + 0.05) / (lo + 0.05)




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
        QPalette.ColorRole.Link: p.accent_fg or p.accent,
        QPalette.ColorRole.LinkVisited: p.accent_fg or p.accent,
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


# Ícones coloridos (como no Fusion/Inventor): cor de cada ícone de comando. A cor final é
# ajustada para contraste ≥ 3:1 (componente gráfico, WCAG 1.4.11) com o painel do tema.
ICON_TINTS: dict[str, str] = {
    "open": "#e0a030", "import": "#e0a030", "new": "#8aa0b8", "save": "#3d8fd9", "export": "#3d8fd9",
    "isolation": "#e07b39", "copper": "#e07b39", "ncc": "#1fa595", "cutout": "#c9a227", "outline": "#c9a227",
    "drill": "#9b6cf0", "panelize": "#5b7cf0", "board": "#2e9e5b", "layers": "#5b8def",
    "move": "#3d8fd9", "rotate-ccw": "#3d8fd9", "rotate-cw": "#3d8fd9", "mirror": "#3d8fd9",
    "edit-path": "#d6458f", "group": "#7b8fa6", "ungroup": "#7b8fa6", "trash": "#d64545",
    "simulate": "#2e9e5b", "play": "#2e9e5b", "gcode": "#1a9fd0", "send": "#1a9fd0",
    "machine": "#6c8aa8", "spindle": "#6c8aa8", "probe": "#e2683c", "wizard": "#a45cd6",
    "tool-library": "#c27c2c", "measure": "#d0a020", "ruler": "#d0a020", "home": "#3d8fd9",
    "zero": "#2e9e5b", "warning": "#e0a100", "error": "#d64545", "info": "#3d8fd9", "help": "#3d8fd9",
    "stop": "#d64545", "pause": "#e0a100", "job": "#38a8d0", "geometry": "#2fb5a0", "settings": "#7b8fa6",
}


def icon_tint(name: str, p: Palette | None = None) -> str | None:
    """Cor do ícone ``name`` com "ícones coloridos" ligado (``None`` = monocromático)."""
    m = manager()
    p = p or m.palette
    if not m.style.colored_icons or p.name == HIGH_CONTRAST.name:
        return None
    base = ICON_TINTS.get(name)
    if base is None:
        return None
    key = (p.name, p.surface, p.ribbon, name)
    hit = _tint_cache.get(key)
    if hit is None:
        c = base
        for bg in (p.surface, p.ribbon, p.surface_alt, p.elevated):
            c = ensure_contrast(c, bg, 3.0)
        _tint_cache[key] = hit = c
    return hit


_tint_cache: dict[tuple[str, str, str, str], str] = {}


class ThemeManager(QObject):
    """Mantém o tema atual e o aplica à aplicação."""

    changed = Signal(object)  # Palette

    def __init__(self) -> None:
        super().__init__()
        self.mode = "system"          # "system" | chave de BASES
        self.high_contrast = False
        self.font_scale = 1.0
        self.accent = "theme"         # chave de ACCENTS ou "custom"
        self.accent_custom = "#0696d7"
        self.style = Style()
        self._palette = _finish(DARK)
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

    def base(self) -> Palette:
        if self.high_contrast:
            return HIGH_CONTRAST
        if self.mode in BASES:
            return BASES[self.mode]
        return DARK if self.system_is_dark() else LIGHT

    def accent_seed(self) -> str | None:
        if self.accent == "custom":
            c = QColor(str(self.accent_custom or ""))
            return c.name() if c.isValid() else None
        return ACCENT_SEEDS.get(self.accent)

    def resolve(self) -> Palette:
        return derive_palette(self.base(), self.accent_seed())

    # ------------------------------------------------------------------ aplicação
    def configure(self, mode: str | None = None, high_contrast: bool | None = None,
                  font_scale: float | None = None, *, accent: str | None = None,
                  accent_custom: str | None = None, canvas: str | None = None, corners: str | None = None,
                  density: str | None = None, colored_icons: bool | None = None) -> None:
        if mode is not None:
            self.mode = mode
        if high_contrast is not None:
            self.high_contrast = high_contrast
        if font_scale is not None:
            self.font_scale = max(1.0, min(2.0, float(font_scale)))
        if accent is not None:
            self.accent = accent
        if accent_custom is not None:
            self.accent_custom = accent_custom
        st = self.style
        self.style = Style(canvas=canvas if canvas is not None else st.canvas,
                           corners=corners if corners is not None else st.corners,
                           density=density if density is not None else st.density,
                           colored_icons=colored_icons if colored_icons is not None else st.colored_icons)
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
        _tint_cache.clear()
        app.setStyle("Fusion")
        font = QFont(ui_font_family())
        font.setPointSizeF(BASE_POINT_SIZE * self.font_scale)
        font.setHintingPreference(QFont.HintingPreference.PreferNoHinting)
        app.setFont(font)
        app.setPalette(_qpalette(p))
        app.setStyleSheet(build_qss(p, self.font_scale, self.style))
        self.changed.emit(p)

    def _on_system_scheme(self, *_args: object) -> None:
        if self.mode == "system" and not self.high_contrast:
            self.apply()

    # ------------------------------------------------------------------ canvas
    def canvas_gradient(self) -> tuple[str, str] | None:
        """(topo, base) do degradê do canvas, ou ``None`` se o fundo é sólido."""
        p = self._palette
        if self.style.canvas != "gradient" or p.name == HIGH_CONTRAST.name or p.canvas_top == p.canvas_bottom:
            return None
        return p.canvas_top, p.canvas_bottom


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
    "Palette", "DARK", "LIGHT", "GRAPHITE", "STEEL", "MIDNIGHT", "HIGH_CONTRAST", "BASES", "ACCENTS",
    "Style", "derive_palette", "ROLE_COLORS", "ROLE_LABELS", "ThemeManager", "manager", "on_change",
    "palette", "qcolor", "set_property", "mono_font", "ui_font_family", "contrast_ratio", "SPACING",
    "drill_color", "drill_colors", "icon_tint", "mix", "shade", "ensure_contrast",
]

