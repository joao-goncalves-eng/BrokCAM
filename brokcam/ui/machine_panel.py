"""Aba "Máquina": conexão GRBL, DRO, jog, console, envio de programa e configurações $$.

Com ``split=True`` (usado pela janela principal) cada seção vira um widget próprio
(``jog_panel``, ``program_panel``, ``console_panel``, ``settings_panel``) para ser
colocada num painel (dock) separado — arrastável e encaixável como no Candle. O próprio
``MachinePanel`` fica só com conexão, estado e DRO. A lógica é a mesma nos dois modos.

Os callbacks do ``GrblController`` chegam numa thread de comunicação. O
:class:`GrblBridge` (um ``QObject`` da thread da GUI) os converte em sinais Qt; a
conexão enfileirada garante que todos os widgets sejam atualizados somente na
thread principal.
"""
from __future__ import annotations

import html
import inspect
import logging
import re
import threading
import time
from typing import Any

from PySide6.QtCore import QEvent, QObject, QSize, Qt, QTimer, Signal
from PySide6.QtGui import QColor, QKeyEvent, QTextCursor
from PySide6.QtWidgets import (
    QAbstractItemView, QButtonGroup, QCheckBox, QComboBox, QDialog, QDialogButtonBox, QFrame,
    QGridLayout, QHBoxLayout, QHeaderView, QInputDialog, QLabel, QLineEdit, QMessageBox, QPlainTextEdit,
    QProgressBar, QPushButton, QTableWidget, QTableWidgetItem, QTabWidget,
    QToolButton, QVBoxLayout, QWidget,
)

from brokcam.core.params import ParamGroup, ParamSpec

from . import backend, theme, units
from .icons import icon, pixmap
from .param_form import DoubleSpin, ParamForm

log = logging.getLogger("brokcam.ui.machine")

STATE_PT = {
    "Idle": "Ocioso", "Run": "Executando", "Hold": "Pausado", "Jog": "Jog",
    "Alarm": "Alarme", "Door": "Porta aberta", "Check": "Verificação", "Home": "Homing",
    "Sleep": "Suspenso", "": "Desconectado", "Disconnected": "Desconectado",
}
STATE_TOKEN = {
    "Idle": "success", "Run": "info", "Jog": "info", "Home": "info", "Check": "info",
    "Hold": "warning", "Door": "warning", "Alarm": "danger", "Sleep": "text_muted",
}

PROBE_GROUP = ParamGroup(
    key="probe_z", title="Sonda Z (probe)",
    description=("Mede a altura da superfície da placa descendo a ferramenta até tocar uma "
                 "placa de contato ligada ao pino de probe do GRBL (G38.2). Ao tocar, define "
                 "Z0 de trabalho compensando a espessura da placa de contato."),
    params=(
        ParamSpec("max_depth", "Descida máxima", "float", 10.0,
                  "Distância máxima que a ferramenta desce procurando contato. Se não tocar "
                  "nada até aqui, o GRBL gera o alarme 5 (falha de probe). Use um valor que "
                  "não leve a ferramenta a bater na mesa.", unit="mm", minimum=0.5, maximum=100, decimals=2),
        ParamSpec("feed", "Avanço da sonda", "float", 50.0,
                  "Velocidade de descida durante a procura. Mais lento = medição mais "
                  "precisa e menos risco de quebrar fresas finas (V-bits).",
                  unit="mm/min", minimum=1, maximum=1000, decimals=0),
        ParamSpec("plate_thickness", "Espessura da placa de contato", "float", 0.0,
                  "Espessura da chapa usada como contato. O Z0 é definido somando essa "
                  "espessura, para que Z0 fique na superfície da PCB. Use 0 se tocar a "
                  "própria placa (com clipe no cobre).", unit="mm", minimum=0, maximum=50, decimals=3),
    ),
)


# =========================================================================== ponte

class GrblBridge(QObject):
    """Converte callbacks (thread do controlador) em sinais Qt (thread da GUI)."""

    status = Signal(dict)
    line = Signal(str, str)              # direção ("tx"/"rx"/">"/"<"), texto
    progress = Signal(int, int, int)     # enviados, confirmados, total
    alarm = Signal(int, str)
    error = Signal(int, str, int)        # código, mensagem, nº da linha
    state = Signal(str)
    connection = Signal(bool)
    streamDone = Signal(bool)            # concluído com sucesso?
    message = Signal(str)                # [MSG:...] do GRBL
    commandFailed = Signal(str, str)     # comando, mensagem de erro
    commandResult = Signal(str, object)  # nome do comando, resultado
    streaming = Signal(bool)             # envio de programa iniciado/terminado (MachinePanel)

    #: controlador ligado por :meth:`attach` (usado por outros painéis, ex. MachineConfigPanel)
    ctrl: Any = None

    def is_streaming(self) -> bool:
        """``True`` se há um programa sendo enviado (não grave configurações nesse caso)."""
        c = self.ctrl
        try:
            return bool(c is not None and getattr(c, "is_streaming", False))
        except Exception:  # noqa: BLE001
            return False

    def is_connected(self) -> bool:
        c = self.ctrl
        try:
            return bool(c is not None and getattr(c, "is_connected", False))
        except Exception:  # noqa: BLE001
            return False

    def attach(self, ctrl: Any) -> None:
        self.ctrl = ctrl
        def code(c: Any) -> int:
            try:
                return int(c)
            except (TypeError, ValueError):
                return -1

        ctrl.on_status = lambda st: self.status.emit(backend.status_to_dict(st))
        ctrl.on_line = lambda d, t: self.line.emit(str(d), str(t))
        ctrl.on_progress = lambda s, a, t: self.progress.emit(int(s or 0), int(a or 0), int(t or 0))
        ctrl.on_alarm = lambda c, m="": self.alarm.emit(code(c), str(m or ""))
        ctrl.on_error = lambda c, m="", ln=None: self.error.emit(code(c), str(m or ""), code(ln))
        # on_state_change pode receber (novo) ou (antigo, novo)
        ctrl.on_state_change = lambda *a: self.state.emit(str(a[-1]) if a else "")
        ctrl.on_connection = lambda ok: self.connection.emit(bool(ok))
        ctrl.on_stream_done = lambda ok=True: self.streamDone.emit(bool(ok))
        ctrl.on_message = lambda text: self.message.emit(str(text))

    def run(self, name: str, fn: Any, *args: Any, **kwargs: Any) -> None:
        """Executa um comando bloqueante do controlador numa thread auxiliar.

        Comandos como ``home()`` ou ``probe_z()`` esperam a resposta do GRBL (podem levar
        minutos) e jamais devem rodar na thread da GUI. O resultado/erro volta por sinal.
        """
        def work() -> None:
            try:
                res = fn(*args, **kwargs)
                self.commandResult.emit(name, res)
            except Exception as exc:  # noqa: BLE001 - exibido ao usuário
                self.commandFailed.emit(name, str(exc) or exc.__class__.__name__)

        threading.Thread(target=work, name=f"Grbl-{name}", daemon=True).start()


# =========================================================================== widgets auxiliares

class StateChip(QLabel):
    def __init__(self) -> None:
        super().__init__()
        self.setObjectName("StateChip")
        self.setAccessibleName("Estado da máquina")
        self.state = ""
        self.set_state("")
        theme.on_change(self, lambda _p: self.set_state(self.state))

    def set_state(self, state: str) -> None:
        self.state = state
        base = state.split(":")[0]
        p = theme.palette()
        token = STATE_TOKEN.get(base, "text_muted")
        col = QColor(getattr(p, token))
        bg = QColor(col)
        bg.setAlphaF(0.16)
        text = STATE_PT.get(base, base or "Desconectado")
        sub = state.split(":")[1] if ":" in state else ""
        symbol = {"success": "●", "info": "▶", "warning": "❚❚", "danger": "▲"}.get(token, "○")
        self.setText(f"{symbol}  {text}" + (f" ({sub})" if sub else ""))
        self.setStyleSheet(
            f"#StateChip {{ color: {col.name()}; background: rgba({bg.red()},{bg.green()},{bg.blue()},{bg.alpha()});"
            f" border: 1px solid {col.name()}; }}")
        self.setAccessibleDescription(f"Estado: {text}")


class Banner(QFrame):
    """Faixa de aviso (erro/alarme) com ícone, texto e botão fechar."""

    def __init__(self) -> None:
        super().__init__()
        self.setObjectName("Banner")
        lay = QHBoxLayout(self)
        lay.setContentsMargins(10, 8, 6, 8)
        lay.setSpacing(8)
        self.ic = QLabel()
        self.text = QLabel()
        self.text.setWordWrap(True)
        self.text.setTextFormat(Qt.TextFormat.RichText)
        self.close_btn = QToolButton()
        self.close_btn.setIcon(icon("close"))
        self.close_btn.setAccessibleName("Fechar aviso")
        self.close_btn.clicked.connect(self.hide)
        lay.addWidget(self.ic, 0, Qt.AlignmentFlag.AlignTop)
        lay.addWidget(self.text, 1)
        lay.addWidget(self.close_btn, 0, Qt.AlignmentFlag.AlignTop)
        self.hide()

    def show_message(self, kind: str, title: str, body: str) -> None:
        p = theme.palette()
        col = QColor(getattr(p, kind))
        bg = QColor(col)
        bg.setAlphaF(0.14)
        self.setStyleSheet(f"#Banner {{ background: rgba({bg.red()},{bg.green()},{bg.blue()},{bg.alpha()});"
                           f" border: 1px solid {col.name()}; }}")
        self.ic.setPixmap(pixmap("error" if kind == "danger" else "warning", 18, col.name(), self.devicePixelRatioF()))
        self.text.setText(f"<b style='color:{col.name()}'>{html.escape(title)}</b><br>{html.escape(body)}")
        self.setAccessibleName(title)
        self.setAccessibleDescription(body)
        self.show()


class JogPad(QFrame):
    """Pad de jog focável: setas movem XY, PgUp/PgDn movem Z (somente com foco)."""

    jog = Signal(float, float, float)

    def __init__(self) -> None:
        super().__init__()
        self.setObjectName("JogPad")
        self.setFocusPolicy(Qt.FocusPolicy.StrongFocus)
        self.setAccessibleName("Controle de movimento manual (jog)")
        self.setAccessibleDescription(
            "Com este painel em foco: setas movem X/Y, Page Up/Page Down movem Z, teclas 1 a 4 "
            "escolhem o passo.")
        self.setToolTip("Clique aqui (ou use Tab) e use as setas do teclado para mover X/Y e "
                        "PgUp/PgDn para Z.")
        g = QGridLayout(self)
        g.setContentsMargins(10, 10, 10, 10)
        g.setSpacing(6)

        def b(name: str, tip: str, dx: float, dy: float, dz: float) -> QPushButton:
            btn = QPushButton()
            btn.setIcon(icon(name))
            btn.setIconSize(QSize(18, 18))
            btn.setProperty("jog", True)
            btn.setToolTip(tip)
            btn.setAccessibleName(tip)
            btn.setAutoRepeat(False)
            btn.clicked.connect(lambda: self.jog.emit(dx, dy, dz))
            return btn

        self.btn_yp = b("arrow-up", "Mover Y+ (seta para cima)", 0, 1, 0)
        self.btn_yn = b("arrow-down", "Mover Y− (seta para baixo)", 0, -1, 0)
        self.btn_xn = b("arrow-left", "Mover X− (seta para a esquerda)", -1, 0, 0)
        self.btn_xp = b("arrow-right", "Mover X+ (seta para a direita)", 1, 0, 0)
        self.btn_zp = b("z-up", "Subir Z+ (Page Up)", 0, 0, 1)
        self.btn_zn = b("z-down", "Descer Z− (Page Down)", 0, 0, -1)
        g.addWidget(self.btn_yp, 0, 1)
        g.addWidget(self.btn_xn, 1, 0)
        center = QLabel("XY")
        center.setAlignment(Qt.AlignmentFlag.AlignCenter)
        center.setObjectName("Muted")
        g.addWidget(center, 1, 1)
        g.addWidget(self.btn_xp, 1, 2)
        g.addWidget(self.btn_yn, 2, 1)
        sep = QFrame()
        sep.setFrameShape(QFrame.Shape.VLine)
        g.addWidget(sep, 0, 3, 3, 1)
        g.addWidget(self.btn_zp, 0, 4)
        zl = QLabel("Z")
        zl.setAlignment(Qt.AlignmentFlag.AlignCenter)
        zl.setObjectName("Muted")
        g.addWidget(zl, 1, 4)
        g.addWidget(self.btn_zn, 2, 4)
        for btn in (self.btn_yp, self.btn_yn, self.btn_xn, self.btn_xp, self.btn_zp, self.btn_zn):
            btn.setFocusPolicy(Qt.FocusPolicy.NoFocus)  # o pad recebe o foco, não cada seta

    def keyPressEvent(self, ev: QKeyEvent) -> None:  # noqa: N802
        if ev.isAutoRepeat():
            ev.accept()
            return
        m = {Qt.Key.Key_Up: (0, 1, 0), Qt.Key.Key_Down: (0, -1, 0), Qt.Key.Key_Left: (-1, 0, 0),
             Qt.Key.Key_Right: (1, 0, 0), Qt.Key.Key_PageUp: (0, 0, 1), Qt.Key.Key_PageDown: (0, 0, -1)}
        if ev.key() in m:
            self.jog.emit(*m[ev.key()])
            ev.accept()
            return
        super().keyPressEvent(ev)


class HistoryLineEdit(QLineEdit):
    def __init__(self) -> None:
        super().__init__()
        self.history: list[str] = []
        self.pos = 0

    def push(self, text: str) -> None:
        if text and (not self.history or self.history[-1] != text):
            self.history.append(text)
        self.pos = len(self.history)

    def keyPressEvent(self, ev: QKeyEvent) -> None:  # noqa: N802
        if ev.key() == Qt.Key.Key_Up and self.history:
            self.pos = max(0, self.pos - 1)
            self.setText(self.history[self.pos])
            return
        if ev.key() == Qt.Key.Key_Down and self.history:
            self.pos = min(len(self.history), self.pos + 1)
            self.setText(self.history[self.pos] if self.pos < len(self.history) else "")
            return
        super().keyPressEvent(ev)


# =========================================================================== painel

class MachinePanel(QWidget):
    programLine = Signal(int)            # linha (0-based) do editor confirmada pela máquina
    toolPosition = Signal(object)        # (x, y, z) de trabalho, ou None
    stateChanged = Signal(str)
    connectedChanged = Signal(bool)
    streamingChanged = Signal(bool)
    programLoaded = Signal()             # programa carregado (a janela mostra o painel Programa)

    def __init__(self, parent: QWidget | None = None, *, controller: Any | None = None,
                 confirm_send: bool = True, split: bool = False) -> None:
        super().__init__(parent)
        self.setObjectName("Panel")
        self.setAccessibleName("Painel da máquina GRBL")
        self.codes = backend.grbl_codes()
        self.ctrl = controller if controller is not None else backend.create_controller()
        self.bridge = GrblBridge(self)
        if self.ctrl is not None:
            self.bridge.attach(self.ctrl)
        self.connected = False
        self.streaming = False
        self.paused = False
        self.state = ""
        self.confirm_send = confirm_send
        self.program_name = ""
        self.program_lines: list[str] = []
        self.n_commands = 0
        self.stream_start = 0.0
        self.paused_total = 0.0
        self.pause_start = 0.0
        self.estimate_s = 0.0
        self.last_wpos: tuple[float, float, float] | None = None
        self._settings_values: dict[int, str] = {}
        self._last_progress = (0, 0, 0)
        self._resume_line = 0               # linha onde o último envio parou (para retomar)
        self.split = split

        if split:
            root_v = QVBoxLayout(self)
            root_v.setContentsMargins(10, 8, 10, 8)
            root_v.addLayout(self._build_left())
            self.jog_panel = self._build_middle()
            self.jog_panel.setContentsMargins(10, 8, 10, 8)
            self.jog_panel.setAccessibleName("Controle e jog")
            self.program_panel = self._build_program()
            self.program_panel.setContentsMargins(10, 0, 10, 8)
            self.console_panel = self._build_console()
            self.console_panel.setContentsMargins(10, 0, 10, 8)
            self.settings_panel = self._build_settings()
            self.settings_panel.setContentsMargins(10, 0, 10, 8)
            self.tabs: QTabWidget | None = None
        else:
            root = QHBoxLayout(self)
            root.setContentsMargins(10, 8, 10, 8)
            root.setSpacing(12)
            root.addLayout(self._build_left(), 0)
            root.addWidget(self._build_middle(), 0)
            self.tabs = self._build_tabs()
            root.addWidget(self.tabs, 1)

        self.bridge.status.connect(self._on_status)
        self.bridge.line.connect(self._on_line)
        self.bridge.progress.connect(self._on_progress)
        self.bridge.alarm.connect(self._on_alarm)
        self.bridge.error.connect(self._on_error)
        self.bridge.state.connect(self._on_state)
        self.bridge.connection.connect(self._on_connection)
        self.bridge.streamDone.connect(lambda ok: self._finish_stream(aborted=not ok) if self.streaming else None)
        self.bridge.message.connect(self._on_grbl_message)
        self.bridge.commandFailed.connect(self._on_command_failed)
        self.bridge.commandResult.connect(self._on_command_result)
        self.streamingChanged.connect(self.bridge.streaming)

        self.clock = QTimer(self)
        self.clock.setInterval(500)
        self.clock.timeout.connect(self._update_times)
        self.refresh_ports()
        self._update_enabled()
        if self.ctrl is None:
            self.banner.show_message(
                "warning", "Controlador GRBL indisponível",
                "O módulo de comunicação com a máquina (brokcam.machine) ainda não está "
                "instalado. A interface está pronta e será ativada automaticamente quando ele existir.")

    # ------------------------------------------------------------------ construção
    def _build_left(self) -> QVBoxLayout:
        col = QVBoxLayout()
        col.setSpacing(8)
        conn = QHBoxLayout()
        conn.setSpacing(6)
        self.port = QComboBox()
        self.port.setEditable(True)
        self.port.setMinimumWidth(130)
        self.port.setAccessibleName("Porta serial")
        self.port.setToolTip("Porta serial da placa GRBL. 'SIMULADOR' usa uma máquina simulada para testes "
                             "('SIMULADOR-DLC32' imita uma MKS DLC32). Para conectar pelo WiFi "
                             "(MKS DLC32/Grbl_Esp32) digite tcp://IP:23, ex.: tcp://192.168.4.1:23.")
        self.refresh_btn = QToolButton()
        self.refresh_btn.setIcon(icon("refresh"))
        self.refresh_btn.setToolTip("Atualizar a lista de portas")
        self.refresh_btn.setAccessibleName("Atualizar portas")
        self.refresh_btn.clicked.connect(self.refresh_ports)
        self.baud = QComboBox()
        self.baud.setAccessibleName("Velocidade (baud)")
        self.baud.setToolTip("Velocidade da comunicação serial. GRBL 1.1 usa 115200 por padrão.")
        for b in (115200, 250000, 230400, 57600, 38400, 19200, 9600):
            self.baud.addItem(str(b), b)
        self.connect_btn = QPushButton("&Conectar")
        self.connect_btn.setIcon(icon("plug"))
        self.connect_btn.setProperty("primary", True)
        self.connect_btn.setAccessibleName("Conectar ou desconectar da máquina")
        self.connect_btn.clicked.connect(self.toggle_connection)
        conn.addWidget(self.port, 1)
        conn.addWidget(self.refresh_btn)
        conn.addWidget(self.baud)
        col.addLayout(conn)
        col.addWidget(self.connect_btn)

        st = QHBoxLayout()
        self.chip = StateChip()
        st.addWidget(self.chip)
        st.addStretch(1)
        col.addLayout(st)
        self.state_help = QLabel("Conecte-se à máquina para começar.")
        self.state_help.setObjectName("Muted")
        self.state_help.setWordWrap(True)
        self.state_help.setMaximumWidth(330)
        col.addWidget(self.state_help)
        self.banner = Banner()
        self.banner.setMaximumWidth(340)
        col.addWidget(self.banner)

        dro = QFrame()
        dro.setObjectName("Card")
        dro.setAccessibleName("Leitura de posição (DRO)")
        g = QGridLayout(dro)
        g.setContentsMargins(12, 8, 8, 8)
        g.setHorizontalSpacing(10)
        g.setVerticalSpacing(2)
        hdr_w = QLabel("Trabalho")
        hdr_w.setObjectName("Muted")
        hdr_m = QLabel("Máquina")
        hdr_m.setObjectName("Muted")
        g.addWidget(hdr_w, 0, 1, Qt.AlignmentFlag.AlignRight)
        g.addWidget(hdr_m, 0, 2, Qt.AlignmentFlag.AlignRight)
        self.dro_w: list[QLabel] = []
        self.dro_m: list[QLabel] = []
        self.zero_axis_btns: list[QToolButton] = []
        mono = theme.mono_font_family()
        for i, ax in enumerate("XYZ"):
            a = QLabel(ax)
            a.setObjectName("DroAxis")
            w = QLabel("—")
            w.setObjectName("DroValue")
            w.setAlignment(Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter)
            w.setMinimumWidth(110)
            w.setStyleSheet(f"font-family: '{mono}';")
            w.setAccessibleName(f"Posição de trabalho {ax}")
            m = QLabel("—")
            m.setObjectName("DroSecondary")
            m.setAlignment(Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter)
            m.setMinimumWidth(78)
            m.setStyleSheet(f"font-family: '{mono}';")
            m.setAccessibleName(f"Posição de máquina {ax}")
            z = QToolButton()
            z.setText(f"{ax}0")
            z.setToolTip(f"Definir a posição atual como zero de trabalho em {ax} (G10 L20 {ax}0)")
            z.setAccessibleName(f"Zerar eixo {ax}")
            z.clicked.connect(lambda _=False, axis=ax: self.set_zero(axis))
            g.addWidget(a, i + 1, 0)
            g.addWidget(w, i + 1, 1)
            g.addWidget(m, i + 1, 2)
            g.addWidget(z, i + 1, 3)
            self.dro_w.append(w)
            self.dro_m.append(m)
            self.zero_axis_btns.append(z)
        col.addWidget(dro)
        self.fs_label = QLabel("Avanço — · Spindle —")
        self.fs_label.setObjectName("Muted")
        col.addWidget(self.fs_label)
        col.addStretch(1)
        return col

    def _build_middle(self) -> QWidget:
        host = QWidget()
        col = QVBoxLayout(host)
        col.setContentsMargins(0, 0, 0, 0)
        col.setSpacing(8)
        acts = QGridLayout()
        acts.setSpacing(6)

        def act(name: str, text: str, tip: str, slot: Any) -> QPushButton:
            b = QPushButton(text)
            b.setIcon(icon(name))
            b.setToolTip(tip)
            b.setAccessibleName(text.replace("&", ""))
            b.setAccessibleDescription(tip)
            b.clicked.connect(slot)
            return b

        self.home_btn = act("home", "&Home", "Executar o ciclo de homing ($H). Requer chaves de fim de curso.", self.home)
        self.unlock_btn = act("unlock", "&Destravar", "Destravar após alarme ($X). Verifique a posição antes de mover!", self.unlock)
        self.reset_btn = act("reset", "&Reset (emergência)",
                             "Reset por software (Ctrl-X): corta tudo NA HORA, como o botão de emergência do "
                             "Candle. Se a máquina estava em movimento o GRBL perde a posição (ALARM:3): refaça "
                             "o zero. Para interromper um programa sem perder a posição use Parar.", self.reset)
        self.reset_btn.setProperty("danger", True)
        self.zero_xy_btn = act("zero", "Zero XY", "Definir X e Y atuais como zero de trabalho.", lambda: self.set_zero("XY"))
        self.zero_xyz_btn = act("zero", "Zero XYZ", "Definir X, Y e Z atuais como zero de trabalho.", lambda: self.set_zero("XYZ"))
        self.goto_btn = act("goto-zero", "Ir para zero", "Mover rapidamente para X0 Y0 de trabalho (Z sobe primeiro).", self.goto_zero)
        self.probe_btn = act("probe", "Sonda Z…", "Medir a superfície com a sonda (G38.2) e definir Z0.", self.probe)
        acts.addWidget(self.home_btn, 0, 0)
        acts.addWidget(self.unlock_btn, 0, 1)
        acts.addWidget(self.zero_xy_btn, 1, 0)
        acts.addWidget(self.zero_xyz_btn, 1, 1)
        acts.addWidget(self.goto_btn, 2, 0)
        acts.addWidget(self.probe_btn, 2, 1)
        acts.addWidget(self.reset_btn, 3, 0, 1, 2)
        col.addLayout(acts)

        self.jogpad = JogPad()
        self.jogpad.jog.connect(self._jog)
        col.addWidget(self.jogpad)
        steps = QHBoxLayout()
        steps.setSpacing(4)
        lab = QLabel("Passo")
        lab.setObjectName("Muted")
        steps.addWidget(lab)
        self.step_group = QButtonGroup(self)
        for i, s in enumerate((0.01, 0.1, 1.0, 10.0)):
            b = QPushButton(f"{s:g}".replace(".", ","))
            b.setCheckable(True)
            b.setProperty("jog", True)
            b.setMinimumWidth(40)
            b.setToolTip(f"Passo de jog de {s:g} mm (tecla {i + 1} com o pad em foco)")
            b.setAccessibleName(f"Passo {s:g} milímetros")
            self.step_group.addButton(b, i)
            steps.addWidget(b)
            if s == 1.0:
                b.setChecked(True)
        self.step_values = (0.01, 0.1, 1.0, 10.0)
        col.addLayout(steps)
        feed = QHBoxLayout()
        fl = QLabel("Avanço do jog")
        fl.setObjectName("Muted")
        self.jog_feed = DoubleSpin()
        self.jog_feed.setRange(1, 10000)
        self.jog_feed.setDecimals(0)
        self.jog_feed.setValue(600)
        self.jog_feed.setSingleStep(50)
        self.jog_feed.setSuffix(" mm/min")
        self.jog_feed.setAccessibleName("Avanço do jog em milímetros por minuto")
        fl.setBuddy(self.jog_feed)
        feed.addWidget(fl)
        feed.addWidget(self.jog_feed, 1)
        col.addLayout(feed)
        col.addStretch(1)
        self.jogpad.installEventFilter(self)
        return host

    def eventFilter(self, obj: QObject, ev: QEvent) -> bool:  # noqa: N802
        if obj is self.jogpad and ev.type() == QEvent.Type.KeyPress:
            k = ev.key()  # type: ignore[attr-defined]
            if Qt.Key.Key_1 <= k <= Qt.Key.Key_4:
                self.step_group.button(k - Qt.Key.Key_1).setChecked(True)
                return True
        return super().eventFilter(obj, ev)

    def _build_tabs(self) -> QTabWidget:
        tabs = QTabWidget()
        tabs.setDocumentMode(True)
        tabs.addTab(self._build_program(), icon("play"), "Programa")
        tabs.addTab(self._build_console(), icon("terminal"), "Console")
        tabs.addTab(self._build_settings(), icon("settings"), "Ajustes $$")
        return tabs

    def _build_console(self) -> QWidget:
        w = QWidget()
        lay = QVBoxLayout(w)
        lay.setContentsMargins(0, 8, 0, 0)
        lay.setSpacing(6)
        self.console = QPlainTextEdit()
        self.console.setReadOnly(True)
        self.console.setMaximumBlockCount(3000)
        self.console.setFont(theme.mono_font(theme.manager().font_scale, -0.5))
        self.console.setAccessibleName("Saída do console GRBL")
        lay.addWidget(self.console, 1)
        row = QHBoxLayout()
        self.cmd = HistoryLineEdit()
        self.cmd.setPlaceholderText("Comando G-code ou $ (↑/↓ para histórico)…")
        self.cmd.setAccessibleName("Comando para enviar ao GRBL")
        self.cmd.returnPressed.connect(self._send_cmd)
        send = QPushButton("Enviar")
        send.setIcon(icon("send"))
        send.setAccessibleName("Enviar comando")
        send.clicked.connect(self._send_cmd)
        self.show_ok = QCheckBox("Mostrar 'ok'")
        self.show_ok.setToolTip("Exibe as confirmações 'ok' do GRBL no console")
        row.addWidget(self.cmd, 1)
        row.addWidget(send)
        row.addWidget(self.show_ok)
        lay.addLayout(row)
        self.send_cmd_btn = send
        return w

    def _build_program(self) -> QWidget:
        w = QWidget()
        lay = QVBoxLayout(w)
        lay.setContentsMargins(0, 8, 0, 0)
        lay.setSpacing(8)
        top = QHBoxLayout()
        self.prog_label = QLabel("Nenhum programa carregado. Gere o G-code e use “Enviar à máquina”.")
        self.prog_label.setWordWrap(True)
        top.addWidget(self.prog_label, 1)
        lay.addLayout(top)
        self.prog_bar = QProgressBar()
        self.prog_bar.setRange(0, 1000)
        self.prog_bar.setValue(0)
        self.prog_bar.setFormat("%p%")
        self.prog_bar.setAccessibleName("Progresso do envio do programa")
        lay.addWidget(self.prog_bar)
        times = QHBoxLayout()
        self.elapsed = QLabel("Decorrido —")
        self.remaining = QLabel("Restante —")
        self.lines_label = QLabel("Linha —")
        for lab in (self.elapsed, self.remaining, self.lines_label):
            lab.setObjectName("Muted")
            times.addWidget(lab)
            times.addStretch(1)
        lay.addLayout(times)
        btns = QHBoxLayout()
        btns.setSpacing(6)
        self.start_btn = QPushButton("&Iniciar")
        self.start_btn.setIcon(icon("play"))
        self.start_btn.setProperty("primary", True)
        self.start_btn.setToolTip("Iniciar o envio do programa para a máquina")
        self.start_btn.clicked.connect(lambda: self.start_program())
        self.pause_btn = QPushButton("&Pausar")
        self.pause_btn.setIcon(icon("pause"))
        self.pause_btn.setToolTip("Pausa suave (feed hold '!'). Retome com o mesmo botão.")
        self.pause_btn.clicked.connect(self.toggle_pause)
        self.stop_btn = QPushButton("Parar")
        self.stop_btn.setIcon(icon("stop"))
        self.stop_btn.setProperty("danger", True)
        self.stop_btn.setToolTip("Interromper o programa com segurança: para de enviar, segura o movimento "
                                 "(feed hold), espera a máquina parar e só então faz o reset. A posição é "
                                 "mantida e não há ALARM:3 — dá para retomar da linha depois.")
        self.stop_btn.clicked.connect(self.stop_program)
        self.resume_btn = QPushButton("Iniciar da &linha…")
        self.resume_btn.setIcon(icon("redo"))
        self.resume_btn.setToolTip("Começar (ou retomar) a partir de uma linha do programa. O BrokCAM lê as "
                                   "linhas anteriores e restaura o estado (G90/G91, unidades, G54…, avanço, "
                                   "spindle): sobe ao Z seguro, liga o spindle, vai ao ponto XY e desce devagar.")
        self.resume_btn.clicked.connect(self.start_from_line)
        for b in (self.start_btn, self.pause_btn, self.stop_btn, self.resume_btn):
            b.setAccessibleName(b.text().replace("&", ""))
            btns.addWidget(b)
        btns.addStretch(1)
        lay.addLayout(btns)
        opts = QHBoxLayout()
        self.m6_pause = QCheckBox("Pausar em M6 (troca de ferramenta)")
        self.m6_pause.setChecked(True)
        self.m6_pause.setToolTip("O GRBL 1.1 não reconhece M6 (error:20). Com esta opção o BrokCAM troca o M6 por: "
                                 "desligar spindle (M5) + pausa (M0). Troque a ferramenta, refaça o Z se preciso e "
                                 "clique Retomar: o spindle volta a girar antes de continuar.")
        self.check_travel = QCheckBox("Conferir curso da máquina")
        self.check_travel.setChecked(True)
        self.check_travel.setToolTip("Antes de iniciar, confere se o programa somado ao zero de trabalho atual cabe no "
                                     "curso da máquina ($130–$132). Só funciona com homing ligado ($22=1) e com as "
                                     "configurações $$ já lidas.")
        for c in (self.m6_pause, self.check_travel):
            c.setAccessibleName(c.text())
            opts.addWidget(c)
        opts.addStretch(1)
        lay.addLayout(opts)

        ovr = QGridLayout()
        ovr.setHorizontalSpacing(4)
        ovr.setVerticalSpacing(6)
        self.ovr_labels: dict[str, QLabel] = {}
        for r, (kind, title, ic) in enumerate((("feed", "Avanço", "feed"), ("spindle", "Spindle", "spindle"))):
            lab = QLabel(title)
            lab.setObjectName("Muted")
            ovr.addWidget(lab, r, 0)
            for c, (txt, act) in enumerate((("−10", "-10"), ("−1", "-1"), ("100%", "reset"), ("+1", "+1"), ("+10", "+10"))):
                b = QToolButton()
                b.setText(txt)
                b.setToolTip(f"Override de {title.lower()}: {txt}")
                b.setAccessibleName(f"{title} override {txt}")
                b.clicked.connect(lambda _=False, k=kind, a=act: backend.override(self.ctrl, k, a))
                ovr.addWidget(b, r, c + 1)
            val = QLabel("100%")
            val.setMinimumWidth(48)
            val.setAccessibleName(f"Override atual de {title.lower()}")
            ovr.addWidget(val, r, 7)
            self.ovr_labels[kind] = val
        lab = QLabel("Rápido")
        lab.setObjectName("Muted")
        ovr.addWidget(lab, 2, 0)
        for c, pct in enumerate(("25", "50", "100")):
            b = QToolButton()
            b.setText(f"{pct}%")
            b.setToolTip(f"Limitar movimentos rápidos a {pct}%")
            b.setAccessibleName(f"Override de rápido {pct} por cento")
            b.clicked.connect(lambda _=False, p=pct: backend.override(self.ctrl, "rapid", p))
            ovr.addWidget(b, 2, c + 1)
        self.ovr_labels["rapid"] = QLabel("100%")
        ovr.addWidget(self.ovr_labels["rapid"], 2, 7)
        ovr.setColumnStretch(8, 1)
        lay.addLayout(ovr)
        lay.addStretch(1)
        return w

    def _build_settings(self) -> QWidget:
        w = QWidget()
        lay = QVBoxLayout(w)
        lay.setContentsMargins(0, 8, 0, 0)
        lay.setSpacing(6)
        row = QHBoxLayout()
        self.read_settings_btn = QPushButton("Ler da máquina ($$)")
        self.read_settings_btn.setIcon(icon("refresh"))
        self.read_settings_btn.clicked.connect(self.read_settings)
        self.write_settings_btn = QPushButton("Gravar alterações")
        self.write_settings_btn.setIcon(icon("save"))
        self.write_settings_btn.clicked.connect(self.write_settings)
        row.addWidget(self.read_settings_btn)
        row.addWidget(self.write_settings_btn)
        row.addStretch(1)
        lay.addLayout(row)
        self.table = QTableWidget(0, 5)
        self.table.setHorizontalHeaderLabels(["$", "Valor", "Nome", "Unidade", "Explicação"])
        self.table.verticalHeader().setVisible(False)
        self.table.setAlternatingRowColors(True)
        self.table.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
        self.table.setWordWrap(True)
        hh = self.table.horizontalHeader()
        hh.setSectionResizeMode(0, QHeaderView.ResizeMode.ResizeToContents)
        hh.setSectionResizeMode(1, QHeaderView.ResizeMode.Interactive)
        hh.setSectionResizeMode(2, QHeaderView.ResizeMode.Interactive)
        hh.setSectionResizeMode(3, QHeaderView.ResizeMode.ResizeToContents)
        hh.setSectionResizeMode(4, QHeaderView.ResizeMode.Stretch)
        self.table.setColumnWidth(1, 90)
        self.table.setColumnWidth(2, 200)
        self.table.setAccessibleName("Configurações do GRBL")
        lay.addWidget(self.table, 1)
        self._fill_settings_table()
        return w

    def _fill_settings_table(self) -> None:
        nums = sorted(set(self.codes.SETTINGS) | set(self._settings_values))
        self.table.setRowCount(len(nums))
        for r, n in enumerate(nums):
            name, unit, help_ = backend.setting_info(self.codes.SETTINGS.get(n))
            val = self._settings_values.get(n, "")
            items = [QTableWidgetItem(f"${n}"), QTableWidgetItem(val), QTableWidgetItem(name),
                     QTableWidgetItem(unit), QTableWidgetItem(help_)]
            for c, it in enumerate(items):
                if c != 1:
                    it.setFlags(it.flags() & ~Qt.ItemFlag.ItemIsEditable)
                it.setToolTip(help_ or name)
            items[0].setData(Qt.ItemDataRole.UserRole, n)
            items[1].setData(Qt.ItemDataRole.UserRole, val)
            for c, it in enumerate(items):
                self.table.setItem(r, c, it)

    # ------------------------------------------------------------------ conexão
    def refresh_ports(self) -> None:
        cur = self.port.currentText()
        ports = backend.list_ports(self.ctrl)
        self.port.clear()
        self.port.addItems(ports)
        if cur and cur in ports:
            self.port.setCurrentText(cur)

    def toggle_connection(self) -> None:
        if self.ctrl is None:
            return
        if self.connected:
            if self.streaming and QMessageBox.question(
                    self, "Desconectar", "Um programa está em execução. Desconectar mesmo assim?") \
                    != QMessageBox.StandardButton.Yes:
                return
            try:
                self.ctrl.disconnect()
            except Exception as exc:
                log.error("Falha ao desconectar: %s", exc)
            self._on_connection(False)
            return
        port = self.port.currentText().strip()
        if not port:
            return
        self.console_append("sys", f"Conectando a {port} @ {self.baud.currentText()}…")
        log.info("Conectando a %s", port)
        self.connect_btn.setEnabled(False)
        self.connect_btn.setText("Conectando…")
        self.bridge.run("connect", self.ctrl.connect, port, int(self.baud.currentData()))

    def _on_command_failed(self, name: str, msg: str) -> None:
        if name == "stream_program" and self.streaming:
            self._finish_stream(aborted=True)
            QMessageBox.warning(self, "Envio", f"Não foi possível iniciar o envio:\n{msg}")
        if name == "connect":
            self.connect_btn.setText("&Conectar")
            self._update_enabled()
            QMessageBox.warning(self, "Conexão", f"Não foi possível conectar:\n{msg}")
        self.console_append("err", f"{name}: {msg}")
        log.error("Comando %s falhou: %s", name, msg)
        if name == "home" and "por segurança" in msg:
            self.banner.show_message("warning", "Homing não iniciado", msg.split(": ", 1)[-1])
            QMessageBox.warning(self, "Homing não iniciado",
                                msg.split(": ", 1)[-1] + "\n\nUse Comandos e sonda › Fins de curso e homing para "
                                "testar as chaves e corrigir a configuração.")

    def _on_command_result(self, name: str, result: Any) -> None:
        if name == "read_settings" and isinstance(result, dict):
            self._settings_values = {int(k): f"{v:g}" if isinstance(v, float) else str(v)
                                     for k, v in result.items()}
            self._fill_settings_table()
            self.console_append("sys", f"{len(result)} configurações lidas.")
        elif name == "probe_z" and isinstance(result, (int, float)):
            self.console_append("sys", f"Sonda tocou em Z de máquina {float(result):.3f}. Z0 definido.")
        elif name == "set_work_zero":
            self.console_append("sys", "Zero de trabalho definido.")
        elif name == "safe_stop":
            if result:
                self.console_append("sys", "Máquina parada sem perder a posição. Use “Iniciar da linha…” "
                                           f"para retomar (parou perto da linha {self._resume_line}).")
            else:
                self.banner.show_message("warning", "Parada com a máquina em movimento",
                                         "A máquina não parou a tempo e o reset foi feito em movimento. "
                                         "Refaça o homing/zero antes de continuar.")
        elif name == "stream_program" and result:
            for w in result:
                self.console_append("warn", f"Aviso: {w}")

    def _on_connection(self, ok: bool) -> None:
        if ok == self.connected and self.connect_btn.isEnabled():
            return
        self.connected = ok
        self.connect_btn.setText("&Desconectar" if ok else "&Conectar")
        self.connect_btn.setIcon(icon("unplug" if ok else "plug"))
        theme.set_property(self.connect_btn, "primary", not ok)
        if not ok:
            self.streaming = False
            self.clock.stop()
            self._on_state("")
            for lab in self.dro_w + self.dro_m:
                lab.setText("—")
            self.toolPosition.emit(None)
            self.streamingChanged.emit(False)
        self.console_append("sys", "Conectado." if ok else "Desconectado.")
        self.connectedChanged.emit(ok)
        self._update_enabled()

    def _update_enabled(self) -> None:
        has = self.ctrl is not None
        c = has and self.connected
        busy = self.streaming
        for w in (self.home_btn, self.unlock_btn, self.zero_xy_btn, self.zero_xyz_btn, self.goto_btn,
                  self.probe_btn, *self.zero_axis_btns, self.jogpad, self.cmd, self.send_cmd_btn,
                  self.read_settings_btn, self.write_settings_btn):
            w.setEnabled(c and not busy)
        self.resume_btn.setEnabled(c and not busy and bool(self.program_lines))
        self.unlock_btn.setEnabled(c)
        self.reset_btn.setEnabled(c)
        self.connect_btn.setEnabled(has)
        self.port.setEnabled(has and not c)
        self.baud.setEnabled(has and not c)
        self.refresh_btn.setEnabled(has and not c)
        self.start_btn.setEnabled(c and not busy and bool(self.program_lines))
        self.pause_btn.setEnabled(c and busy)
        self.stop_btn.setEnabled(c and busy)

    # ------------------------------------------------------------------ status
    def _on_state(self, state: str) -> None:
        self.state = state
        self.chip.set_state(state)
        base = state.split(":")[0]
        title, text = backend.state_text(self.codes, state) if state else ("", "")
        if not text:
            text = title
        if not state:
            text = "Conecte-se à máquina para começar." if not self.connected else "Aguardando status…"
        self.state_help.setText(text)
        self.stateChanged.emit(state)
        if base == "Hold" and self.streaming:
            self.paused = True
            self.pause_btn.setText("&Retomar")
            self.pause_btn.setIcon(icon("play"))

    def _on_status(self, raw: dict) -> None:
        st = backend.normalize_status(raw)
        if st["state"] and st["state"] != self.state:
            self._on_state(st["state"])
        w, m = st["wpos"], st["mpos"]
        for i in range(3):
            if w:
                self.dro_w[i].setText(units.fmt_len(w[i], 3, with_unit=False))
            if m:
                self.dro_m[i].setText(units.fmt_len(m[i], 3, with_unit=False))
        if w:
            self.last_wpos = w
            self.toolPosition.emit(w)
        f, s = st["feed"], st["spindle"]
        if f is not None or s is not None:
            self.fs_label.setText(f"Avanço {float(f or 0):.0f} mm/min · Spindle {float(s or 0):.0f} RPM")
        if st["ov"] and len(st["ov"]) >= 3:
            self.ovr_labels["feed"].setText(f"{st['ov'][0]}%")
            self.ovr_labels["rapid"].setText(f"{st['ov'][1]}%")
            self.ovr_labels["spindle"].setText(f"{st['ov'][2]}%")

    _SETTING_RX = re.compile(r"^\$(\d+)\s*=\s*([^\s(]+)")

    def _on_line(self, direction: str, text: str) -> None:
        d = direction.lower()
        incoming = d in ("rx", "<", "in", "recv", "received")
        if incoming:
            m = self._SETTING_RX.match(text.strip())
            if m:
                self._settings_values[int(m.group(1))] = m.group(2)
                QTimer.singleShot(150, self._fill_settings_table)
            if text.strip() == "ok" and not self.show_ok.isChecked():
                return
            if text.startswith("<") and text.endswith(">"):
                return
        else:
            if text.strip() == "?":
                return
            if self.streaming and not self.show_ok.isChecked():
                return
        self.console_append("rx" if incoming else "tx", text)

    def _on_grbl_message(self, text: str) -> None:
        if text.startswith("Fim de curso") and "parada pelo BrokCAM" in text:
            self.banner.show_message("danger", "Parada de segurança", text)
            self.console_append("err", text)
            log.error(text)
            return
        self.console_append("sys", f"GRBL: {text}")

    def console_append(self, kind: str, text: str) -> None:
        p = theme.palette()
        col = {"tx": p.accent, "rx": p.text, "sys": p.text_muted, "err": p.danger, "warn": p.warning}.get(kind, p.text)
        prefix = {"tx": "›", "rx": "‹", "sys": "•", "err": "✖", "warn": "▲"}.get(kind, " ")
        self.console.appendHtml(f'<span style="color:{p.text_muted}">{prefix}</span> '
                                f'<span style="color:{col}">{html.escape(text)}</span>')
        self.console.moveCursor(QTextCursor.MoveOperation.End)
        # histórico completo no arquivo de log (nível DEBUG): ajuda a diagnosticar alarmes depois
        log.debug("console %s %s", kind, text)

    def _on_alarm(self, code: int, msg: str) -> None:
        title, text = backend.code_text(self.codes.ALARMS.get(code))
        title = title or msg or "Alarme"
        head = f"Alarme {code}: {title}" if code >= 0 else f"Alarme: {title}"
        body = (text or msg or "") + " Use Destravar ($X) ou Home ($H) depois de resolver a causa."
        self.banner.show_message("danger", head, body.strip())
        self.console_append("err", f"{head} — {text or msg}")
        log.error("%s — %s", head, text or msg)

    def _on_error(self, code: int, msg: str, line_no: int) -> None:
        title, text = backend.code_text(self.codes.ERRORS.get(code))
        title = title or msg or "Erro"
        head = f"Erro {code}: {title}" if code >= 0 else f"Erro: {title}"
        where = ""
        if line_no > 0 and self.program_lines:
            # o controlador informa a linha 1-based do arquivo enviado
            where = f" (linha {line_no} do programa)"
            self.programLine.emit(line_no - 1)
        self.banner.show_message("danger", head + where, text or msg)
        self.console_append("err", f"{head}{where} — {text or msg}")
        log.warning("%s%s — %s", head, where, text or msg)

    # ------------------------------------------------------------------ comandos
    def _call(self, names: tuple[str, ...], *args: Any, **kwargs: Any) -> bool:
        """Chama o primeiro método existente em ``names`` numa thread auxiliar."""
        if self.ctrl is None:
            return False
        for n in names:
            fn = getattr(self.ctrl, n, None)
            if callable(fn):
                self.bridge.run(n, fn, *args, **kwargs)
                return True
        log.warning("Controlador não implementa %s", names[0])
        return False

    def _realtime(self, names: tuple[str, ...]) -> None:
        """Comandos de tempo real (pausa, retomar, reset) são instantâneos: chama direto."""
        if self.ctrl is None:
            return
        try:
            backend.call_first(self.ctrl, names)
        except Exception as exc:  # noqa: BLE001
            self.console_append("err", f"{names[0]}: {exc}")

    def _send_cmd(self) -> None:
        text = self.cmd.text().strip()
        if not text:
            return
        self.cmd.push(text)
        self.cmd.clear()
        fn = getattr(self.ctrl, "send_command", None)
        if fn is not None:
            # wait=False: a resposta chega pelo on_line; não bloqueia a interface
            self.bridge.run("send_command", fn, text, wait=False)
        else:
            self._call(("send_line", "send"), text)

    def home(self) -> None:
        self.console_append("sys", "Homing ($H)…")
        self._call(("home",))

    def unlock(self) -> None:
        self.banner.hide()
        self._call(("unlock",))

    def reset(self) -> None:
        self._realtime(("soft_reset", "reset", "stop"))
        if self.streaming:
            self._finish_stream(aborted=True)

    def set_zero(self, axes: str) -> None:
        self._call(("set_work_zero", "zero"), axes)

    def goto_zero(self) -> None:
        self._call(("goto_work_zero", "goto_zero"))

    def _jog(self, dx: float, dy: float, dz: float) -> None:
        if not self.connected or self.streaming:
            return
        step = self.step_values[max(0, self.step_group.checkedId())]
        self._call(("jog",), dx * step, dy * step, dz * step, float(self.jog_feed.value()))

    def probe(self) -> None:
        dlg = QDialog(self)
        dlg.setWindowTitle("Sonda Z")
        lay = QVBoxLayout(dlg)
        form = ParamForm(PROBE_GROUP, show_presets=False)
        lay.addWidget(form)
        bb = QDialogButtonBox(QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel)
        bb.button(QDialogButtonBox.StandardButton.Ok).setText("Iniciar sonda")
        bb.accepted.connect(dlg.accept)
        bb.rejected.connect(dlg.reject)
        lay.addWidget(bb)
        dlg.resize(460, 480)
        if dlg.exec() != QDialog.DialogCode.Accepted:
            return
        vals = form.values()
        fn = getattr(self.ctrl, "probe_z", None)
        if fn is None:
            log.warning("Controlador não implementa probe_z")
            return
        kwargs: dict[str, Any] = {}
        try:
            for name in inspect.signature(fn).parameters:
                low = name.lower()
                if "feed" in low:
                    kwargs[name] = vals["feed"]
                elif any(k in low for k in ("depth", "max", "dist", "travel")):
                    kwargs[name] = vals["max_depth"]
                elif any(k in low for k in ("thick", "plate", "offset")):
                    kwargs[name] = vals["plate_thickness"]
        except (TypeError, ValueError):
            pass
        self.console_append("sys", "Sonda Z iniciada…")
        self.bridge.run("probe_z", fn, **kwargs)

    # ------------------------------------------------------------------ programa
    def load_program(self, text: str, name: str = "programa", estimate_s: float = 0.0) -> None:
        """Carrega o programa. As linhas são enviadas como estão (o controlador pula
        vazias/comentários mas mantém a numeração 1-based do arquivo)."""
        self.program_lines = text.splitlines()
        self.n_commands = sum(1 for ln in self.program_lines
                              if ln.strip() and not ln.strip().startswith((";", "(")))
        self.program_name = name
        self.estimate_s = estimate_s
        est = f" · tempo estimado {units.fmt_time(estimate_s)}" if estimate_s else ""
        self.prog_label.setText(f"<b>{html.escape(name)}</b> — {self.n_commands:,} comandos{est}".replace(",", "."))
        self.prog_bar.setValue(0)
        self.lines_label.setText("Linha —")
        self.elapsed.setText("Decorrido —")
        self.remaining.setText("Restante —")
        self.stream_start = 0.0
        self._resume_line = 0
        if self.tabs is not None:
            self.tabs.setCurrentIndex(0)
        self._update_enabled()
        self.programLoaded.emit()

    def _preflight(self) -> list[str]:
        """Problemas que impedem ou desaconselham iniciar o envio agora (texto para o usuário)."""
        base = self.state.split(":")[0]
        if base == "Alarm":
            return ["!A máquina está em alarme. Resolva a causa e use Destravar ou Home antes de enviar."]
        if base in ("Hold", "Door"):
            return ["!A máquina está pausada (Hold/Porta). Retome ou faça Reset antes de iniciar outro programa."]
        if base in ("Run", "Jog", "Home"):
            return ["!A máquina ainda está em movimento. Aguarde ficar Ocioso."]
        probs: list[str] = []
        if self.check_travel.isChecked() and self.ctrl is not None:
            try:
                from brokcam.machine import safety
                settings = dict(getattr(self.ctrl, "settings", {}) or {})
                wco = getattr(self.ctrl, "wco", None)
                if settings and wco is not None:
                    b = safety.program_bounds(self.program_lines)
                    if b is not None:
                        probs += safety.envelope_problems(b, tuple(wco[:3]), settings)
            except Exception:  # noqa: BLE001 - verificação auxiliar nunca impede por erro interno
                log.exception("Falha ao conferir o curso da máquina")
        return probs

    def start_program(self, start_line: int = 1) -> None:
        if not self.program_lines or self.ctrl is None or not self.connected:
            return
        probs = self._preflight()
        hard = [p[1:] for p in probs if p.startswith("!")]
        if hard:
            QMessageBox.warning(self, "Não é possível iniciar", "\n\n".join(hard))
            return
        if probs:
            box = QMessageBox(QMessageBox.Icon.Warning, "Programa fora do curso da máquina",
                              "O programa, com o zero de trabalho atual, sairia do curso da máquina:\n\n"
                              + "\n".join(probs) + "\n\nO GRBL pararia com ALARM:2 (limite por software) ou a "
                              "máquina bateria no fim do curso. Reposicione o zero de trabalho.",
                              QMessageBox.StandardButton.Ignore | QMessageBox.StandardButton.Cancel, self)
            box.button(QMessageBox.StandardButton.Ignore).setText("Enviar assim mesmo")
            box.setDefaultButton(QMessageBox.StandardButton.Cancel)
            if box.exec() != QMessageBox.StandardButton.Ignore:
                return
        if self.confirm_send or start_line > 1:
            wpos = self.last_wpos
            pos = (f"Posição de trabalho atual: X {wpos[0]:.3f}  Y {wpos[1]:.3f}  Z {wpos[2]:.3f}"
                   if wpos else "Posição atual desconhecida.")
            where = (f" a partir da linha {start_line}" if start_line > 1 else "")
            extra = ("\n\nAntes de continuar, o BrokCAM sobe ao Z seguro (5 mm), liga o spindle como estava, "
                     "vai ao ponto XY da linha anterior e desce devagar." if start_line > 1 else "")
            msg = (f"Enviar “{self.program_name}” ({self.n_commands} comandos){where} para a máquina?\n\n"
                   f"{pos}\n\nConfirme que o zero de trabalho (X0 Y0 e Z0 na superfície da placa) "
                   f"está definido e que a ferramenta correta está instalada.{extra}")
            box = QMessageBox(QMessageBox.Icon.Question, "Iniciar programa", msg,
                              QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.Cancel, self)
            box.button(QMessageBox.StandardButton.Yes).setText("Iniciar")
            if box.exec() != QMessageBox.StandardButton.Yes:
                return
        # o console não precisa de cada linha/ok do programa: menos sinais entre threads
        if hasattr(self.ctrl, "echo_stream"):
            self.ctrl.echo_stream = bool(self.show_ok.isChecked())
        fn = getattr(self.ctrl, "stream_program", None)
        if fn is not None:
            self.bridge.run("stream_program", fn, list(self.program_lines), start_line=int(start_line),
                            m6_pause=bool(self.m6_pause.isChecked()))
        elif not self._call(("send_program", "stream"), list(self.program_lines)):
            return
        self.streaming = True
        self.paused = False
        self.stream_start = time.monotonic()
        self.paused_total = 0.0
        self._last_progress = (0, 0, len(self.program_lines))
        self.banner.hide()
        self.clock.start()
        self.pause_btn.setText("&Pausar")
        self.pause_btn.setIcon(icon("pause"))
        self.streamingChanged.emit(True)
        self._update_enabled()
        log.info("Envio iniciado: %s (%d comandos)", self.program_name, self.n_commands)

    def start_from_line(self) -> None:
        """Pergunta a linha inicial (sugere onde o último envio parou) e inicia dali."""
        if not self.program_lines:
            return
        n = len(self.program_lines)
        last = int(getattr(self.ctrl, "last_stream_line", 0) or 0) or self._resume_line
        suggestion = min(max(1, last), n)
        line, ok = QInputDialog.getInt(
            self, "Iniciar da linha",
            f"Linha inicial (1–{n}). O último envio parou na linha {last or '—'}.\n"
            "O estado das linhas anteriores é restaurado antes de continuar.",
            suggestion, 1, n)
        if ok:
            self.start_program(int(line))

    def toggle_pause(self) -> None:
        if not self.streaming:
            return
        if self.paused:
            self._realtime(("resume", "cycle_start"))
            self.paused = False
            self.paused_total += time.monotonic() - self.pause_start
            self.pause_btn.setText("&Pausar")
            self.pause_btn.setIcon(icon("pause"))
        else:
            self._realtime(("pause", "feed_hold"))
            self.paused = True
            self.pause_start = time.monotonic()
            self.pause_btn.setText("&Retomar")
            self.pause_btn.setIcon(icon("play"))

    def stop_program(self) -> None:
        """Parada segura (feed hold → espera parar → reset), sem perder a posição."""
        if not self.streaming:
            return
        self._resume_line = int(getattr(self.ctrl, "last_stream_line", 0) or self._last_progress[1] or 0)
        fn = getattr(self.ctrl, "safe_stop", None)
        if fn is not None:
            self.console_append("warn", "Parando: segurando o movimento antes do reset…")
            self.bridge.run("safe_stop", fn)
        else:
            self._realtime(("stop", "soft_reset", "reset"))
        self._finish_stream(aborted=True)

    def _finish_stream(self, aborted: bool = False) -> None:
        if not self.streaming:
            return
        self.streaming = False
        self.paused = False
        self.clock.stop()
        self._update_times()
        self.streamingChanged.emit(False)
        self._update_enabled()
        msg = "Programa interrompido." if aborted else "Programa concluído."
        self.console_append("warn" if aborted else "sys", msg)
        if not aborted:
            self.prog_bar.setValue(1000)
        log.info(msg)

    def _on_progress(self, sent: int, acked: int, total: int) -> None:
        """``acked``/``total`` são números de linha (1-based) do programa enviado."""
        self._last_progress = (sent, acked, total)
        if total > 0:
            self.prog_bar.setValue(int(1000 * min(acked, total) / total))
        if acked > 0:
            self.lines_label.setText(f"Linha {acked:,} de {total:,}".replace(",", "."))
            self.programLine.emit(acked - 1)

    def _update_times(self) -> None:
        if not self.stream_start:
            return
        now = time.monotonic()
        paused_now = (now - self.pause_start) if self.paused else 0.0
        el = now - self.stream_start - self.paused_total - paused_now
        self.elapsed.setText(f"Decorrido {units.fmt_time(el)}")
        _, acked, total = self._last_progress
        frac = acked / total if total else 0.0
        if self.estimate_s > 0:
            rem = max(0.0, self.estimate_s * (1 - frac))
            if frac > 0.05 and el > 5:
                rem = el / frac * (1 - frac)
            self.remaining.setText(f"Restante ≈ {units.fmt_time(rem)}")
        elif frac > 0.02:
            self.remaining.setText(f"Restante ≈ {units.fmt_time(el / frac * (1 - frac))}")

    # ------------------------------------------------------------------ $$
    def read_settings(self) -> None:
        if self.ctrl is not None and hasattr(self.ctrl, "read_settings"):
            self._call(("read_settings",))
        else:
            self._call(("send_command",), "$$", wait=False)

    def write_settings(self) -> None:
        changes: list[tuple[int, str]] = []
        for r in range(self.table.rowCount()):
            n = self.table.item(r, 0).data(Qt.ItemDataRole.UserRole)
            it = self.table.item(r, 1)
            new = (it.text() or "").strip().replace(",", ".")
            old = str(it.data(Qt.ItemDataRole.UserRole) or "")
            if new and new != old:
                try:
                    float(new)
                except ValueError:
                    QMessageBox.warning(self, "Configurações", f"Valor inválido para ${n}: {new}")
                    return
                changes.append((int(n), new))
        if not changes:
            QMessageBox.information(self, "Configurações", "Nenhum valor foi alterado.")
            return
        lst = "\n".join(f"${n} = {v}" for n, v in changes)
        if QMessageBox.question(self, "Gravar configurações",
                                f"Gravar {len(changes)} configuração(ões) na EEPROM do GRBL?\n\n{lst}") \
                != QMessageBox.StandardButton.Yes:
            return
        ctrl = self.ctrl

        def write_all() -> dict[int, float]:
            for n, v in changes:
                if hasattr(ctrl, "write_setting"):
                    ctrl.write_setting(n, float(v))
                else:
                    ctrl.send_command(f"${n}={v}")
            return ctrl.read_settings() if hasattr(ctrl, "read_settings") else {}

        self.bridge.run("read_settings", write_all)

    def shutdown(self) -> None:
        if self.ctrl is not None and self.connected:
            try:
                self.ctrl.disconnect()
            except Exception:  # noqa: BLE001
                pass
