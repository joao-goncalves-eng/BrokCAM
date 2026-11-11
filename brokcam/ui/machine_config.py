"""Aba "Config. máquina": status completo e edição das configurações do firmware.

Pensada para a MKS DLC32 (Grbl_Esp32), mas funciona com qualquer GRBL 1.1:

* **Status** — firmware/versão, conexão (USB/WiFi/simulador), estado explicado,
  MPos/WPos/WCO, coordenadas ``$#``, estado do parser ``$G`` traduzido, pinos de
  entrada, buffers e overrides;
* **Configurações** — ``$$`` e (Grbl_Esp32) ``$S`` agrupados por categoria, com
  editor validado por tipo, explicação completa no painel de ajuda, destaque do que
  mudou, gravação só do que mudou (com confirmação e avisos de risco), backup/restauração
  em JSON e comparação com o perfil de referência da DLC32;
* **Rede (WiFi)** — modo, SSID, IP e como conectar por ``tcp://IP:23``.

Todos os comandos rodam numa thread auxiliar via :meth:`GrblBridge.run`; os resultados
voltam por sinal (nomes ``cfg:*``). Nada é gravado durante o envio de um programa.
"""
from __future__ import annotations

import html
import json
import logging
import time
from dataclasses import replace
from datetime import datetime
from typing import Any, Callable

from PySide6.QtCore import QEvent, QLocale, QObject, Qt, QTimer, Signal
from PySide6.QtGui import QColor, QFont, QGuiApplication
from PySide6.QtWidgets import (
    QAbstractItemView, QCheckBox, QDialog, QDialogButtonBox, QFileDialog, QFrame, QGridLayout,
    QHBoxLayout, QHeaderView, QLabel, QLineEdit, QListWidget, QListWidgetItem, QMessageBox,
    QProgressBar, QPushButton, QScrollArea, QSplitter, QTabWidget, QTextBrowser,
    QTreeWidget, QTreeWidgetItem, QVBoxLayout, QWidget,
)

from brokcam.machine import firmware as fw
from brokcam.machine.grbl_codes import state_info

from . import backend, theme
from .icons import icon, pixmap
from .machine_panel import Banner, StateChip
from .param_form import Combo, DoubleSpin, HelpPanel, IntSpin

log = logging.getLogger("brokcam.ui.machine_config")

COL_NAME, COL_MACHINE, COL_NEW, COL_UNIT, COL_REF = range(5)
_KEY_ROLE = Qt.ItemDataRole.UserRole


# =========================================================================== editores

class MaskEditor(QWidget):
    """Máscara de bits como caixas de seleção (X/Y/Z ou MPos/Buffer)."""

    changed = Signal()

    def __init__(self, bits: tuple[tuple[int, str], ...], accessible: str) -> None:
        super().__init__()
        lay = QHBoxLayout(self)
        lay.setContentsMargins(0, 0, 0, 0)
        lay.setSpacing(10)
        self.boxes: list[tuple[int, QCheckBox]] = []
        for bit, label in bits:
            cb = QCheckBox(label)
            cb.setAccessibleName(f"{accessible}: {label}")
            cb.toggled.connect(lambda _on: self.changed.emit())
            lay.addWidget(cb)
            self.boxes.append((bit, cb))
        lay.addStretch(1)
        self.setFocusProxy(self.boxes[0][1] if self.boxes else None)

    def value(self) -> int:
        return sum(bit for bit, cb in self.boxes if cb.isChecked())

    def set_value(self, v: int) -> None:
        for bit, cb in self.boxes:
            cb.blockSignals(True)
            cb.setChecked(bool(int(v) & bit))
            cb.blockSignals(False)


class TrimSpin(DoubleSpin):
    """Campo numérico que mostra só os decimais necessários (6000 em vez de 6000,000)."""

    def __init__(self) -> None:
        super().__init__()
        loc = QLocale(self.locale())
        loc.setNumberOptions(QLocale.NumberOption.OmitGroupSeparator)
        self.setLocale(loc)

    def textFromValue(self, v: float) -> str:  # noqa: N802
        loc = self.locale()
        s = loc.toString(float(v), "f", self.decimals())
        dp = loc.decimalPoint()
        if dp in s:
            s = s.rstrip("0").rstrip(dp)
        return s


class _Row:
    """Uma configuração na árvore: definição, valor na máquina e editor."""

    def __init__(self, panel: "MachineConfigPanel", key: str, machine: str | None,
                 parent_item: QTreeWidgetItem) -> None:
        self.panel = panel
        self.key = key
        self.defn = fw.setting_def(key)
        self.machine = machine
        kind = self.defn.kind
        if kind == "bool" and machine is not None:
            try:
                if fw.to_number(machine, "bool") > 1:      # grblHAL: $5 é máscara por eixo
                    kind = "mask"
            except ValueError:
                pass
        self.kind = kind
        # definição usada para validar (o tipo pode ter sido ajustado acima)
        self.vdef = self.defn if kind == self.defn.kind else replace(self.defn, kind=kind, minimum=0, maximum=255)
        self.item = QTreeWidgetItem(parent_item)
        self.item.setData(COL_NAME, _KEY_ROLE, key)
        self.item.setText(COL_NAME, f"${key}  ·  {self.defn.name}")
        self.item.setText(COL_MACHINE, self.display(machine))
        self.item.setText(COL_UNIT, self.defn.unit if self.defn.unit != "máscara" else "")
        tip = self.defn.full_help()
        for c in (COL_NAME, COL_MACHINE, COL_UNIT):
            self.item.setToolTip(c, tip)
        self.item.setTextAlignment(COL_MACHINE, Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter)
        self.editor = self._make_editor()
        self.editor.setAccessibleName(f"Novo valor de ${key}, {self.defn.name}"
                                      + (f" ({self.defn.unit})" if self.defn.unit else ""))
        self.editor.setAccessibleDescription(self.defn.full_help())
        self.editor.setToolTip(tip)
        self.error = ""
        self.set_text(machine if machine is not None else "")

    # ------------------------------------------------------------------ apresentação
    def display(self, value: str | None) -> str:
        if value is None:
            return "—"
        if fw.is_hidden_value(value):
            return "•••••• (oculta)"
        k = self.kind
        try:
            if k == "bool":
                return "Ligado (1)" if fw.to_number(value, "bool") else "Desligado (0)"
            if k == "mask":
                v = int(fw.to_number(value, "mask"))
                bits = self.defn.bits
                names = [lab for bit, lab in bits if v & bit]
                return f"{'+'.join(names) or 'nenhum'} ({v})"
            if k in ("float", "int"):
                return fw._fmt_num(fw.to_number(value), k).replace(".", ",")
        except ValueError:
            pass
        return value if value != "" else "(vazio)"

    # ------------------------------------------------------------------ editor
    def _make_editor(self) -> QWidget:
        d, k = self.defn, self.kind
        if k == "float":
            w = TrimSpin()
            w.setDecimals(3)
            w.setRange(d.minimum if d.minimum is not None else -1e6, d.maximum if d.maximum is not None else 1e6)
            w.setKeyboardTracking(False)
            w.setAlignment(Qt.AlignmentFlag.AlignRight)
            w.setMinimumWidth(110)
            w.valueChanged.connect(self._changed)
            return w
        if k == "int":
            w = IntSpin()
            w.setRange(int(d.minimum if d.minimum is not None else -1_000_000),
                       int(d.maximum if d.maximum is not None else 1_000_000))
            w.setKeyboardTracking(False)
            w.setAlignment(Qt.AlignmentFlag.AlignRight)
            w.setMinimumWidth(110)
            w.valueChanged.connect(self._changed)
            return w
        if k == "bool":
            w = QCheckBox("Desligado")
            w.toggled.connect(lambda on: (w.setText("Ligado" if on else "Desligado"), self._changed()))
            return w
        if k == "mask":
            bits = d.bits if d.kind == "mask" else fw.AXIS_BITS
            w = MaskEditor(bits, f"${self.key}")
            w.changed.connect(self._changed)
            return w
        if k == "enum":
            w = Combo()
            for c in d.choices:
                w.addItem(c, c)
            w.setMinimumWidth(110)
            w.currentIndexChanged.connect(self._changed)
            return w
        w = QLineEdit()
        w.setMinimumWidth(140)
        if k == "password":
            w.setEchoMode(QLineEdit.EchoMode.Password)
            w.setPlaceholderText("oculta — digite para trocar")
        elif k == "ip":
            w.setPlaceholderText("0.0.0.0")
        w.textChanged.connect(self._changed)
        return w

    def text(self) -> str:
        e, k = self.editor, self.kind
        if k == "float":
            return fw._fmt_num(e.value(), "float")  # type: ignore[attr-defined]
        if k == "int":
            return str(int(e.value()))  # type: ignore[attr-defined]
        if k == "bool":
            return "1" if e.isChecked() else "0"  # type: ignore[attr-defined]
        if k == "mask":
            return str(e.value())  # type: ignore[attr-defined]
        if k == "enum":
            return str(e.currentData() or e.currentText())  # type: ignore[attr-defined]
        return e.text()  # type: ignore[attr-defined]

    def set_text(self, value: str) -> None:
        e, k = self.editor, self.kind
        e.blockSignals(True)
        try:
            if k in ("float", "int"):
                try:
                    v = fw.to_number(value)
                except ValueError:
                    v = 0.0
                e.setValue(int(round(v)) if k == "int" else v)  # type: ignore[attr-defined]
            elif k == "bool":
                try:
                    on = bool(fw.to_number(value, "bool"))
                except ValueError:
                    on = False
                e.setChecked(on)  # type: ignore[attr-defined]
                e.setText("Ligado" if on else "Desligado")  # type: ignore[attr-defined]
            elif k == "mask":
                try:
                    e.set_value(int(fw.to_number(value, "mask")))  # type: ignore[attr-defined]
                except ValueError:
                    e.set_value(0)  # type: ignore[attr-defined]
            elif k == "enum":
                idx = next((i for i in range(e.count()) if str(e.itemData(i)).lower() == str(value).lower()), -1)  # type: ignore[attr-defined]
                if idx < 0 and value:
                    e.addItem(value, value)  # type: ignore[attr-defined]
                    idx = e.count() - 1  # type: ignore[attr-defined]
                e.setCurrentIndex(max(0, idx))  # type: ignore[attr-defined]
            elif k == "password":
                e.setText("" if fw.is_hidden_value(value) or value == self.machine else value)  # type: ignore[attr-defined]
            else:
                e.setText(value)  # type: ignore[attr-defined]
        finally:
            e.blockSignals(False)
        self.refresh_state()

    # ------------------------------------------------------------------ estado
    def is_modified(self) -> bool:
        t = self.text()
        if self.kind == "password":
            return t != ""
        if self.machine is None:
            return False
        return not fw.values_equal(self.vdef, t, self.machine)

    def validated(self) -> str:
        """Valor normalizado a enviar (levanta ``ValueError`` se inválido)."""
        return fw.validate_value(self.vdef, self.text())

    def refresh_state(self) -> None:
        mod = self.is_modified()
        self.error = ""
        if mod:
            try:
                self.validated()
            except ValueError as exc:
                self.error = str(exc)
        if bool(self.editor.property("modified")) != mod:
            theme.set_property(self.editor, "modified", mod)
        if bool(self.editor.property("invalid")) != bool(self.error):
            theme.set_property(self.editor, "invalid", bool(self.error))
        p = theme.palette()
        f = self.item.font(COL_NAME)
        f.setBold(mod)
        self.item.setFont(COL_NAME, f)
        base = self.item.text(COL_NAME).lstrip("● ").lstrip("✖ ")
        if self.error:
            self.item.setText(COL_NAME, "✖ " + base)
            self.item.setForeground(COL_NAME, QColor(p.danger))
        elif mod:
            self.item.setText(COL_NAME, "● " + base)
            self.item.setForeground(COL_NAME, QColor(p.accent_fg))
        else:
            self.item.setText(COL_NAME, base)
            self.item.setForeground(COL_NAME, QColor(p.text))
        tip = self.defn.full_help() + (f"\n\n✖ {self.error}" if self.error else "")
        self.editor.setToolTip(tip)

    def _changed(self, *_a: Any) -> None:
        self.refresh_state()
        self.panel._row_changed(self)


# =========================================================================== painel

class _FocusFilter(QObject):
    def __init__(self, fn: Callable[[], None]) -> None:
        super().__init__()
        self.fn = fn

    def eventFilter(self, obj: QObject, ev: QEvent) -> bool:  # noqa: N802
        if ev.type() in (QEvent.Type.FocusIn, QEvent.Type.Enter):
            self.fn()
        return False


def _card(title: str, icon_name: str = "") -> tuple[QFrame, QVBoxLayout]:
    card = QFrame()
    card.setObjectName("Card")
    card.setAccessibleName(title)
    lay = QVBoxLayout(card)
    lay.setContentsMargins(14, 10, 14, 12)
    lay.setSpacing(8)
    head = QHBoxLayout()
    head.setSpacing(6)
    if icon_name:
        ic = QLabel()
        ic.setPixmap(pixmap(icon_name, 16, theme.palette().text_muted))
        ic.setProperty("iconName", icon_name)
        head.addWidget(ic)
    t = QLabel(title)
    t.setObjectName("SectionTitle")
    head.addWidget(t)
    head.addStretch(1)
    lay.addLayout(head)
    return card, lay


def _muted(text: str = "") -> QLabel:
    lab = QLabel(text)
    lab.setObjectName("Muted")
    lab.setWordWrap(True)
    return lab


def _value_label(mono: bool = False) -> QLabel:
    lab = QLabel("—")
    lab.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
    lab.setWordWrap(True)
    if mono:
        lab.setStyleSheet(f"font-family: '{theme.mono_font_family()}';")
    return lab


class MachineConfigPanel(QWidget):
    """Status da máquina e editor de configurações ($$ / $S) do firmware.

    ``bridge`` é o :class:`~brokcam.ui.machine_panel.GrblBridge` do painel Máquina
    (compartilha a mesma conexão). Sinais:

    * ``firmwareDetected(object)`` — :class:`brokcam.machine.firmware.FirmwareInfo`;
    * ``settingsWritten(int)`` — número de configurações gravadas com sucesso.
    """

    firmwareDetected = Signal(object)
    settingsWritten = Signal(int)

    def __init__(self, bridge: Any, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setObjectName("Panel")
        self.setAccessibleName("Configurações da máquina")
        self.bridge = bridge
        self.connected = False
        self.streaming = False
        self.busy = False
        self.confirm_writes = True
        self.fw = fw.FirmwareInfo()
        self.numbered: dict[str, str] = {}      # "110" -> "1000"
        self.extended: dict[str, str] = {}      # "Sta/SSID" -> "MKS_DLC"
        self.params: dict[str, tuple[float, ...]] = {}
        self.parser_words: list[str] = []
        self.rows: dict[str, _Row] = {}
        self.cat_items: dict[str, QTreeWidgetItem] = {}
        self._planner_max = 0
        self._rx_max = 0
        self._last_refresh = 0.0
        self._current_row: _Row | None = None
        self._focus_filters: list[_FocusFilter] = []

        root = QVBoxLayout(self)
        root.setContentsMargins(10, 8, 10, 8)
        root.setSpacing(8)
        root.addLayout(self._build_header())
        self.banner = Banner()
        root.addWidget(self.banner)
        self.tabs = QTabWidget()
        self.tabs.setDocumentMode(True)
        self.tabs.setAccessibleName("Seções da configuração da máquina")
        self.tabs.addTab(self._build_status_tab(), icon("machine"), "Status")
        self.tabs.addTab(self._build_settings_tab(), icon("settings"), "Configurações")
        self.tabs.addTab(self._build_network_tab(), icon("send"), "Rede (WiFi)")
        root.addWidget(self.tabs, 1)

        b = bridge
        b.connection.connect(self._on_connection)
        b.status.connect(self._on_status)
        b.state.connect(self._on_state)
        b.commandResult.connect(self._on_result)
        b.commandFailed.connect(self._on_failed)
        b.streamDone.connect(lambda _ok: self._set_streaming(False))
        if hasattr(b, "streaming"):
            b.streaming.connect(self._set_streaming)
        theme.on_change(self, lambda _p: self._restyle())

        is_conn = getattr(b, "is_connected", None)
        self._on_connection(bool(is_conn()) if callable(is_conn) else False)

    # ================================================================== construção
    def _build_header(self) -> QHBoxLayout:
        row = QHBoxLayout()
        row.setSpacing(10)
        self.fw_icon = QLabel()
        self.fw_icon.setFixedSize(20, 20)
        row.addWidget(self.fw_icon)
        col = QVBoxLayout()
        col.setSpacing(0)
        self.fw_title = QLabel("Configurações da máquina")
        self.fw_title.setObjectName("Heading")
        self.fw_title.setAccessibleName("Firmware detectado")
        self.fw_sub = _muted("Desconectado")
        self.fw_sub.setAccessibleName("Resumo da conexão")
        col.addWidget(self.fw_title)
        col.addWidget(self.fw_sub)
        row.addLayout(col, 1)
        self.chip = StateChip()
        row.addWidget(self.chip, 0, Qt.AlignmentFlag.AlignVCenter)
        self.refresh_btn = QPushButton("Atualizar")
        self.refresh_btn.setIcon(icon("refresh"))
        self.refresh_btn.setToolTip("Ler de novo firmware ($I), coordenadas ($#), parser ($G) e "
                                    "configurações ($$ e $S) da máquina (F5)")
        self.refresh_btn.setAccessibleName("Atualizar status e configurações da máquina")
        self.refresh_btn.setShortcut("F5")
        self.refresh_btn.clicked.connect(self.refresh_all)
        row.addWidget(self.refresh_btn, 0, Qt.AlignmentFlag.AlignVCenter)
        return row

    # ------------------------------------------------------------------ status
    def _build_status_tab(self) -> QWidget:
        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setFrameShape(QFrame.Shape.NoFrame)
        host = QWidget()
        host.setObjectName("PanelBody")
        self.status_grid = QGridLayout(host)
        self.status_grid.setContentsMargins(0, 10, 4, 4)
        self.status_grid.setSpacing(10)
        scroll.setWidget(host)
        self.cards: list[QFrame] = []

        # firmware e conexão
        card, lay = _card("Firmware e conexão", "info")
        g = QGridLayout()
        g.setHorizontalSpacing(12)
        g.setVerticalSpacing(4)
        self.st_fw: dict[str, QLabel] = {}
        for r, (k, t) in enumerate((("fw", "Firmware"), ("ver", "Versão / build"), ("machine", "Placa"),
                                    ("conn", "Conexão"), ("opt", "Opções"))):
            lab = _muted(t)
            val = _value_label()
            val.setAccessibleName(t)
            lab.setBuddy(val)
            g.addWidget(lab, r, 0, Qt.AlignmentFlag.AlignTop)
            g.addWidget(val, r, 1)
            self.st_fw[k] = val
        g.setColumnStretch(1, 1)
        lay.addLayout(g)
        lay.addStretch(1)
        self.cards.append(card)

        # estado
        card, lay = _card("Estado da máquina", "machine")
        self.st_state = QLabel("—")
        self.st_state.setObjectName("Heading")
        self.st_state.setAccessibleName("Estado da máquina")
        self.st_meaning = QLabel("Conecte-se para ver o estado.")
        self.st_meaning.setWordWrap(True)
        self.st_action = _muted("")
        lay.addWidget(self.st_state)
        lay.addWidget(self.st_meaning)
        lay.addWidget(self.st_action)
        state_lay = lay
        self.cards.append(card)

        # posição
        card, lay = _card("Posição", "move")
        g = QGridLayout()
        g.setHorizontalSpacing(14)
        g.setVerticalSpacing(4)
        for c, ax in enumerate("XYZ"):
            h = _muted(ax)
            h.setAlignment(Qt.AlignmentFlag.AlignRight)
            g.addWidget(h, 0, c + 1)
        self.st_pos: dict[str, list[QLabel]] = {}
        tips = {"WPos": "Posição de trabalho (relativa ao zero G54…)",
                "MPos": "Posição de máquina (relativa ao homing/ligação)",
                "WCO": "Deslocamento de trabalho = MPos − WPos"}
        for r, key in enumerate(("WPos", "MPos", "WCO")):
            lab = _muted(key)
            lab.setToolTip(tips[key])
            g.addWidget(lab, r + 1, 0)
            vals = []
            for c, ax in enumerate("XYZ"):
                v = _value_label(mono=True)
                v.setAlignment(Qt.AlignmentFlag.AlignRight)
                v.setMinimumWidth(70)
                v.setAccessibleName(f"{tips[key]} {ax}")
                g.addWidget(v, r + 1, c + 1)
                vals.append(v)
            self.st_pos[key] = vals
        lay.addLayout(g)
        self.st_feed = _muted("Avanço — · Spindle —")
        lay.addWidget(self.st_feed)
        lay.addStretch(1)
        self.cards.append(card)

        # entradas
        card, lay = _card("Entradas (pinos)", "probe")
        g = QGridLayout()
        g.setHorizontalSpacing(8)
        g.setVerticalSpacing(4)
        self.st_pins: dict[str, QLabel] = {}
        for i, (k, name) in enumerate((("X", "Limite X"), ("Y", "Limite Y"), ("Z", "Limite Z"),
                                       ("P", "Sonda"), ("D", "Porta"), ("H", "Pausa"),
                                       ("R", "Reset"), ("S", "Início"))):
            lab = QLabel()
            lab.setAccessibleName(name)
            g.addWidget(lab, i // 2, i % 2)
            self.st_pins[k] = lab
        lay.addLayout(g)
        lay.addWidget(_muted("Um limite 'acionado' sem nada encostado indica $5 (inverter fins de "
                             "curso) errado ou fio solto; a sonda acionada sem tocar indica $6 errado."))
        lay.addStretch(1)
        self.cards.append(card)

        # buffers e overrides (no cartão de estado)
        lay = state_lay
        sep = QFrame()
        sep.setFrameShape(QFrame.Shape.HLine)
        lay.addWidget(sep)
        g = QGridLayout()
        g.setHorizontalSpacing(10)
        g.setVerticalSpacing(6)
        self.st_planner = QProgressBar()
        self.st_planner.setAccessibleName("Uso do planejador de movimentos")
        self.st_rx = QProgressBar()
        self.st_rx.setAccessibleName("Uso do buffer de recepção")
        for bar in (self.st_planner, self.st_rx):
            bar.setRange(0, 1)
            bar.setValue(0)
            bar.setFormat("—")
            bar.setMaximumHeight(16)
        g.addWidget(_muted("Planejador"), 0, 0)
        g.addWidget(self.st_planner, 0, 1)
        g.addWidget(_muted("Buffer RX"), 1, 0)
        g.addWidget(self.st_rx, 1, 1)
        self.st_ov = _value_label()
        self.st_ov.setAccessibleName("Overrides")
        g.addWidget(_muted("Overrides"), 2, 0)
        g.addWidget(self.st_ov, 2, 1)
        self.st_line = _value_label()
        g.addWidget(_muted("Linha"), 3, 0)
        g.addWidget(self.st_line, 3, 1)
        g.setColumnStretch(1, 1)
        lay.addLayout(g)
        self.st_buf_note = _muted("Uso dos buffers: ligue o bit 'Buffer' de $10 para o GRBL reportá-lo.")
        lay.addWidget(self.st_buf_note)
        lay.addStretch(1)

        # parser
        card, lay = _card("Estado do parser ($G)", "gcode")
        self.st_parser = QGridLayout()
        self.st_parser.setHorizontalSpacing(12)
        self.st_parser.setVerticalSpacing(3)
        self.st_parser.setColumnStretch(2, 1)
        lay.addLayout(self.st_parser)
        lay.addStretch(1)
        self.cards.append(card)

        # coordenadas
        card, lay = _card("Coordenadas de trabalho ($#)", "zero")
        self.st_coords = QGridLayout()
        self.st_coords.setHorizontalSpacing(14)
        self.st_coords.setVerticalSpacing(3)
        lay.addLayout(self.st_coords)
        lay.addWidget(_muted("G54–G59: zeros de trabalho · G28/G30: posições guardadas · G92: "
                             "deslocamento temporário · TLO: compensação de comprimento · PRB: último "
                             "toque da sonda."))
        lay.addStretch(1)
        self.cards.append(card)

        fwc, est, pos, pins, parser, coords = self.cards
        self.cards = [fwc, est, pos, coords, parser, pins]
        self._status_cols = 0
        self._relayout_cards()
        self._render_pins("")
        return scroll

    def _relayout_cards(self) -> None:
        w = max(1, self.width())
        if hasattr(self, "net_cards"):
            ncols = 2 if w >= 900 else 1
            if ncols != self._net_cols:
                self._net_cols = ncols
                for i, c in enumerate(self.net_cards):
                    self.net_grid.removeWidget(c)
                    self.net_grid.addWidget(c, i // ncols, i % ncols)
                for c in range(2):
                    self.net_grid.setColumnStretch(c, 1 if c < ncols else 0)
                self.net_grid.setRowStretch(2, 1)
        cols = 3 if w >= 1150 else 2 if w >= 720 else 1
        if cols == self._status_cols:
            return
        self._status_cols = cols
        for c in self.cards:
            self.status_grid.removeWidget(c)
        for i, c in enumerate(self.cards):
            self.status_grid.addWidget(c, i // cols, i % cols)
        for c in range(3):
            self.status_grid.setColumnStretch(c, 1 if c < cols else 0)
        self.status_grid.setRowStretch(len(self.cards) // cols + 1, 1)

    def resizeEvent(self, ev) -> None:  # type: ignore[no-untyped-def]  # noqa: N802
        super().resizeEvent(ev)
        self._relayout_cards()

    # ------------------------------------------------------------------ configurações
    def _build_settings_tab(self) -> QWidget:
        w = QWidget()
        lay = QVBoxLayout(w)
        lay.setContentsMargins(0, 8, 0, 0)
        lay.setSpacing(8)

        bar = QHBoxLayout()
        bar.setSpacing(6)

        def btn(text: str, ic: str, tip: str, slot: Callable[[], None]) -> QPushButton:
            b = QPushButton(text)
            b.setIcon(icon(ic))
            b.setToolTip(tip)
            b.setAccessibleName(text.replace("&", "").replace("…", ""))
            b.setAccessibleDescription(tip)
            b.clicked.connect(slot)
            return b

        self.read_btn = btn("Ler da máquina", "refresh", "Ler $$ e, no Grbl_Esp32/DLC32, $S.", self.refresh_all)
        self.write_btn = btn("&Gravar alterações", "save",
                             "Enviar à máquina só as configurações alteradas (pede confirmação).",
                             self.write_changes)
        self.write_btn.setProperty("primary", True)
        self.discard_btn = btn("Descartar", "undo", "Voltar todos os campos ao valor lido da máquina.",
                               self.discard_changes)
        self.backup_btn = btn("Backup…", "export", "Salvar os valores lidos da máquina num arquivo JSON.",
                              self.backup_dialog)
        self.restore_btn = btn("Restaurar…", "import",
                               "Carregar um backup nos campos 'Novo valor' (nada é gravado até você "
                               "confirmar em Gravar alterações).", self.restore_dialog)
        self.compare_btn = btn("Comparar com DLC32", "measure",
                               f"Mostrar a coluna de referência: {fw.DLC32_PROFILE_NAME}.",
                               lambda: self.set_compare(self.compare_btn.isChecked()))
        self.compare_btn.setCheckable(True)
        self.use_ref_btn = btn("Usar referência", "restore",
                               "Copiar os valores de referência (exceto os que dependem da mecânica) "
                               "para os campos 'Novo valor'. Nada é gravado sem confirmação.",
                               self.apply_reference)
        self.use_ref_btn.setVisible(False)
        for b in (self.read_btn, self.write_btn, self.discard_btn):
            bar.addWidget(b)
        sep = QFrame()
        sep.setFrameShape(QFrame.Shape.VLine)
        bar.addWidget(sep)
        for b in (self.backup_btn, self.restore_btn, self.compare_btn, self.use_ref_btn):
            bar.addWidget(b)
        bar.addStretch(1)
        lay.addLayout(bar)

        info = QHBoxLayout()
        info.setSpacing(8)
        self.filter_edit = QLineEdit()
        self.filter_edit.setPlaceholderText("Filtrar ($110, velocidade, WiFi…)")
        self.filter_edit.setClearButtonEnabled(True)
        self.filter_edit.setAccessibleName("Filtrar configurações")
        self.filter_edit.setMaximumWidth(300)
        self.filter_edit.setMinimumWidth(220)
        self.filter_edit.textChanged.connect(self._apply_filter)
        self.summary = _muted("Nenhuma configuração lida ainda. Conecte-se e clique em “Ler da máquina”.")
        self.summary.setAccessibleName("Resumo das alterações")
        info.addWidget(self.summary, 1)
        info.addWidget(self.filter_edit)
        lay.addLayout(info)

        split = QSplitter(Qt.Orientation.Horizontal)
        split.setChildrenCollapsible(False)
        self.tree = QTreeWidget()
        self.tree.setColumnCount(5)
        self.tree.setHeaderLabels(["Configuração", "Na máquina", "Novo valor", "Unidade",
                                   "Referência DLC32"])
        self.tree.setAlternatingRowColors(True)
        self.tree.setUniformRowHeights(False)
        self.tree.setSelectionMode(QAbstractItemView.SelectionMode.SingleSelection)
        self.tree.setAccessibleName("Configurações do firmware agrupadas por categoria")
        self.tree.setAccessibleDescription(
            "Use as setas para navegar e Tab para ir ao campo Novo valor. A explicação completa "
            "aparece no painel de ajuda ao lado.")
        hh = self.tree.header()
        hh.setStretchLastSection(False)
        hh.setSectionResizeMode(COL_NAME, QHeaderView.ResizeMode.Stretch)
        hh.setSectionResizeMode(COL_MACHINE, QHeaderView.ResizeMode.Interactive)
        hh.setSectionResizeMode(COL_NEW, QHeaderView.ResizeMode.Interactive)
        hh.setSectionResizeMode(COL_UNIT, QHeaderView.ResizeMode.ResizeToContents)
        hh.setSectionResizeMode(COL_REF, QHeaderView.ResizeMode.Interactive)
        self.tree.setColumnWidth(COL_MACHINE, 130)
        self.tree.setColumnWidth(COL_NEW, 190)
        self.tree.setColumnWidth(COL_REF, 150)
        self.tree.setColumnHidden(COL_REF, True)
        # caixas de seleção dentro da árvore herdariam a borda de foco da view
        self.tree.setStyleSheet("QTreeWidget QCheckBox { border: none; padding: 0px; min-height: 22px; }")
        self.tree.currentItemChanged.connect(self._on_current_item)
        split.addWidget(self.tree)
        self.help = HelpPanel()
        self.help.setMinimumWidth(260)
        self.help.set_default("Configurações do firmware",
                              "Selecione uma configuração para ver o que ela faz, o valor típico e os "
                              "riscos de mudar.\n\nOs valores só são enviados à máquina quando você "
                              "clica em “Gravar alterações” e confirma.")
        split.addWidget(self.help)
        split.setStretchFactor(0, 3)
        split.setStretchFactor(1, 1)
        split.setSizes([800, 320])
        lay.addWidget(split, 1)
        return w

    # ------------------------------------------------------------------ rede
    def _build_network_tab(self) -> QWidget:
        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setFrameShape(QFrame.Shape.NoFrame)
        host = QWidget()
        host.setObjectName("PanelBody")
        grid = QGridLayout(host)
        grid.setContentsMargins(0, 10, 4, 4)
        grid.setSpacing(10)
        scroll.setWidget(host)

        card, lay = _card("WiFi da placa", "send")
        self.net_note = _muted("")
        lay.addWidget(self.net_note)
        g = QGridLayout()
        g.setHorizontalSpacing(12)
        g.setVerticalSpacing(4)
        self.net: dict[str, QLabel] = {}
        for r, (k, t) in enumerate((("mode", "Modo do rádio"), ("ssid", "Rede (SSID)"), ("ip", "Endereço IP"),
                                    ("status", "Situação"), ("mac", "MAC"), ("hostname", "Hostname"),
                                    ("telnet", "Telnet"), ("http", "WebUI (HTTP)"))):
            lab = _muted(t)
            val = _value_label(mono=k in ("ip", "mac"))
            val.setAccessibleName(t)
            lab.setBuddy(val)
            g.addWidget(lab, r, 0, Qt.AlignmentFlag.AlignTop)
            g.addWidget(val, r, 1)
            self.net[k] = val
        g.setColumnStretch(1, 1)
        lay.addLayout(g)
        btns = QHBoxLayout()
        self.edit_net_btn = QPushButton("Editar rede…")
        self.edit_net_btn.setIcon(icon("edit-path"))
        self.edit_net_btn.setToolTip("Abrir a categoria Rede / WiFi na aba Configurações.")
        self.edit_net_btn.clicked.connect(self.show_network_settings)
        self.scan_btn = QPushButton("Procurar redes")
        self.scan_btn.setIcon(icon("search"))
        self.scan_btn.setToolTip("Listar as redes WiFi que a placa enxerga ($WiFi/ListAPs).")
        self.scan_btn.clicked.connect(self.scan_networks)
        self.restart_btn = QPushButton("Reiniciar placa…")
        self.restart_btn.setIcon(icon("reset"))
        self.restart_btn.setProperty("danger", True)
        self.restart_btn.setToolTip("Reinicia o ESP32 ($System/Control=RESTART) para aplicar mudanças de WiFi.")
        self.restart_btn.clicked.connect(self.restart_board)
        for b in (self.edit_net_btn, self.scan_btn, self.restart_btn):
            b.setAccessibleName(b.text().replace("…", ""))
            btns.addWidget(b)
        btns.addStretch(1)
        lay.addLayout(btns)
        self.ap_list = QListWidget()
        self.ap_list.setAccessibleName("Redes WiFi encontradas")
        self.ap_list.setMaximumHeight(120)
        self.ap_list.hide()
        lay.addWidget(self.ap_list)
        lay.addStretch(1)
        self.net_grid = grid
        self.net_cards = [card]

        card, lay = _card("Conectar o BrokCAM pelo WiFi", "plug")
        intro = QLabel(
            "A MKS DLC32 (e outras placas Grbl_Esp32) aceita a mesma comunicação da USB por um "
            "servidor Telnet na porta 23. Para usar sem cabo:")
        intro.setWordWrap(True)
        lay.addWidget(intro)
        steps = QLabel(
            "<ol style='margin-left:-20px'>"
            "<li><b>Modo AP</b> (padrão): conecte o computador à rede <b>MKS_DLC</b> "
            "(senha de fábrica 12345678). O endereço é 192.168.4.1.</li>"
            "<li><b>Modo STA</b>: a placa entra na rede do roteador; use o IP mostrado ao lado "
            "(ou na tela da DLC32).</li>"
            "<li>Na aba <b>Máquina</b>, digite o endereço abaixo no campo da porta e clique em "
            "<b>Conectar</b>. O baud é ignorado no WiFi.</li></ol>")
        steps.setWordWrap(True)
        steps.setTextFormat(Qt.TextFormat.RichText)
        lay.addWidget(steps)
        row = QHBoxLayout()
        self.url_edit = QLineEdit("tcp://192.168.4.1:23")
        self.url_edit.setReadOnly(True)
        self.url_edit.setAccessibleName("Endereço para conectar pelo WiFi")
        self.url_edit.setStyleSheet(f"font-family: '{theme.mono_font_family()}';")
        copy = QPushButton("Copiar")
        copy.setIcon(icon("export"))
        copy.setAccessibleName("Copiar endereço de conexão WiFi")
        copy.clicked.connect(lambda: QGuiApplication.clipboard().setText(self.url_edit.text()))
        row.addWidget(self.url_edit, 1)
        row.addWidget(copy)
        lay.addLayout(row)
        lay.addWidget(_muted(
            "Dicas: o ESP32 só usa WiFi 2,4 GHz. Pelo WiFi a latência é maior que na USB; para "
            "trabalhos longos prefira o cabo. Mudanças de rede só valem após reiniciar a placa e "
            "podem derrubar esta conexão."))
        lay.addStretch(1)
        self.net_cards.append(card)
        self._net_cols = 0
        self._relayout_cards()
        return scroll

    # ================================================================== conexão / estado
    def _ctrl(self) -> Any:
        return getattr(self.bridge, "ctrl", None)

    def _on_connection(self, ok: bool) -> None:
        self.connected = bool(ok)
        if not ok:
            self._set_streaming(False)
            self.chip.set_state("")
            self.banner.show_message(
                "warning", "Máquina desconectada",
                "Conecte-se na aba Máquina (porta USB, SIMULADOR, SIMULADOR-DLC32 ou tcp://IP:23 pelo "
                "WiFi) para ler o status e editar as configurações. Os valores mostrados abaixo são da "
                "última leitura.")
        else:
            self.banner.hide()
            QTimer.singleShot(250, self.refresh_all)
        self._update_header()
        self._update_buttons()

    def _set_streaming(self, on: bool) -> None:
        if self.streaming == bool(on):
            return
        self.streaming = bool(on)
        if on:
            self.banner.show_message(
                "warning", "Programa em execução",
                "Enquanto um programa é enviado as configurações não podem ser lidas nem gravadas. "
                "O status continua atualizando.")
        elif self.connected:
            self.banner.hide()
        self._update_buttons()

    def _update_buttons(self) -> None:
        live = self.connected and not self.streaming and not self.busy
        has_rows = bool(self.rows)
        n_mod = sum(1 for r in self.rows.values() if r.is_modified())
        n_err = sum(1 for r in self.rows.values() if r.error)
        self.refresh_btn.setEnabled(live)
        self.read_btn.setEnabled(live)
        self.write_btn.setEnabled(live and n_mod > 0 and n_err == 0)
        self.discard_btn.setEnabled(n_mod > 0 and not self.busy)
        self.backup_btn.setEnabled(has_rows)
        self.restore_btn.setEnabled(has_rows and not self.busy)
        self.use_ref_btn.setEnabled(has_rows and not self.busy)
        self.tree.setEnabled(not self.busy)
        esp = self.fw.supports_extended
        self.scan_btn.setEnabled(live and esp)
        self.restart_btn.setEnabled(live and bool(self.fw.restart_command))
        self.edit_net_btn.setEnabled(esp and "Rede / WiFi" in self.cat_items)
        if self.busy:
            self.summary.setText("Comunicando com a máquina…")
        elif has_rows:
            parts = [f"{len(self.rows)} configurações"]
            if n_mod:
                parts.append(f"{n_mod} alterada(s) — destacadas com ●")
            if n_err:
                parts.append(f"{n_err} inválida(s) — marcadas com ✖")
            if self._last_refresh:
                parts.append("lidas às " + time.strftime("%H:%M:%S", time.localtime(self._last_refresh)))
            self.summary.setText(" · ".join(parts))

    def _update_header(self) -> None:
        p = theme.palette()
        ctrl = self._ctrl()
        if not self.connected:
            self.fw_title.setText("Configurações da máquina")
            self.fw_sub.setText("Desconectado")
            self.fw_icon.setPixmap(pixmap("unplug", 20, p.text_muted, self.devicePixelRatioF()))
        else:
            kind = getattr(ctrl, "connection_kind", "") or ""
            port = getattr(ctrl, "port", "") or ""
            conn = {"wifi": f"WiFi ({port})", "serial": f"USB/serial ({port})",
                    "simulador": f"Simulador ({port})"}.get(kind, port or "conectado")
            self.fw_title.setText(self.fw.label if self.fw.kind != "desconhecido" else "Firmware não identificado")
            ver = " · ".join(x for x in (f"versão {self.fw.version}" if self.fw.version else "",
                                         f"build {self.fw.build}" if self.fw.build else "") if x)
            self.fw_sub.setText(" · ".join(x for x in (ver, conn) if x))
            self.fw_icon.setPixmap(pixmap("send" if kind == "wifi" else "plug", 20, p.accent,
                                          self.devicePixelRatioF()))
            self.st_fw["conn"].setText(conn)
        self.fw_title.setAccessibleDescription(self.fw_sub.text())

    def _on_state(self, state: str) -> None:
        self.chip.set_state(state if self.connected else "")
        info = state_info(state) if state else None
        self.st_state.setText(f"{info.title} ({state})" if info else "—")
        self.st_meaning.setText(info.meaning if info else "Conecte-se para ver o estado.")
        self.st_action.setText(("O que fazer: " + info.action) if info else "")

    def _on_status(self, raw: dict) -> None:
        st = backend.normalize_status(raw)
        if st["state"] and st["state"] != self.chip.state:
            self._on_state(st["state"])
        for key in ("WPos", "MPos", "WCO"):
            v = st[key.lower()]
            for i, lab in enumerate(self.st_pos[key]):
                lab.setText("—" if v is None else f"{v[i]:.3f}".replace(".", ","))
        f, s = st["feed"], st["spindle"]
        if f is not None or s is not None:
            self.st_feed.setText(f"Avanço atual {float(f or 0):.0f} mm/min · Spindle/laser S{float(s or 0):.0f}")
        self._render_pins(st["pins"] or "")
        low = {str(k).lower(): v for k, v in raw.items()}
        pf, rf = low.get("planner_free"), low.get("rx_free")
        self.st_buf_note.setVisible(pf is None)
        if pf is not None:
            self._planner_max = max(self._planner_max, int(pf))
            self._bar(self.st_planner, int(pf), self._planner_max, "blocos")
        if rf is not None:
            self._rx_max = max(self._rx_max, int(rf))
            self._bar(self.st_rx, int(rf), self._rx_max, "bytes")
        ov = st["ov"]
        if ov and len(ov) >= 3:
            self.st_ov.setText(f"Avanço {ov[0]}% · Rápido {ov[1]}% · Spindle {ov[2]}%")
        ln = st["line"]
        self.st_line.setText(str(ln) if ln else "—")

    @staticmethod
    def _bar(bar: QProgressBar, free: int, total: int, unit: str) -> None:
        total = max(total, 1)
        used = max(0, total - free)
        bar.setRange(0, total)
        bar.setValue(used)
        bar.setFormat(f"{used}/{total} {unit} em uso")

    def _render_pins(self, pins: str) -> None:
        p = theme.palette()
        state = fw.describe_pins(pins)
        for k, lab in self.st_pins.items():
            on = state.get(k, False)
            name = lab.accessibleName()
            col = p.danger if on else p.success
            text = "acionado" if on else "livre"
            sym = "▲" if on else "●"
            lab.setText(f"<span style='color:{col}'>{sym}</span> {html.escape(name)}: "
                        f"<b style='color:{col if on else p.text}'>{text}</b>")
            lab.setAccessibleDescription(f"{name}: {text}")

    # ================================================================== leitura
    def refresh_all(self) -> None:
        """Lê firmware, $#, $G, $$ e $S numa thread auxiliar."""
        ctrl = self._ctrl()
        if ctrl is None or not self.connected or self.streaming or self.busy:
            return
        self.busy = True
        self._update_buttons()

        def work() -> dict:
            out: dict[str, Any] = {"errors": []}
            try:
                out["firmware"] = ctrl.detect_firmware() if hasattr(ctrl, "detect_firmware") else None
            except Exception as exc:  # noqa: BLE001
                out["errors"].append(f"$I: {exc}")
            for key, name in (("params", "read_parameters"), ("parser", "read_parser_state"),
                              ("settings", "read_settings")):
                fn = getattr(ctrl, name, None)
                if fn is None:
                    continue
                try:
                    out[key] = fn()
                except Exception as exc:  # noqa: BLE001
                    out["errors"].append(f"{name}: {exc}")
            f = out.get("firmware")
            if f is not None and getattr(f, "supports_extended", False) and hasattr(ctrl, "read_extended_settings"):
                try:
                    out["extended"] = ctrl.read_extended_settings()
                except Exception as exc:  # noqa: BLE001
                    out["errors"].append(f"$S: {exc}")
            return out

        self.bridge.run("cfg:refresh", work)

    def _on_result(self, name: str, result: Any) -> None:
        if not name.startswith("cfg:"):
            return
        if name == "cfg:refresh":
            self.busy = False
            self._apply_refresh(result or {})
        elif name == "cfg:write":
            self.busy = False
            self._after_write(result or {})
        elif name == "cfg:scan":
            self._show_networks(result or [])
        elif name == "cfg:restart":
            QMessageBox.information(self, "Reiniciar placa",
                                    "Comando de reinício enviado. Se estiver conectado pelo WiFi, a "
                                    "conexão vai cair; reconecte em alguns segundos.")
        self._update_buttons()

    def _on_failed(self, name: str, msg: str) -> None:
        if not name.startswith("cfg:"):
            return
        self.busy = False
        log.warning("%s falhou: %s", name, msg)
        if name == "cfg:write":
            self.refresh_all()
        QMessageBox.warning(self, "Configurações da máquina", f"Falha na comunicação:\n{msg}")
        self._update_buttons()

    def _apply_refresh(self, res: dict) -> None:
        f = res.get("firmware")
        if isinstance(f, fw.FirmwareInfo):
            self.fw = f
            self.firmwareDetected.emit(f)
        elif self._ctrl() is not None and isinstance(getattr(self._ctrl(), "firmware", None), fw.FirmwareInfo):
            self.fw = self._ctrl().firmware
        if "params" in res:
            self.params = dict(res["params"])
        if "parser" in res:
            self.parser_words = list(res["parser"])
        numbered: dict[str, str] = {}
        if isinstance(res.get("settings"), dict):
            for k, v in res["settings"].items():
                d = fw.setting_def(str(k))
                try:
                    numbered[str(int(k))] = fw._fmt_num(float(v), d.kind if d.kind != "string" else "float")
                except (TypeError, ValueError):
                    numbered[str(int(k))] = str(v)
        extended: dict[str, str] = {}
        if isinstance(res.get("extended"), dict):
            for k, v in res["extended"].items():
                num = fw.numbered_equivalent(k)
                if num is not None:
                    if num not in numbered and num in fw.NUMBERED:     # ex.: $34–$36 só no $S/$+
                        numbered[num] = str(v)
                    continue
                extended[k] = str(v)
        if numbered or extended:
            self.numbered, self.extended = numbered, extended
            self._last_refresh = time.time()
            self._build_tree()
        self._render_firmware()
        self._render_coords()
        self._render_parser()
        self._render_network()
        self._update_header()
        if res.get("errors"):
            log.info("Leitura parcial: %s", "; ".join(res["errors"]))
            self.summary.setToolTip("Avisos da última leitura:\n" + "\n".join(res["errors"]))

    # ================================================================== status (render)
    def _render_firmware(self) -> None:
        f = self.fw
        self.st_fw["fw"].setText(f.label)
        self.st_fw["ver"].setText(" · ".join(x for x in (f.version, f.build) if x) or "—")
        self.st_fw["machine"].setText(f.machine or ("—" if f.kind != "grbl" else "Arduino (GRBL clássico)"))
        opts = fw.decode_options(f.options, f.kind)
        if opts:
            self.st_fw["opt"].setText(" · ".join(t for _, t in opts))
            self.st_fw["opt"].setToolTip("\n".join(f"{c}: {t}" for c, t in opts))
            self.st_fw["opt"].setAccessibleDescription("; ".join(t for _, t in opts))
        else:
            self.st_fw["opt"].setText("—")

    def _clear_grid(self, g: QGridLayout) -> None:
        while g.count():
            it = g.takeAt(0)
            w = it.widget()
            if w is not None:
                w.deleteLater()

    def _render_coords(self) -> None:
        g = self.st_coords
        self._clear_grid(g)
        if not self.params:
            g.addWidget(_muted("Sem leitura."), 0, 0)
            return
        for c, ax in enumerate("XYZ"):
            h = _muted(ax)
            h.setAlignment(Qt.AlignmentFlag.AlignRight)
            g.addWidget(h, 0, c + 1)
        order = ["G54", "G55", "G56", "G57", "G58", "G59", "G28", "G30", "G92", "TLO", "PRB"]
        r = 1
        active = next((w for w in self.parser_words if w.startswith("G5") and w[1:3].isdigit()), "G54")
        for key in order + [k for k in self.params if k not in order]:
            if key not in self.params:
                continue
            v = self.params[key]
            lab = QLabel(("▸ " if key == active else "") + key)
            if key == active:
                f = lab.font()
                f.setBold(True)
                lab.setFont(f)
                lab.setToolTip("Sistema de coordenadas ativo")
            else:
                lab.setObjectName("Muted")
            g.addWidget(lab, r, 0)
            if key == "TLO":
                val = _value_label(mono=True)
                val.setText(f"{v[0]:.3f}".replace(".", ",") if v else "—")
                val.setAlignment(Qt.AlignmentFlag.AlignRight)
                g.addWidget(val, r, 3)
            else:
                for c in range(3):
                    val = _value_label(mono=True)
                    val.setAlignment(Qt.AlignmentFlag.AlignRight)
                    val.setText(f"{v[c]:.3f}".replace(".", ",") if len(v) > c else "—")
                    val.setAccessibleName(f"{key} {'XYZ'[c]}")
                    g.addWidget(val, r, c + 1)
                if key == "PRB" and len(v) > 3:
                    ok = _muted("tocou" if v[3] else "não tocou")
                    g.addWidget(ok, r, 4)
            r += 1

    def _render_parser(self) -> None:
        g = self.st_parser
        self._clear_grid(g)
        items = fw.describe_parser_state(self.parser_words)
        if not items:
            g.addWidget(_muted("Sem leitura."), 0, 0)
            return
        mono = theme.mono_font_family()
        for r, it in enumerate(items):
            grp = _muted(it.group)
            code = QLabel(it.code)
            code.setStyleSheet(f"font-family: '{mono}'; font-weight: 600;")
            mean = QLabel(it.meaning)
            mean.setWordWrap(True)
            mean.setAccessibleName(f"{it.group}: {it.meaning}")
            g.addWidget(grp, r, 0)
            g.addWidget(code, r, 1)
            g.addWidget(mean, r, 2)

    def _render_network(self) -> None:
        f = self.fw
        e = self.extended
        for lab in self.net.values():
            lab.setText("—")
        if not f.is_esp32:
            kind = f.label if f.kind != "desconhecido" else "Este firmware"
            self.net_note.setText(f"{kind} não tem WiFi. A MKS DLC32 e outras placas com Grbl_Esp32 "
                                  "têm; conecte-se a uma delas para ver e editar a rede.")
            return
        self.net_note.setText("Situação atual da rede (do $I) e configurações gravadas (do $S).")
        w = f.wifi[0] if f.wifi else {}
        mode = (w.get("mode") or e.get("Radio/Mode") or "—").upper()
        mode_txt = {"AP": "AP — a placa cria a própria rede", "STA": "STA — conectada ao roteador",
                    "OFF": "Desligado (só USB)", "NONE": "Desligado (só USB)"}.get(mode, mode)
        self.net["mode"].setText(mode_txt)
        ssid = w.get("ssid") or (e.get("AP/SSID") if mode == "AP" else e.get("Sta/SSID")) or "—"
        self.net["ssid"].setText(ssid)
        ip = w.get("ip") or (e.get("AP/IP") if mode == "AP" else "") or "—"
        self.net["ip"].setText(ip)
        self.net["status"].setText(w.get("status") or ("Rede própria ativa" if mode == "AP" else "—"))
        self.net["mac"].setText(w.get("mac") or "—")
        self.net["hostname"].setText(e.get("System/Hostname") or "—")
        tel_on = (e.get("Telnet/Enable") or "ON").upper() == "ON"
        tport = e.get("Telnet/Port") or "23"
        self.net["telnet"].setText(f"{'Ligado' if tel_on else 'DESLIGADO'} · porta {tport}")
        hport = e.get("Http/Port") or "80"
        h_on = (e.get("Http/Enable") or "ON").upper() == "ON"
        self.net["http"].setText(f"{'Ligado' if h_on else 'Desligado'} · http://{ip if ip != '—' else 'IP'}"
                                 + (f":{hport}" if hport != "80" else ""))
        if ip and ip != "—" and ip != "0.0.0.0":
            self.url_edit.setText(f"tcp://{ip}:{tport}")

    # ================================================================== árvore
    def _build_tree(self) -> None:
        cur_key = self._current_row.key if self._current_row else None
        self.tree.clear()
        self.rows.clear()
        self.cat_items.clear()
        self._current_row = None
        self._focus_filters.clear()
        keys = list(self.numbered) + list(self.extended)
        cats = fw.categorize(keys)
        bold = QFont(self.tree.font())
        bold.setBold(True)
        prev: QWidget | None = None
        holders: list[QWidget] = []
        for cat, ks in cats.items():
            top = QTreeWidgetItem(self.tree)
            label = cat
            if cat == "Rede / WiFi":
                label += "  —  ⚠ mudanças podem desconectar a placa e exigem reinício"
            top.setText(COL_NAME, f"{label}  ({len(ks)})")
            top.setFont(COL_NAME, bold)
            top.setFirstColumnSpanned(True)
            top.setFlags(Qt.ItemFlag.ItemIsEnabled | Qt.ItemFlag.ItemIsSelectable)
            top.setData(COL_NAME, _KEY_ROLE, None)
            self.cat_items[cat] = top
            for k in ks:
                machine = self.numbered.get(k) if k in self.numbered else self.extended.get(k)
                row = _Row(self, k, machine, top)
                self.rows[k] = row
                holder = QWidget()
                hl = QHBoxLayout(holder)
                hl.setContentsMargins(2, 2, 6, 2)
                hl.addWidget(row.editor)
                self.tree.setItemWidget(row.item, COL_NEW, holder)
                holders.append(holder)
                filt = _FocusFilter(lambda r=row: self._focus_row(r))
                self._focus_filters.append(filt)
                row.editor.installEventFilter(filt)
                for child in row.editor.findChildren(QWidget):
                    child.installEventFilter(filt)
                if prev is not None:
                    QWidget.setTabOrder(prev, row.editor)
                prev = row.editor
            top.setExpanded(True)
        if holders:                         # linhas com a mesma altura (a do campo numérico)
            row_h = max(h.sizeHint().height() for h in holders)
            for h in holders:
                h.setMinimumHeight(row_h)
            self.tree.doItemsLayout()
        self._fill_reference()
        self._apply_filter(self.filter_edit.text())
        if cur_key and cur_key in self.rows:
            self.tree.setCurrentItem(self.rows[cur_key].item)
        self._update_buttons()

    def _focus_row(self, row: _Row) -> None:
        if self._current_row is not row:
            self._current_row = row
            self.tree.blockSignals(True)
            self.tree.setCurrentItem(row.item)
            self.tree.blockSignals(False)
        self._show_help(row)

    def _on_current_item(self, cur: QTreeWidgetItem | None, _prev: QTreeWidgetItem | None) -> None:
        if cur is None:
            return
        key = cur.data(COL_NAME, _KEY_ROLE)
        if key is None:
            cat = next((c for c, it in self.cat_items.items() if it is cur), "")
            self.help.title.setText(cat)
            self.help.body.setText(html.escape(_CATEGORY_HELP.get(cat, "")))
            self.help.meta.setText("")
            return
        row = self.rows.get(str(key))
        if row is not None:
            self._current_row = row
            self._show_help(row)

    def _show_help(self, row: _Row) -> None:
        d = row.defn
        p = theme.palette()
        unit = f" ({d.unit})" if d.unit and d.unit != "máscara" else ""
        self.help.title.setText(f"${row.key} — {d.name}{unit}")
        parts = [f"<p style='margin:0 0 6px 0'>{html.escape(d.help)}</p>"]
        if d.typical:
            parts.append(f"<p style='margin:0 0 6px 0'><b>Valor típico:</b> {html.escape(d.typical)}</p>")
        if d.risk:
            parts.append(f"<p style='margin:0 0 6px 0; color:{p.warning}'><b>⚠ Risco:</b> "
                         f"{html.escape(d.risk)}</p>")
        if d.restart:
            parts.append(f"<p style='margin:0 0 6px 0; color:{p.warning}'>↻ Só passa a valer depois de "
                         f"reiniciar a placa.</p>")
        if row.error:
            parts.append(f"<p style='margin:0; color:{p.danger}; font-weight:600'>✖ {html.escape(row.error)}</p>")
        self.help.body.setText("".join(parts))
        meta = [f"Na máquina: {row.display(row.machine)}"]
        if row.is_modified():
            meta.append(f"Novo: {row.display(row.text()) if row.kind != 'password' else '(nova senha)'}")
        ref = fw.DLC32_PROFILE.get(row.key)
        if ref is not None:
            meta.append(f"Referência DLC32: {ref.value} — {ref.note}")
        rng = []
        if d.minimum is not None and d.kind in ("int", "float"):
            rng.append(f"mín. {fw._fmt_num(d.minimum, d.kind)}")
        if d.maximum is not None and d.kind in ("int", "float"):
            rng.append(f"máx. {fw._fmt_num(d.maximum, d.kind)}")
        if rng:
            meta.append("Faixa: " + ", ".join(rng))
        self.help.meta.setText("  ·  ".join(meta))
        self.help.setAccessibleDescription(d.full_help())

    def _row_changed(self, row: _Row) -> None:
        if self._current_row is row:
            self._show_help(row)
        self._update_buttons()

    def _apply_filter(self, text: str) -> None:
        q = (text or "").strip().lower().lstrip("$")
        for cat, top in self.cat_items.items():
            visible = 0
            for i in range(top.childCount()):
                it = top.child(i)
                row = self.rows.get(str(it.data(COL_NAME, _KEY_ROLE)))
                hay = ""
                if row is not None:
                    d = row.defn
                    hay = " ".join((row.key, d.name, d.help, d.alias, cat)).lower()
                show = not q or q in hay
                it.setHidden(not show)
                visible += show
            top.setHidden(visible == 0)

    # ================================================================== referência
    def set_compare(self, on: bool) -> None:
        self.compare_btn.setChecked(on)
        self.tree.setColumnHidden(COL_REF, not on)
        self.use_ref_btn.setVisible(on)
        self._fill_reference()

    def _fill_reference(self) -> None:
        p = theme.palette()
        for key, row in self.rows.items():
            ref = fw.DLC32_PROFILE.get(key)
            if ref is None:
                row.item.setText(COL_REF, "")
                continue
            same = row.machine is not None and fw.values_equal(row.defn, ref.value, row.machine)
            mark = "=" if same else ("≈" if ref.machine_specific else "≠")
            row.item.setText(COL_REF, f"{mark} {row.display(ref.value)}")
            row.item.setToolTip(COL_REF, ref.note + ("" if not ref.machine_specific else
                                                     " (depende da mecânica: diferença é normal)"))
            row.item.setForeground(COL_REF, QColor(p.success if same else p.text_muted if ref.machine_specific
                                                   else p.warning))

    def apply_reference(self) -> int:
        """Copia valores do perfil de referência para os editores (não grava). Retorna quantos."""
        n = 0
        for key, row in self.rows.items():
            ref = fw.DLC32_PROFILE.get(key)
            if ref is None or ref.machine_specific or row.kind == "password":
                continue
            if row.machine is not None and fw.values_equal(row.defn, ref.value, row.machine):
                continue
            row.set_text(ref.value)
            n += 1
        self._update_buttons()
        self.summary.setText(self.summary.text() + f" · {n} valor(es) de referência copiados (revise antes de gravar)")
        return n

    # ================================================================== edição
    def changes(self) -> list[tuple[str, str]]:
        """Alterações pendentes ``[(chave, valor normalizado)]`` (levanta ``ValueError`` se inválida)."""
        out = []
        for key, row in self.rows.items():
            if row.is_modified():
                out.append((key, row.validated()))
        return out

    def set_new_value(self, key: str, value: str) -> bool:
        """Define o 'Novo valor' de uma configuração (usado por testes/restauração)."""
        row = self.rows.get(str(key).lstrip("$"))
        if row is None:
            return False
        row.set_text(str(value))
        self._update_buttons()
        return True

    def discard_changes(self) -> None:
        for row in self.rows.values():
            row.set_text(row.machine if row.machine is not None else "")
        self._update_buttons()

    def _confirm_changes(self, changes: list[tuple[str, str]]) -> bool:
        if not self.confirm_writes:
            return True
        p = theme.palette()
        items = []
        risky = False
        net = False
        for key, val in changes:
            row = self.rows[key]
            d = row.defn
            old = row.display(row.machine)
            new = "(nova senha)" if row.kind == "password" else row.display(val)
            unit = f" {d.unit}" if d.unit and d.unit != "máscara" else ""
            li = (f"<li><b>${html.escape(key)}</b> {html.escape(d.name)}: {html.escape(old)} → "
                  f"<b>{html.escape(new)}</b>{html.escape(unit)}")
            if d.risk:
                risky = True
                li += f"<br><span style='color:{p.warning}'>⚠ {html.escape(d.risk)}</span>"
            if d.category == "Rede / WiFi":
                net = True
            items.append(li + "</li>")
        body = f"<p>Gravar {len(changes)} configuração(ões) na memória da placa?</p><ul>{''.join(items)}</ul>"
        if net:
            kind = getattr(self._ctrl(), "connection_kind", "")
            body += (f"<p style='color:{p.warning}'><b>Rede/WiFi:</b> as mudanças só valem após reiniciar a "
                     f"placa e podem tirá-la da rede"
                     + (" — <b>você está conectado pelo WiFi e pode perder esta conexão</b>" if kind == "wifi" else "")
                     + ". Tenha o cabo USB à mão.</p>")
        body += "<p>Dica: faça um <b>Backup…</b> antes, para poder voltar atrás.</p>"
        dlg = QDialog(self)
        dlg.setWindowTitle("Gravar configurações")
        lay = QVBoxLayout(dlg)
        tb = QTextBrowser()
        tb.setHtml(body)
        tb.setMinimumSize(520, 260)
        tb.setAccessibleName("Lista de alterações a gravar")
        lay.addWidget(tb)
        bb = QDialogButtonBox(QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel)
        ok = bb.button(QDialogButtonBox.StandardButton.Ok)
        ok.setText(f"Gravar {len(changes)}")
        ok.setProperty("danger" if (risky or net) else "primary", True)
        bb.button(QDialogButtonBox.StandardButton.Cancel).setText("Cancelar")
        bb.button(QDialogButtonBox.StandardButton.Cancel).setDefault(True)
        bb.accepted.connect(dlg.accept)
        bb.rejected.connect(dlg.reject)
        lay.addWidget(bb)
        return dlg.exec() == QDialog.DialogCode.Accepted

    def write_changes(self) -> None:
        """Grava só as configurações alteradas (com confirmação)."""
        ctrl = self._ctrl()
        if ctrl is None or not self.connected or self.busy:
            return
        if self.streaming or self.bridge_streaming():
            QMessageBox.warning(self, "Gravar configurações",
                                "Há um programa sendo enviado. Aguarde terminar ou pare o envio antes de "
                                "gravar configurações.")
            return
        try:
            changes = self.changes()
        except ValueError as exc:
            QMessageBox.warning(self, "Gravar configurações", f"Há valores inválidos:\n{exc}")
            return
        if not changes:
            QMessageBox.information(self, "Gravar configurações", "Nenhum valor foi alterado.")
            return
        if not self._confirm_changes(changes):
            return
        self.busy = True
        self._update_buttons()
        supports_ext = self.fw.supports_extended

        def work() -> dict:
            written: list[str] = []
            failed: tuple[str, str] | None = None
            for key, val in changes:
                try:
                    if key.isdigit():
                        ctrl.write_setting(int(key), float(val))
                    else:
                        ctrl.write_extended_setting(key, val)
                    written.append(key)
                except Exception as exc:  # noqa: BLE001
                    code = getattr(exc, "code", None)
                    msg = str(exc)
                    if isinstance(code, int) and code >= 39:
                        msg += f" — {fw.esp32_error_text(code)}"
                    failed = (key, msg)
                    break
            out: dict[str, Any] = {"written": written, "failed": failed}
            try:
                out["settings"] = ctrl.read_settings()
                if supports_ext:
                    out["extended"] = ctrl.read_extended_settings()
            except Exception as exc:  # noqa: BLE001
                out["reread_error"] = str(exc)
            return out

        self.bridge.run("cfg:write", work)

    def bridge_streaming(self) -> bool:
        fn = getattr(self.bridge, "is_streaming", None)
        return bool(fn()) if callable(fn) else False

    def _after_write(self, res: dict) -> None:
        written: list[str] = res.get("written", [])
        restart_needed = [k for k in written if self.rows.get(k) and self.rows[k].defn.restart]
        self._apply_refresh({k: v for k, v in res.items() if k in ("settings", "extended")})
        self.settingsWritten.emit(len(written))
        if res.get("failed"):
            key, msg = res["failed"]
            QMessageBox.warning(self, "Gravar configurações",
                                f"{len(written)} configuração(ões) gravada(s). A gravação parou em ${key}:\n"
                                f"{msg}\n\nAs demais não foram enviadas.")
        elif restart_needed and self.fw.restart_command:
            box = QMessageBox(QMessageBox.Icon.Question, "Reiniciar placa",
                              "As mudanças de rede só valem depois de reiniciar a placa. Reiniciar agora?"
                              + ("\n\nVocê está conectado pelo WiFi: a conexão vai cair."
                                 if getattr(self._ctrl(), "connection_kind", "") == "wifi" else ""),
                              QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No, self)
            box.button(QMessageBox.StandardButton.Yes).setText("Reiniciar agora")
            box.button(QMessageBox.StandardButton.No).setText("Depois")
            if self.confirm_writes and box.exec() == QMessageBox.StandardButton.Yes:
                self._do_restart()

    # ================================================================== backup
    def backup_data(self) -> dict:
        return fw.backup_payload(self.numbered, self.extended, self.fw,
                                 created=datetime.now().isoformat(timespec="seconds"))

    def save_backup(self, path: str) -> None:
        with open(path, "w", encoding="utf-8") as fh:
            json.dump(self.backup_data(), fh, ensure_ascii=False, indent=2)

    def load_backup(self, path: str) -> tuple[int, list[str]]:
        """Carrega um backup nos editores. Retorna ``(aplicados, chaves ausentes na máquina)``."""
        with open(path, encoding="utf-8") as fh:
            data = json.load(fh)
        num, ext = fw.read_backup(data)
        applied, missing = 0, []
        for key, val in list(num.items()) + list(ext.items()):
            row = self.rows.get(key)
            if row is None:
                missing.append(key)
                continue
            if row.kind == "password":
                continue
            row.set_text(val)
            applied += 1
        self._update_buttons()
        return applied, missing

    def backup_dialog(self) -> None:
        stamp = datetime.now().strftime("%Y%m%d-%H%M")
        name = f"config-{self.fw.kind}-{stamp}.json"
        path, _ = QFileDialog.getSaveFileName(self, "Salvar backup das configurações", name,
                                              "Backup de configurações (*.json)")
        if not path:
            return
        try:
            self.save_backup(path)
        except OSError as exc:
            QMessageBox.warning(self, "Backup", f"Não foi possível salvar:\n{exc}")
            return
        QMessageBox.information(self, "Backup", f"Backup salvo em:\n{path}\n\nSenhas de WiFi não são "
                                                "salvas (o firmware não as revela).")

    def restore_dialog(self) -> None:
        path, _ = QFileDialog.getOpenFileName(self, "Restaurar backup de configurações", "",
                                              "Backup de configurações (*.json)")
        if not path:
            return
        try:
            n, missing = self.load_backup(path)
        except (OSError, ValueError) as exc:
            QMessageBox.warning(self, "Restaurar", f"Não foi possível ler o backup:\n{exc}")
            return
        n_mod = sum(1 for r in self.rows.values() if r.is_modified())
        msg = (f"{n} valores carregados; {n_mod} diferem da máquina e estão destacados com ●.\n"
               "Revise e clique em “Gravar alterações” para enviar.")
        if missing:
            msg += f"\n\nIgnorados (não existem neste firmware): {', '.join('$' + m for m in missing[:12])}"
        QMessageBox.information(self, "Restaurar", msg)

    # ================================================================== rede
    def show_network_settings(self) -> None:
        self.tabs.setCurrentIndex(1)
        top = self.cat_items.get("Rede / WiFi")
        if top is not None:
            self.filter_edit.clear()
            self.tree.scrollToItem(top, QAbstractItemView.ScrollHint.PositionAtTop)
            self.tree.setCurrentItem(top)

    def scan_networks(self) -> None:
        ctrl = self._ctrl()
        if ctrl is None or not self.connected or self.streaming:
            return
        self.ap_list.clear()
        self.ap_list.addItem("Procurando…")
        self.ap_list.show()
        self.bridge.run("cfg:scan", ctrl.list_wifi_networks)

    def _show_networks(self, aps: list[dict]) -> None:
        self.ap_list.clear()
        if not aps:
            self.ap_list.addItem("Nenhuma rede encontrada.")
        for ap in aps:
            lock = "🔒 " if ap.get("protected") else ""
            it = QListWidgetItem(f"{lock}{ap.get('ssid')}  —  sinal {ap.get('signal')}%")
            it.setData(Qt.ItemDataRole.AccessibleTextRole,
                       f"{ap.get('ssid')}, sinal {ap.get('signal')} por cento, "
                       f"{'protegida por senha' if ap.get('protected') else 'aberta'}")
            self.ap_list.addItem(it)
        self.ap_list.show()

    def restart_board(self) -> None:
        if not self.connected or not self.fw.restart_command:
            return
        if self.streaming:
            return
        wifi = getattr(self._ctrl(), "connection_kind", "") == "wifi"
        box = QMessageBox(QMessageBox.Icon.Warning, "Reiniciar placa",
                          "Reiniciar o ESP32 agora? Movimentos e o spindle param, a posição de máquina "
                          "é perdida (refaça homing/zero)."
                          + ("\n\nVocê está conectado pelo WiFi: a conexão vai cair." if wifi else ""),
                          QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.Cancel, self)
        box.button(QMessageBox.StandardButton.Yes).setText("Reiniciar")
        if box.exec() == QMessageBox.StandardButton.Yes:
            self._do_restart()

    def _do_restart(self) -> None:
        ctrl = self._ctrl()
        if ctrl is not None and hasattr(ctrl, "restart_board"):
            self.bridge.run("cfg:restart", ctrl.restart_board)

    # ================================================================== tema
    def _restyle(self) -> None:
        for row in self.rows.values():
            row.refresh_state()
        self._fill_reference()
        self._render_pins("".join(k for k, lab in self.st_pins.items()
                                  if lab.accessibleDescription().endswith("acionado")))
        for card in self.cards:
            for lab in card.findChildren(QLabel):
                name = lab.property("iconName")
                if name:
                    lab.setPixmap(pixmap(str(name), 16, theme.palette().text_muted))
        self._update_header()


_CATEGORY_HELP = {
    "Geral e relatórios": "Como o firmware reporta posição e interpreta arcos.",
    "Motores e passos": "Calibração e sinais dos motores de passo. Erros aqui mudam todas as medidas.",
    "Velocidades e acelerações": "Limites de velocidade e aceleração. Valores altos demais fazem os "
                                 "motores perderem passos.",
    "Curso, limites e sonda": "Fins de curso, limites de software e sonda (probe).",
    "Homing": "Ciclo de referência ($H) com chaves de fim de curso.",
    "Spindle / Laser": "Controle do spindle ou do laser (PWM, rotação, modo laser).",
    "Rede / WiFi": "WiFi da placa (Grbl_Esp32/DLC32). Mudanças podem tirar a placa da rede e só valem "
                   "após reiniciar.",
    "Sistema": "Outras opções do firmware.",
}

__all__ = ["MachineConfigPanel", "MaskEditor"]
