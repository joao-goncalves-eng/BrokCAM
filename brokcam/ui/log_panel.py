"""Aba "Log": mensagens do aplicativo (via ``logging``) com filtro por nível."""
from __future__ import annotations

import html
import logging
import time

from PySide6.QtCore import QObject, Qt, Signal
from PySide6.QtGui import QTextCursor
from PySide6.QtWidgets import (
    QComboBox, QHBoxLayout, QLabel, QPlainTextEdit, QToolButton, QVBoxLayout, QWidget,
)

from . import theme
from .icons import icon

MAX_BLOCKS = 5000


class _Emitter(QObject):
    record = Signal(int, str, str)   # nível, hora, mensagem


class QtLogHandler(logging.Handler):
    """Encaminha registros de ``logging`` (de qualquer thread) para a GUI por sinal."""

    def __init__(self) -> None:
        super().__init__(logging.INFO)
        self.emitter = _Emitter()

    def emit(self, record: logging.LogRecord) -> None:
        try:
            msg = record.getMessage()
            if record.exc_info and record.levelno >= logging.ERROR:
                msg = msg.splitlines()[0] if msg else str(record.exc_info[1])
            self.emitter.record.emit(record.levelno, time.strftime("%H:%M:%S", time.localtime(record.created)), msg)
        except Exception:  # pragma: no cover
            self.handleError(record)


_LEVELS = [(logging.DEBUG, "Tudo"), (logging.INFO, "Informações"),
           (logging.WARNING, "Avisos e erros"), (logging.ERROR, "Somente erros")]


class LogPanel(QWidget):
    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setObjectName("Panel")
        lay = QVBoxLayout(self)
        lay.setContentsMargins(8, 6, 8, 8)
        lay.setSpacing(6)
        bar = QHBoxLayout()
        lab = QLabel("Mostrar")
        lab.setObjectName("Muted")
        self.level = QComboBox()
        self.level.setAccessibleName("Nível mínimo de mensagens do log")
        for lv, name in _LEVELS:
            self.level.addItem(name, lv)
        self.level.setCurrentIndex(1)
        lab.setBuddy(self.level)
        self.level.currentIndexChanged.connect(self._rerender)
        clear = QToolButton()
        clear.setIcon(icon("trash"))
        clear.setToolTip("Limpar o log")
        clear.setAccessibleName("Limpar o log")
        clear.clicked.connect(self.clear)
        bar.addWidget(lab)
        bar.addWidget(self.level)
        bar.addStretch(1)
        bar.addWidget(clear)
        lay.addLayout(bar)
        self.view = QPlainTextEdit()
        self.view.setReadOnly(True)
        self.view.setMaximumBlockCount(MAX_BLOCKS)
        self.view.setFont(theme.mono_font(theme.manager().font_scale, -0.5))
        self.view.setAccessibleName("Mensagens do log")
        self.view.setLineWrapMode(QPlainTextEdit.LineWrapMode.WidgetWidth)
        self.view.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse
                                          | Qt.TextInteractionFlag.TextSelectableByKeyboard)
        lay.addWidget(self.view, 1)
        self.records: list[tuple[int, str, str]] = []
        self.handler = QtLogHandler()
        self.handler.emitter.record.connect(self.append)
        theme.on_change(self, lambda _p: self._rerender())

    def install(self, logger: logging.Logger | None = None) -> None:
        (logger or logging.getLogger("brokcam")).addHandler(self.handler)

    def uninstall(self, logger: logging.Logger | None = None) -> None:
        (logger or logging.getLogger("brokcam")).removeHandler(self.handler)

    def _line(self, level: int, ts: str, msg: str) -> str:
        p = theme.palette()
        if level >= logging.ERROR:
            col, tag = p.danger, "ERRO"
        elif level >= logging.WARNING:
            col, tag = p.warning, "AVISO"
        elif level >= logging.INFO:
            col, tag = p.text_muted, "INFO"
        else:
            col, tag = p.text_disabled, "DEBUG"
        return (f'<span style="color:{p.text_muted}">{ts}</span>&nbsp;'
                f'<span style="color:{col}; font-weight:600">{tag:<5}</span>&nbsp;'
                f'<span style="color:{p.text}">{html.escape(msg)}</span>')

    def append(self, level: int, ts: str, msg: str) -> None:
        self.records.append((level, ts, msg))
        if len(self.records) > MAX_BLOCKS:
            del self.records[: len(self.records) - MAX_BLOCKS]
        if level >= int(self.level.currentData()):
            self.view.appendHtml(self._line(level, ts, msg))
            self.view.moveCursor(QTextCursor.MoveOperation.End)

    def _rerender(self) -> None:
        self.view.setFont(theme.mono_font(theme.manager().font_scale, -0.5))
        self.view.clear()
        lv = int(self.level.currentData())
        self.view.appendHtml("<br>".join(self._line(*r) for r in self.records if r[0] >= lv))

    def clear(self) -> None:
        self.records.clear()
        self.view.clear()

    def text(self) -> str:
        return self.view.toPlainText()

