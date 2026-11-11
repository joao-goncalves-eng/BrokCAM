"""Componentes de layout profissional: faixa de comandos, controle segmentado e seções recolhíveis.

* :class:`Ribbon` — barra de comandos no topo, organizada em grupos nomeados (como no
  Fusion 360 / KiCad / LightBurn). Botões grandes com ícone + rótulo para as ações do
  fluxo; botões compactos empilhados para ações secundárias. Ações são ``QAction``
  comuns: habilitar/desabilitar e atalhos continuam centralizados na janela.
* :class:`SegmentedControl` — alternador de espaço de trabalho (Preparar | Máquina).
* :class:`CollapsibleSection` — seção do inspetor com cabeçalho clicável; lembra o estado.
"""
from __future__ import annotations

from typing import Any, Callable

from PySide6.QtCore import QSize, Qt, Signal
from PySide6.QtGui import QAction
from PySide6.QtWidgets import (
    QButtonGroup, QFrame, QHBoxLayout, QLabel, QMenu, QPushButton, QSizePolicy, QToolButton, QVBoxLayout,
    QWidget,
)

from .icons import icon

ROW_HEIGHT = 62          # altura da linha de botões: todos os títulos de grupo ficam alinhados


def _sync(btn: QToolButton, action: QAction) -> None:
    """Liga o botão à ação (texto, ícone, tooltip, habilitado, marcado) sem herdar o '&'."""
    def update() -> None:
        try:
            _apply()
        except RuntimeError:        # ação ou botão já destruídos (fechando a janela)
            pass

    def _apply() -> None:
        text = action.iconText() or action.text().replace("&", "").replace("…", "")
        btn.setText(text)
        btn.setIcon(action.icon())
        tip = action.toolTip().replace("&", "") or text
        sc = action.shortcut().toString()
        btn.setToolTip(f"{tip} ({sc})" if sc and sc not in tip else tip)
        btn.setEnabled(action.isEnabled())
        btn.setCheckable(action.isCheckable())
        if action.isCheckable():
            btn.setChecked(action.isChecked())
        btn.setAccessibleName(action.text().replace("&", "").replace("…", ""))
        btn.setAccessibleDescription(action.statusTip() or tip)
        btn.setVisible(action.isVisible())
    action.changed.connect(update)
    if action.isCheckable():
        action.toggled.connect(lambda on: btn.setChecked(on))
        btn.clicked.connect(lambda _=False: action.setChecked(btn.isChecked()))
    else:
        btn.clicked.connect(lambda _=False: action.trigger())
    update()


class RibbonGroup(QFrame):
    """Grupo de comandos com título embaixo."""

    def __init__(self, title: str, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setObjectName("RibbonGroup")
        self.setAccessibleName(f"Grupo {title}")
        outer = QVBoxLayout(self)
        outer.setContentsMargins(4, 2, 4, 0)
        outer.setSpacing(0)
        host = QWidget()
        host.setFixedHeight(ROW_HEIGHT)
        self.row = QHBoxLayout(host)
        self.row.setContentsMargins(0, 0, 0, 0)
        self.row.setSpacing(2)
        outer.addWidget(host)
        self.title = QLabel(title.upper())
        self.title.setObjectName("RibbonGroupTitle")
        self.title.setAlignment(Qt.AlignmentFlag.AlignHCenter)
        outer.addWidget(self.title)
        self._stack: QVBoxLayout | None = None
        self.buttons: list[QToolButton] = []

    def add(self, action: QAction, menu: QMenu | None = None, accent: bool = False) -> QToolButton:
        """Botão grande (ícone sobre o rótulo)."""
        self._stack = None
        b = QToolButton()
        b.setObjectName("RibbonButton")
        b.setToolButtonStyle(Qt.ToolButtonStyle.ToolButtonTextUnderIcon)
        b.setIconSize(QSize(22, 22))
        b.setAutoRaise(True)
        if accent:
            b.setProperty("accent", True)
        b.setSizePolicy(QSizePolicy.Policy.Fixed, QSizePolicy.Policy.Preferred)   # nunca elidir o rótulo
        _sync(b, action)
        if menu is not None:
            b.setMenu(menu)
            b.setPopupMode(QToolButton.ToolButtonPopupMode.MenuButtonPopup)
        self.row.addWidget(b)
        self.buttons.append(b)
        return b

    def add_small(self, action: QAction, menu: QMenu | None = None) -> QToolButton:
        """Botão compacto (ícone ao lado do rótulo), empilhado em colunas de até 3."""
        if self._stack is None or self._stack.count() >= 3:
            self._stack = QVBoxLayout()
            self._stack.setSpacing(0)
            self._stack.setContentsMargins(0, 0, 0, 0)
            self._stack.setAlignment(Qt.AlignmentFlag.AlignTop)
            self.row.addLayout(self._stack)
        b = QToolButton()
        b.setObjectName("RibbonSmall")
        b.setToolButtonStyle(Qt.ToolButtonStyle.ToolButtonTextBesideIcon)
        b.setIconSize(QSize(16, 16))
        b.setAutoRaise(True)
        b.setFixedHeight(ROW_HEIGHT // 3)
        b.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Fixed)
        _sync(b, action)
        if menu is not None:
            b.setMenu(menu)
            b.setPopupMode(QToolButton.ToolButtonPopupMode.MenuButtonPopup)
        self._stack.addWidget(b)
        self.buttons.append(b)
        return b

    def add_widget(self, w: QWidget) -> None:
        self._stack = None
        self.row.addWidget(w)


class Ribbon(QFrame):
    """Faixa de comandos com grupos à esquerda e área livre à direita."""

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setObjectName("Ribbon")
        self.setAccessibleName("Faixa de comandos")
        lay = QHBoxLayout(self)
        lay.setContentsMargins(6, 4, 8, 2)
        lay.setSpacing(0)
        self.groups_lay = QHBoxLayout()
        self.groups_lay.setSpacing(0)
        lay.addLayout(self.groups_lay)
        lay.addStretch(1)
        self.right = QHBoxLayout()
        self.right.setSpacing(6)
        lay.addLayout(self.right)
        self._left_count = 0
        self.groups: list[RibbonGroup] = []
        self._compact = False
        self._full_width = 0

    def resizeEvent(self, ev: Any) -> None:  # noqa: N802
        super().resizeEvent(ev)
        self._fit()

    def _fit(self) -> None:
        """Compacta (só ícones) quando os rótulos não cabem; volta quando houver espaço."""
        if not self._compact:
            need = self.sizeHint().width()
            self._full_width = need
            if need > self.width():
                self.set_compact(True)
        elif self._full_width and self._full_width <= self.width():
            self.set_compact(False)

    def group(self, title: str) -> RibbonGroup:
        if self.groups:
            sep = QFrame()
            sep.setObjectName("RibbonSep")
            sep.setFrameShape(QFrame.Shape.VLine)
            self.groups_lay.addWidget(sep)
        g = RibbonGroup(title)
        self.groups_lay.addWidget(g)
        self.groups.append(g)
        return g

    def add_left(self, w: QWidget, title: str = "") -> RibbonGroup:
        """Bloco fixo no início da faixa (ex.: seletor de espaço de trabalho), com título."""
        g = RibbonGroup(title)
        g.row.addWidget(w, 0, Qt.AlignmentFlag.AlignVCenter)
        self.groups_lay.insertWidget(self._left_count, g)
        sep = QFrame()
        sep.setObjectName("RibbonSep")
        sep.setFrameShape(QFrame.Shape.VLine)
        self.groups_lay.insertWidget(self._left_count + 1, sep)
        self._left_count += 2
        self.left_groups = getattr(self, "left_groups", []) + [g]
        return g

    def add_right(self, w: QWidget) -> None:
        self.right.addWidget(w, 0, Qt.AlignmentFlag.AlignVCenter)

    def set_compact(self, on: bool) -> None:
        """Esconde os rótulos dos botões grandes (janela estreita)."""
        if on == self._compact:
            return
        self._compact = on
        for g in self.groups + getattr(self, "left_groups", []):
            g.title.setVisible(not on)
            for b in g.buttons:
                if b.objectName() == "RibbonButton":
                    b.setToolButtonStyle(Qt.ToolButtonStyle.ToolButtonIconOnly if on
                                         else Qt.ToolButtonStyle.ToolButtonTextUnderIcon)
                else:
                    b.setToolButtonStyle(Qt.ToolButtonStyle.ToolButtonIconOnly if on
                                         else Qt.ToolButtonStyle.ToolButtonTextBesideIcon)


class SegmentedControl(QFrame):
    """Alternador exclusivo estilo "pílula" (ex.: espaço de trabalho)."""

    changed = Signal(str)

    def __init__(self, items: list[tuple[str, str, str]], accessible: str,
                 parent: QWidget | None = None, vertical: bool = False) -> None:
        """``items``: lista de (chave, rótulo, ícone). ``vertical`` empilha os botões (seletor de
        espaço de trabalho no início da faixa, ocupando pouca largura)."""
        super().__init__(parent)
        self.setObjectName("Segmented")
        self.setProperty("vertical", vertical)
        self.setAccessibleName(accessible)
        lay = QVBoxLayout(self) if vertical else QHBoxLayout(self)
        lay.setContentsMargins(0, 0, 0, 0) if vertical else lay.setContentsMargins(3, 3, 3, 3)
        lay.setSpacing(4 if vertical else 2)
        self.group = QButtonGroup(self)
        self.group.setExclusive(True)
        self._keys: list[str] = []
        for key, label, icon_name in items:
            b = QPushButton(icon(icon_name), label)
            b.setObjectName("SegmentButton")
            b.setCheckable(True)
            b.setAccessibleName(label)
            b.setCursor(Qt.CursorShape.PointingHandCursor)
            self.group.addButton(b, len(self._keys))
            self._keys.append(key)
            lay.addWidget(b)
        self.group.button(0).setChecked(True)
        self.group.idClicked.connect(lambda i: self.changed.emit(self._keys[i]))

    def set_current(self, key: str) -> None:
        if key in self._keys:
            self.group.button(self._keys.index(key)).setChecked(True)

    def current(self) -> str:
        return self._keys[max(0, self.group.checkedId())]

    def button(self, key: str) -> QPushButton:
        return self.group.button(self._keys.index(key))  # type: ignore[return-value]


class CollapsibleSection(QWidget):
    """Seção com cabeçalho clicável (▸/▾). ``state_get/state_set`` persistem o estado."""

    toggled = Signal(bool)

    def __init__(self, title: str, content: QWidget, expanded: bool = True, *,
                 summary: str = "", key: str = "",
                 state_get: Callable[[str, bool], bool] | None = None,
                 state_set: Callable[[str, bool], Any] | None = None,
                 parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.key = key
        self._set = state_set
        if key and state_get is not None:
            expanded = state_get(key, expanded)
        lay = QVBoxLayout(self)
        lay.setContentsMargins(0, 0, 0, 0)
        lay.setSpacing(6)
        self.header = QToolButton()
        self.header.setObjectName("SectionHeader")
        self.header.setToolButtonStyle(Qt.ToolButtonStyle.ToolButtonTextBesideIcon)
        self.header.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Fixed)
        self.header.setCheckable(True)
        self.header.setAutoRaise(True)
        self.header.setIconSize(QSize(12, 12))
        self._title = title
        self._summary = summary
        self.header.toggled.connect(self._toggle)
        lay.addWidget(self.header)
        self.content = content
        lay.addWidget(content)
        self.header.setChecked(expanded)
        self._toggle(expanded, emit=False)

    def set_summary(self, text: str) -> None:
        self._summary = text
        self._update_text()

    def _update_text(self) -> None:
        on = self.header.isChecked()
        extra = f"   ·   {self._summary}" if self._summary and not on else ""
        self.header.setText(self._title.upper() + extra)
        self.header.setAccessibleName(f"{self._title} ({'expandido' if on else 'recolhido'})")

    def _toggle(self, on: bool, emit: bool = True) -> None:
        self.content.setVisible(on)
        self.header.setIcon(icon("chevron-down" if on else "chevron-right"))
        self._update_text()
        if emit:
            if self.key and self._set is not None:
                self._set(self.key, on)
            self.toggled.emit(on)

    def is_expanded(self) -> bool:
        return self.header.isChecked()

    def set_expanded(self, on: bool) -> None:
        self.header.setChecked(on)
