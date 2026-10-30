"""Editor de G-code com números de linha, destaque de sintaxe e linha em execução."""
from __future__ import annotations

import re

from PySide6.QtCore import QRect, QSize, Qt, Signal
from PySide6.QtGui import (
    QColor, QFont, QKeySequence, QPainter, QShortcut, QSyntaxHighlighter, QTextCharFormat,
    QTextCursor, QTextDocument, QTextFormat,
)
from PySide6.QtWidgets import (
    QHBoxLayout, QLabel, QLineEdit, QSizePolicy, QPlainTextEdit, QTextEdit, QToolButton, QVBoxLayout, QWidget,
)

from . import theme
from .icons import icon


class GcodeHighlighter(QSyntaxHighlighter):
    _rules = [
        (re.compile(r"\b[Gg]\d+(\.\d+)?"), "g"),
        (re.compile(r"\b[Mm]\d+"), "m"),
        (re.compile(r"\b[Tt]\d+"), "t"),
        (re.compile(r"\b[FfSs][-+]?\d*\.?\d+"), "fs"),
        (re.compile(r"\b[XxYyZzIiJjKkRrPp][-+]?\d*\.?\d+"), "coord"),
        (re.compile(r"^\s*[Nn]\d+"), "n"),
        (re.compile(r"\$\S*"), "m"),
    ]
    _comment = [re.compile(r"\(.*?\)"), re.compile(r";.*$")]

    def __init__(self, doc: QTextDocument) -> None:
        super().__init__(doc)
        self.formats: dict[str, QTextCharFormat] = {}
        self.refresh()

    def refresh(self) -> None:
        p = theme.palette()
        def fmt(color: str, bold: bool = False, italic: bool = False) -> QTextCharFormat:
            f = QTextCharFormat()
            f.setForeground(QColor(color))
            if bold:
                f.setFontWeight(QFont.Weight.DemiBold)
            f.setFontItalic(italic)
            return f
        if p.dark:
            pal = {"g": "#6cb6ff", "m": "#d49cff", "t": "#57d69a", "fs": "#ffb86b",
                   "coord": p.text, "n": p.text_muted, "comment": "#8b949e"}
        else:
            pal = {"g": "#0b5cc5", "m": "#8a2ab8", "t": "#11744a", "fs": "#a24c00",
                   "coord": p.text, "n": p.text_muted, "comment": "#5d6673"}
        if p.name == "alto contraste":
            pal = {"g": "#5cd6ff", "m": "#ff8cff", "t": "#3dff8b", "fs": "#ffd400",
                   "coord": "#ffffff", "n": "#d0d0d0", "comment": "#c0c0c0"}
        self.formats = {
            "g": fmt(pal["g"], True), "m": fmt(pal["m"], True), "t": fmt(pal["t"], True),
            "fs": fmt(pal["fs"]), "coord": fmt(pal["coord"]), "n": fmt(pal["n"]),
            "comment": fmt(pal["comment"], italic=True),
        }
        self.rehighlight()

    def highlightBlock(self, text: str) -> None:  # noqa: N802
        if len(text) > 2000:
            return
        for rx, name in self._rules:
            for m in rx.finditer(text):
                self.setFormat(m.start(), m.end() - m.start(), self.formats[name])
        for rx in self._comment:
            for m in rx.finditer(text):
                self.setFormat(m.start(), m.end() - m.start(), self.formats["comment"])


class _LineNumbers(QWidget):
    def __init__(self, editor: "CodeEditor") -> None:
        super().__init__(editor)
        self.editor = editor

    def sizeHint(self) -> QSize:  # noqa: N802
        return QSize(self.editor.line_number_width(), 0)

    def paintEvent(self, ev) -> None:  # type: ignore[no-untyped-def]  # noqa: N802
        self.editor.paint_line_numbers(ev)


class CodeEditor(QPlainTextEdit):
    """QPlainTextEdit com margem de números de linha e destaque da linha em execução."""

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setLineWrapMode(QPlainTextEdit.LineWrapMode.NoWrap)
        self.setFont(theme.mono_font(theme.manager().font_scale))
        self.setTabChangesFocus(False)
        self.setAccessibleName("Editor de G-code")
        self.setAccessibleDescription("Programa G-code gerado. Pode ser editado antes de salvar ou enviar.")
        self._numbers = _LineNumbers(self)
        self.exec_line = -1
        self.highlighter = GcodeHighlighter(self.document())
        self.blockCountChanged.connect(self._update_margin)
        self.updateRequest.connect(self._update_area)
        self.cursorPositionChanged.connect(self._update_extra)
        self._update_margin()
        self._update_extra()
        theme.on_change(self, self._on_theme)

    def _on_theme(self, _p: object) -> None:
        self.setFont(theme.mono_font(theme.manager().font_scale))
        self.highlighter.refresh()
        self._update_margin()
        self._update_extra()
        self._numbers.update()

    def line_number_width(self) -> int:
        digits = max(3, len(str(max(1, self.blockCount()))))
        return 16 + self.fontMetrics().horizontalAdvance("9") * digits

    def _update_margin(self, *_a: object) -> None:
        self.setViewportMargins(self.line_number_width(), 0, 0, 0)

    def _update_area(self, rect: QRect, dy: int) -> None:
        if dy:
            self._numbers.scroll(0, dy)
        else:
            self._numbers.update(0, rect.y(), self._numbers.width(), rect.height())

    def resizeEvent(self, ev) -> None:  # type: ignore[no-untyped-def]  # noqa: N802
        super().resizeEvent(ev)
        cr = self.contentsRect()
        self._numbers.setGeometry(QRect(cr.left(), cr.top(), self.line_number_width(), cr.height()))

    def paint_line_numbers(self, ev) -> None:  # type: ignore[no-untyped-def]
        p = theme.palette()
        painter = QPainter(self._numbers)
        painter.fillRect(ev.rect(), QColor(p.surface))
        block = self.firstVisibleBlock()
        num = block.blockNumber()
        top = round(self.blockBoundingGeometry(block).translated(self.contentOffset()).top())
        bottom = top + round(self.blockBoundingRect(block).height())
        cur = self.textCursor().blockNumber()
        w = self._numbers.width() - 8
        h = self.fontMetrics().height()
        while block.isValid() and top <= ev.rect().bottom():
            if block.isVisible() and bottom >= ev.rect().top():
                if num == self.exec_line:
                    painter.setPen(QColor(p.done))
                elif num == cur:
                    painter.setPen(QColor(p.text))
                else:
                    painter.setPen(QColor(p.text_muted))
                painter.drawText(0, top, w, h, Qt.AlignmentFlag.AlignRight, str(num + 1))
            block = block.next()
            top = bottom
            bottom = top + round(self.blockBoundingRect(block).height())
            num += 1
        painter.setPen(QColor(p.border))
        painter.drawLine(self._numbers.width() - 1, ev.rect().top(), self._numbers.width() - 1, ev.rect().bottom())

    def _update_extra(self) -> None:
        p = theme.palette()
        sels = []
        cur = QTextEdit.ExtraSelection()
        cur.format.setBackground(QColor(p.highlight_line))
        cur.format.setProperty(QTextFormat.Property.FullWidthSelection, True)
        cur.cursor = self.textCursor()
        cur.cursor.clearSelection()
        sels.append(cur)
        if self.exec_line >= 0:
            blk = self.document().findBlockByNumber(self.exec_line)
            if blk.isValid():
                ex = QTextEdit.ExtraSelection()
                col = QColor(p.done)
                col.setAlphaF(0.28)
                ex.format.setBackground(col)
                ex.format.setProperty(QTextFormat.Property.FullWidthSelection, True)
                ex.cursor = QTextCursor(blk)
                sels.append(ex)
        self.setExtraSelections(sels)

    def set_exec_line(self, line: int, follow: bool = True) -> None:
        """Destaca a linha (0-based) em execução e rola até ela."""
        self.exec_line = line
        self._update_extra()
        if follow and line >= 0:
            blk = self.document().findBlockByNumber(line)
            if blk.isValid():
                c = QTextCursor(blk)
                self.setTextCursor(c)
                self.centerCursor()
        self._numbers.update()

    def go_to_line(self, line: int) -> None:
        blk = self.document().findBlockByNumber(max(0, line))
        if blk.isValid():
            self.setTextCursor(QTextCursor(blk))
            self.centerCursor()


class GcodePanel(QWidget):
    """Aba "G-code": barra de ações + busca + editor."""

    saveRequested = Signal()
    sendRequested = Signal()
    previewRequested = Signal()
    openRequested = Signal()

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setObjectName("Panel")
        lay = QVBoxLayout(self)
        lay.setContentsMargins(8, 6, 8, 8)
        lay.setSpacing(6)
        bar = QHBoxLayout()
        bar.setSpacing(4)

        def btn(name: str, text: str, tip: str, sig: Signal) -> QToolButton:
            b = QToolButton()
            b.setIcon(icon(name))
            b.setText(text)
            b.setToolButtonStyle(Qt.ToolButtonStyle.ToolButtonTextBesideIcon)
            b.setToolTip(tip)
            b.setAccessibleName(text.replace("&", ""))
            b.clicked.connect(sig.emit)
            bar.addWidget(b)
            return b

        self.open_btn = btn("open", "Abrir…", "Abrir um arquivo G-code existente (.nc, .gcode, .tap)", self.openRequested)
        self.save_btn = btn("save", "Salvar…", "Salvar o G-code em arquivo (.nc/.gcode)", self.saveRequested)
        self.preview_btn = btn("eye", "Visualizar", "Desenhar este G-code no canvas", self.previewRequested)
        self.send_btn = btn("send", "Enviar à máquina", "Abrir a aba Máquina com este programa pronto para envio", self.sendRequested)
        self.info = QLabel("Nenhum programa")
        self.info.setObjectName("Muted")
        # o texto de estatísticas pode encolher: não deve impor largura mínima ao rodapé
        self.info.setSizePolicy(QSizePolicy.Policy.Ignored, QSizePolicy.Policy.Preferred)
        self.info.setMinimumWidth(0)
        self.info.setAlignment(Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter)
        bar.addWidget(self.info, 1)
        bar.addSpacing(8)
        self.find = QLineEdit()
        self.find.setPlaceholderText("Buscar (Ctrl+F)…")
        self.find.setAccessibleName("Buscar no G-code")
        self.find.setClearButtonEnabled(True)
        self.find.setMaximumWidth(220)
        self.find.setMinimumWidth(110)
        self.find.returnPressed.connect(self._find_next)
        bar.addWidget(self.find)
        lay.addLayout(bar)
        self.editor = CodeEditor()
        lay.addWidget(self.editor, 1)
        QShortcut(QKeySequence.StandardKey.Find, self, self._focus_find,
                  context=Qt.ShortcutContext.WidgetWithChildrenShortcut)
        self.editor.textChanged.connect(self._update_info)
        self.estimate_s = 0.0
        self._update_info()

    def _focus_find(self) -> None:
        self.find.setFocus()
        self.find.selectAll()

    def _find_next(self) -> None:
        txt = self.find.text()
        if not txt:
            return
        if not self.editor.find(txt):
            c = self.editor.textCursor()
            c.movePosition(QTextCursor.MoveOperation.Start)
            self.editor.setTextCursor(c)
            self.editor.find(txt)

    def set_text(self, text: str, estimate_s: float = 0.0) -> None:
        self.estimate_s = estimate_s
        self.editor.setPlainText(text)
        self.editor.set_exec_line(-1, follow=False)

    def text(self) -> str:
        return self.editor.toPlainText()

    def _update_info(self) -> None:
        from .units import fmt_time
        n = self.editor.blockCount() if self.editor.toPlainText() else 0
        has = n > 0
        for b in (self.save_btn, self.preview_btn, self.send_btn):
            b.setEnabled(has)
        if not has:
            self.info.setText("Nenhum programa")
            return
        est = f" · tempo estimado {fmt_time(self.estimate_s)}" if self.estimate_s else ""
        self.info.setText(f"{n:,} linhas".replace(",", ".") + est)
