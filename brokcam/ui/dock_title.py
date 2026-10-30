"""Barra de título compacta para painéis (docks) destacáveis.

Quando o painel está encaixado em abas, o nome já aparece na aba: a barra mostra só os
botões (destacar / fechar), ocupando pouca altura. Sozinho numa área (ex.: coluna da
direita do espaço Máquina) ou destacado (janela flutuante), mostra também o título.

Arrastar a barra move o painel: ao passar perto de uma borda da janela o Qt mostra onde ele
vai encaixar (esquerda, direita, topo, rodapé ou em abas sobre outro painel) e, ao soltar,
encaixa automaticamente. Duplo clique destaca/encaixa. Com os painéis travados
(Exibir › Painéis › Travar painéis) o botão de destacar some e a barra não arrasta.
"""
from __future__ import annotations

from PySide6.QtCore import QSize, Qt
from PySide6.QtGui import QMouseEvent
from PySide6.QtWidgets import QDockWidget, QHBoxLayout, QLabel, QMainWindow, QToolButton, QWidget

from .icons import icon


class DockTitleBar(QWidget):
    def __init__(self, dock: QDockWidget) -> None:
        super().__init__(dock)
        self.dock = dock
        self.setObjectName("DockTitleBar")
        self.setCursor(Qt.CursorShape.OpenHandCursor)
        lay = QHBoxLayout(self)
        lay.setContentsMargins(10, 2, 4, 2)
        lay.setSpacing(2)
        self.title = QLabel(dock.windowTitle())
        self.title.setObjectName("Muted")
        lay.addWidget(self.title)
        lay.addStretch(1)
        self.float_btn = QToolButton()
        self.float_btn.setAutoRaise(True)
        self.float_btn.setIconSize(QSize(14, 14))
        self.float_btn.setCursor(Qt.CursorShape.ArrowCursor)
        self.float_btn.clicked.connect(lambda: dock.setFloating(not dock.isFloating()))
        self.close_btn = QToolButton()
        self.close_btn.setAutoRaise(True)
        self.close_btn.setIconSize(QSize(14, 14))
        self.close_btn.setCursor(Qt.CursorShape.ArrowCursor)
        self.close_btn.setIcon(icon("close"))
        self.close_btn.setToolTip(f"Fechar o painel {dock.windowTitle()} (reabra pelo menu Exibir › Painéis)")
        self.close_btn.setAccessibleName(f"Fechar painel {dock.windowTitle()}")
        self.close_btn.clicked.connect(dock.close)
        lay.addWidget(self.float_btn)
        lay.addWidget(self.close_btn)
        dock.topLevelChanged.connect(lambda _f: self.sync())
        dock.dockLocationChanged.connect(lambda _a: self.sync())
        dock.featuresChanged.connect(lambda _f: self.sync())
        dock.windowTitleChanged.connect(self.title.setText)
        self.sync()

    def _tabbed(self) -> bool:
        mw = self.dock.parentWidget()
        if self.dock.isFloating() or not isinstance(mw, QMainWindow):
            return False
        try:
            return bool(mw.tabifiedDockWidgets(self.dock))
        except RuntimeError:          # janela sendo destruída
            return False

    def sync(self) -> None:
        """Atualiza título/botões conforme o painel está flutuando, em abas ou travado."""
        floating = self.dock.isFloating()
        movable = bool(self.dock.features() & QDockWidget.DockWidgetFeature.DockWidgetFloatable)
        self.title.setVisible(floating or not self._tabbed())
        self.float_btn.setVisible(movable or floating)
        self.float_btn.setIcon(icon("restore" if floating else "export"))
        tip = ("Encaixar de volta na janela" if floating else
               "Destacar como janela separada — ou arraste esta barra até uma borda da janela para encaixar ali")
        self.float_btn.setToolTip(tip)
        self.float_btn.setAccessibleName(tip)
        self.setCursor(Qt.CursorShape.OpenHandCursor if movable else Qt.CursorShape.ArrowCursor)

    _sync = sync

    def mouseDoubleClickEvent(self, ev: QMouseEvent) -> None:  # noqa: N802
        if ev.button() == Qt.MouseButton.LeftButton and (
                self.dock.features() & QDockWidget.DockWidgetFeature.DockWidgetFloatable):
            self.dock.setFloating(not self.dock.isFloating())
            ev.accept()
            return
        super().mouseDoubleClickEvent(ev)

    def sizeHint(self) -> QSize:  # noqa: N802
        s = super().sizeHint()
        return QSize(s.width(), max(24, s.height()))
