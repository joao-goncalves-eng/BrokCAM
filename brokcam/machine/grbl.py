"""Controlador GRBL 1.1 em Python puro (sem Qt).

``GrblController`` gerencia a conexão serial (ou o simulador), lê as respostas numa
thread própria, consulta o status periodicamente e envia programas pelo protocolo de
**contagem de caracteres** (mantém o buffer RX de 128 bytes do GRBL sempre cheio,
máximo desempenho) ou, opcionalmente, pelo protocolo simples envia-espera-``ok``.

A interface recebe eventos por atributos chamáveis (``on_status``, ``on_line``,
``on_progress``, ``on_alarm``, ``on_error``, ``on_state_change``, ``on_connection``,
``on_stream_done``, ``on_message``). Eles são chamados a partir das threads internas;
a UI deve reencaminhá-los para a thread da GUI (ex.: emitindo sinais Qt).
"""
from __future__ import annotations

import logging
import re
import threading
import time
from collections import deque
from dataclasses import dataclass, field
from typing import Any, Callable, Iterable

from brokcam.machine import firmware as fwmod
from brokcam.machine import safety
from brokcam.machine.firmware import FirmwareInfo
from brokcam.machine.grbl_codes import alarm_text, error_text
from brokcam.machine.transport import (
    SerialTransport, TcpTransport, Transport, is_tcp_url, list_serial_ports,
)

__all__ = ["GrblController", "GrblStatus", "CommandResult", "GrblError", "RT",
           "parse_status", "SIMULATOR_PORT", "SIMULATOR_DLC32_PORT", "DLC32_AP_URL"]

log = logging.getLogger(__name__)

SIMULATOR_PORT = "SIMULADOR"
SIMULATOR_DLC32_PORT = "SIMULADOR-DLC32"     # máquina virtual MKS DLC32 (Grbl_Esp32)
DLC32_AP_URL = "tcp://192.168.4.1:23"        # WiFi da DLC32 no modo AP (padrão de fábrica)


class RT:
    """Bytes de comandos em tempo real do GRBL 1.1 (não ocupam o buffer RX)."""
    STATUS = b"?"
    FEED_HOLD = b"!"
    CYCLE_START = b"~"
    SOFT_RESET = b"\x18"
    SAFETY_DOOR = b"\x84"
    JOG_CANCEL = b"\x85"
    FEED_100 = b"\x90"
    FEED_PLUS_10 = b"\x91"
    FEED_MINUS_10 = b"\x92"
    FEED_PLUS_1 = b"\x93"
    FEED_MINUS_1 = b"\x94"
    RAPID_100 = b"\x95"
    RAPID_50 = b"\x96"
    RAPID_25 = b"\x97"
    SPINDLE_100 = b"\x99"
    SPINDLE_PLUS_10 = b"\x9a"
    SPINDLE_MINUS_10 = b"\x9b"
    SPINDLE_PLUS_1 = b"\x9c"
    SPINDLE_MINUS_1 = b"\x9d"
    SPINDLE_STOP = b"\x9e"
    FLOOD_TOGGLE = b"\xa0"
    MIST_TOGGLE = b"\xa1"


class GrblError(RuntimeError):
    """Erro de comunicação ou comando recusado pelo GRBL."""

    def __init__(self, message: str, code: int | None = None) -> None:
        super().__init__(message)
        self.code = code


# --------------------------------------------------------------------------- status

Vec3 = tuple[float, float, float]


@dataclass
class GrblStatus:
    """Relatório de status ``<...>`` interpretado."""
    state: str                         # "Idle", "Run", "Hold:0", "Door:1", "Alarm"...
    mpos: Vec3 | None = None           # posição de máquina
    wpos: Vec3 | None = None           # posição de trabalho
    wco: Vec3 | None = None            # deslocamento de trabalho (MPos - WPos)
    feed: float = 0.0                  # avanço atual (mm/min)
    spindle: float = 0.0               # rotação/potência programada atual
    overrides: tuple[int, int, int] | None = None   # avanço, rápido, spindle (%)
    pins: str = ""                     # ex.: "XYZP" (fins de curso/sonda acionados)
    planner_free: int | None = None    # blocos livres no planejador (Bf)
    rx_free: int | None = None         # bytes livres no buffer RX (Bf)
    line_number: int | None = None     # Ln
    accessories: str = ""              # A: S/C/F/M
    raw: str = ""

    @property
    def base_state(self) -> str:
        """Estado sem subestado ("Hold:0" -> "Hold")."""
        return self.state.split(":")[0]


def _vec(text: str) -> tuple[float, ...]:
    return tuple(float(v) for v in text.split(","))


def parse_status(text: str, wco_cache: Vec3 | None = None) -> GrblStatus:
    """Interpreta um relatório ``<Idle|MPos:..|WPos:..|WCO:..|FS:..|Ov:..|Pn:..|Bf:..>``.

    ``wco_cache`` é o último WCO conhecido (o GRBL só o envia a cada 10–30
    relatórios); ele é usado para calcular WPos a partir de MPos (ou vice-versa).
    """
    body = text.strip()
    if body.startswith("<"):
        body = body[1:]
    if body.endswith(">"):
        body = body[:-1]
    fields = body.split("|")
    st = GrblStatus(state=fields[0], raw=text)
    for f in fields[1:]:
        key, _, val = f.partition(":")
        try:
            if key == "MPos":
                st.mpos = _vec(val)[:3]  # type: ignore[assignment]
            elif key == "WPos":
                st.wpos = _vec(val)[:3]  # type: ignore[assignment]
            elif key == "WCO":
                st.wco = _vec(val)[:3]  # type: ignore[assignment]
            elif key == "FS":
                v = _vec(val)
                st.feed = v[0]
                st.spindle = v[1] if len(v) > 1 else 0.0
            elif key == "F":
                st.feed = float(val)
            elif key == "Ov":
                v = _vec(val)
                st.overrides = (int(v[0]), int(v[1]), int(v[2]))
            elif key == "Pn":
                st.pins = val
            elif key == "Bf":
                v = _vec(val)
                st.planner_free, st.rx_free = int(v[0]), int(v[1])
            elif key == "Ln":
                st.line_number = int(val)
            elif key == "A":
                st.accessories = val
        except ValueError:
            continue
    wco = st.wco or wco_cache
    if wco is not None:
        if st.mpos is not None and st.wpos is None:
            st.wpos = tuple(st.mpos[i] - wco[i] for i in range(3))  # type: ignore[assignment]
        elif st.wpos is not None and st.mpos is None:
            st.mpos = tuple(st.wpos[i] + wco[i] for i in range(3))  # type: ignore[assignment]
        st.wco = wco
    elif st.mpos is not None and st.wpos is None:
        st.wpos = st.mpos
    return st


# --------------------------------------------------------------------------- fila

@dataclass
class CommandResult:
    """Resposta de um comando: ``ok``/``error`` e as linhas recebidas antes dele."""
    ok: bool
    error: int | None = None
    lines: list[str] = field(default_factory=list)

    @property
    def message(self) -> str:
        return "ok" if self.ok else error_text(self.error or 0)


@dataclass
class _Pending:
    text: str
    nbytes: int
    line_no: int | None = None         # linha (1-based) do programa, se for streaming
    stream_id: int = 0
    event: threading.Event = field(default_factory=threading.Event)
    error: int | None = None
    done: bool = False
    aborted: bool = False
    responses: list[str] = field(default_factory=list)


_COMMENT_RE = re.compile(r"\(([^)]*)\)|;.*$")
_RT_CHARS = str.maketrans("", "", "?!~")


def _clean_line(line: str, strip_comments: bool) -> str:
    """Prepara uma linha para envio: remove comentários (mantém MSG) e caracteres de
    tempo real que o GRBL extrairia do meio da linha."""
    s = line.strip()
    if not s:
        return ""
    if strip_comments and ("(" in s or ";" in s):
        def keep(m: re.Match) -> str:
            inner = m.group(1)
            if inner is not None and inner.lstrip().upper().startswith("MSG"):
                return "(" + inner + ")"
            return ""
        s = _COMMENT_RE.sub(keep, s).strip()
    s = s.translate(_RT_CHARS)
    return s.encode("ascii", "replace").decode("ascii")


# --------------------------------------------------------------------------- controlador

Callback = Callable[..., Any] | None


class GrblController:
    """Conexão com uma máquina GRBL 1.1 (real ou simulada).

    Parâmetros:
        status_interval: intervalo entre consultas ``?`` em segundos (0,2 = 5 Hz).
        rx_buffer_size: tamanho do buffer serial do GRBL (128 no Arduino Uno).
        strip_comments: remove comentários antes de enviar (economiza buffer).
        stop_on_error: ao receber ``error:N`` durante o envio, para de enviar e aplica
            feed hold; ``resume()`` continua, ``stop()`` aborta.
        echo_status: repassa os relatórios ``<...>`` também para ``on_line``.
        echo_stream: repassa cada linha enviada/``ok`` do programa para ``on_line``.
    """

    def __init__(self, status_interval: float = 0.2, rx_buffer_size: int = 128,
                 strip_comments: bool = True, stop_on_error: bool = True,
                 echo_status: bool = False, echo_stream: bool = True) -> None:
        self.status_interval = status_interval
        self.limit_guard = True          # parar se um fim de curso for acionado em movimento (reforço)
        self._idle_pins = ""
        self._guard_tripped = False
        self._limit_guard_until = 0.0
        self.rx_buffer_size = rx_buffer_size
        self.strip_comments = strip_comments
        self.stop_on_error = stop_on_error
        self.echo_status = echo_status
        self.echo_stream = echo_stream

        # callbacks (atribua funções; chamadas a partir de threads internas)
        self.on_status: Callback = None          # (GrblStatus)
        self.on_line: Callback = None            # (direction: "tx"|"rx"|"info", text)
        self.on_progress: Callback = None        # (sent, acked, total)
        self.on_alarm: Callback = None           # (code, msg)
        self.on_error: Callback = None           # (code, msg, line_no | None)
        self.on_state_change: Callback = None    # (old_state, new_state)
        self.on_connection: Callback = None      # (connected: bool)
        self.on_stream_done: Callback = None     # (completed: bool)
        self.on_message: Callback = None         # (text) — [MSG:...] do GRBL

        self._lock = threading.RLock()
        self._cv = threading.Condition(self._lock)
        self._write_lock = threading.Lock()
        self._transport: Transport | None = None
        self._threads: list[threading.Thread] = []
        self._running = False
        self._reset_runtime()

    # ================================================================== estado público
    def _reset_runtime(self) -> None:
        self._pending: deque[_Pending] = deque()
        self._rx_used = 0
        self._banner_event = threading.Event()
        self.version: str | None = None
        self.port: str | None = None
        self.state: str = "Desconectado"
        self.status: GrblStatus | None = None
        self.wco: Vec3 | None = None
        self.settings: dict[int, float] = {}
        self.extended_settings: dict[str, str] = {}   # $S (Grbl_Esp32/DLC32)
        self.firmware: FirmwareInfo = FirmwareInfo()
        self.banner: str = ""
        self.build_info_lines: list[str] = []
        self.last_probe: tuple[float, float, float, bool] | None = None
        self._last_rx_time = 0.0
        self._last_status_time = 0.0
        self._watchdog_warned = False
        # streaming
        self._stream_id = 0
        self._streaming = False
        self._stream_thread: threading.Thread | None = None
        self._stream_stop = False
        self._error_hold = False
        self._stream_total = 0
        self._stream_sent = 0
        self._stream_acked = 0
        self._stream_ok = True
        self._stream_done_event = threading.Event()
        self._stream_done_event.set()
        self._check_exit_pending = False
        self._progress_t = 0.0
        self.last_stream_line = 0          # última linha confirmada do último envio (para retomar)

    @property
    def is_connected(self) -> bool:
        return self._running and self._transport is not None

    @property
    def is_streaming(self) -> bool:
        return self._streaming

    @property
    def connection_kind(self) -> str:
        """"serial", "wifi" (TCP/Telnet), "simulador" ou "" (desconectado)."""
        if not self.is_connected or not self.port:
            return ""
        if self.port.upper().startswith(SIMULATOR_PORT):
            return "simulador"
        if is_tcp_url(self.port) or isinstance(self._transport, TcpTransport):
            return "wifi"
        return "serial"

    @property
    def rx_used(self) -> int:
        """Bytes atualmente ocupados no buffer RX do GRBL (contagem de caracteres)."""
        with self._lock:
            return self._rx_used

    # ================================================================== portas
    @staticmethod
    def list_ports() -> list[tuple[str, str]]:
        """Portas seriais disponíveis + a máquina virtual ``SIMULADOR``."""
        ports = list_serial_ports()
        ports.append((SIMULATOR_PORT, "Máquina GRBL virtual (simulador)"))
        ports.append((SIMULATOR_DLC32_PORT, "Máquina virtual MKS DLC32 / Grbl_Esp32 (simulador)"))
        ports.append((DLC32_AP_URL, "WiFi da MKS DLC32 no modo AP (Telnet, porta 23)"))
        return ports

    # ================================================================== conexão
    def connect(self, port: str, baud: int = 115200, transport: Transport | None = None,
                timeout: float = 3.0, simulator_speed: float = 1.0) -> None:
        """Abre a porta, aguarda o banner ``Grbl 1.1x`` e inicia as threads.

        ``port == "SIMULADOR"`` cria um ``GrblSimulator`` (com ``simulator_speed``);
        ``"SIMULADOR-DLC32"`` simula uma MKS DLC32 (Grbl_Esp32). ``port`` no formato
        ``tcp://IP:porta`` (ou ``telnet://``) conecta pelo WiFi via ``TcpTransport``
        (ex.: ``tcp://192.168.4.1:23`` na DLC32 em modo AP); ``baud`` é ignorado.
        Um ``transport`` já pronto pode ser passado diretamente (testes).
        Se o banner não chegar em ``timeout`` s é enviado um soft reset; se ainda
        assim não houver resposta, a conexão é fechada e ``TimeoutError`` é levantado.
        """
        if self.is_connected:
            self.disconnect()
        if transport is None:
            if port.upper() == SIMULATOR_PORT:
                from brokcam.machine.simulator import GrblSimulator
                transport = GrblSimulator(speed=simulator_speed)
            elif port.upper() == SIMULATOR_DLC32_PORT:
                from brokcam.machine.simulator import GrblSimulator
                transport = GrblSimulator(speed=simulator_speed, firmware="mks_dlc32")
            elif is_tcp_url(port):
                transport = TcpTransport.from_url(port, connect_timeout=timeout)
            else:
                transport = SerialTransport(port, baud)
                timeout = max(timeout, 5.0)     # ESP32 (DLC32) leva ~1–2 s para dar boot e mostrar o banner
        with self._lock:
            self._reset_runtime()
            self._transport = transport
            self.port = port
            self._running = True
            self._last_rx_time = time.monotonic()
        reader = threading.Thread(target=self._reader_loop, name="GrblReader", daemon=True)
        self._threads = [reader]
        reader.start()

        if not self._banner_event.wait(timeout):
            self._emit_line("info", "Sem banner do GRBL; enviando reset (ctrl-X)...")
            self._write_raw(RT.SOFT_RESET)
            if not self._banner_event.wait(timeout):
                self.disconnect()
                raise TimeoutError(
                    f"A placa em {port} não respondeu como GRBL. Verifique: (1) se é a porta certa — a CNC "
                    f"costuma aparecer como CH340/CP210x, não Bluetooth; (2) a velocidade (baud): GRBL e MKS "
                    f"DLC32 usam 115200; (3) se a placa está ligada e com firmware GRBL; (4) troque o cabo USB "
                    f"(alguns só carregam, sem dados).")
        poller = threading.Thread(target=self._poll_loop, name="GrblStatusPoll", daemon=True)
        self._threads.append(poller)
        poller.start()
        self._emit("on_connection", True)

    def disconnect(self) -> None:
        """Fecha a conexão (aborta envio em andamento sem resetar a máquina)."""
        self._shutdown(lost=False)

    def _shutdown(self, lost: bool) -> None:
        with self._lock:
            if not self._running:
                return
            self._running = False
            tr = self._transport
            self._transport = None
            self._abort_stream_locked(ok=False)
            self._fail_pending_locked()
            old = self.state
            self.state = "Desconectado"
            self._cv.notify_all()
        if tr is not None:
            try:
                tr.close()
            except Exception:
                pass
        cur = threading.current_thread()
        for t in self._threads:
            if t is not cur and t.is_alive():
                t.join(timeout=1.0)
        self._threads = []
        if lost:
            self._emit_line("info", "Conexão perdida (cabo desconectado ou dispositivo removido).")
        self._emit("on_state_change", old, "Desconectado")
        self._emit("on_connection", False)
        self._finish_stream_callbacks()

    # ================================================================== threads
    def _reader_loop(self) -> None:
        buf = bytearray()
        while self._running:
            tr = self._transport
            if tr is None:
                break
            try:
                data = tr.read()
            except (OSError, ValueError) as exc:
                if self._running:
                    log.warning("Erro de leitura: %s", exc)
                    threading.Thread(target=self._shutdown, args=(True,), daemon=True).start()
                return
            if not data:
                continue
            self._last_rx_time = time.monotonic()
            buf += data
            while True:
                i = buf.find(b"\n")
                if i < 0:
                    break
                raw = bytes(buf[:i]).decode("ascii", "replace").strip("\r ")
                del buf[: i + 1]
                if raw:
                    try:
                        self._handle_line(raw)
                    except Exception:  # nunca derruba a thread de leitura
                        log.exception("Erro ao tratar linha %r", raw)

    def _poll_loop(self) -> None:
        while self._running:
            self._write_raw(RT.STATUS)
            time.sleep(self.status_interval)
            if (self._running and not self._watchdog_warned and self._last_status_time
                    and time.monotonic() - self._last_status_time > max(3.0, 10 * self.status_interval)):
                self._watchdog_warned = True
                self._emit_line("info", "GRBL não responde às consultas de status.")

    # ================================================================== recepção
    def _handle_line(self, line: str) -> None:
        if line.startswith("<"):
            self._handle_status(line)
            return
        if line == "ok":
            self._ack(None, line)
            return
        if line.startswith("error:"):
            try:
                code = int(line[6:])
            except ValueError:
                code = 0
            self._ack(code, line)
            return
        self._emit_line("rx", line)
        if line.startswith("ALARM:"):
            try:
                code = int(line[6:])
            except ValueError:
                code = 0
            self._handle_alarm(code)
            return
        if line.startswith("Grbl ") or line.startswith("GrblHAL "):
            self._handle_banner(line)
            return
        if line.startswith("[MSG:"):
            self._emit("on_message", line[5:-1] if line.endswith("]") else line[5:])
        elif line.startswith("[PRB:"):
            m = re.match(r"\[PRB:([^:]+):(\d)\]", line)
            if m:
                v = _vec(m.group(1))
                self.last_probe = (v[0], v[1], v[2], m.group(2) == "1")
        with self._lock:
            if self._pending:
                self._pending[0].responses.append(line)

    def _limit_guard(self, st: GrblStatus) -> None:
        """Proteção extra por software: se um fim de curso que estava SOLTO em repouso for
        acionado durante um movimento (fora do homing), para a máquina na hora.

        É um reforço — quem deve parar a máquina é o limite físico da placa ($21=1). Chaves já
        lidas como acionadas em repouso (configuração invertida) não disparam a proteção, para
        não travar a máquina; nesse caso o pré-teste do homing avisa o problema.
        """
        base = st.base_state
        pins = "".join(a for a in "XYZABC" if a in (st.pins or ""))
        if base in ("Idle", "Alarm", "Check", "Sleep"):
            self._idle_pins = pins
            return
        if not self.limit_guard or base == "Home" or time.monotonic() < self._limit_guard_until:
            return
        new = "".join(a for a in pins if a not in self._idle_pins)
        if new and base in ("Run", "Jog") and not self._guard_tripped:
            self._guard_tripped = True
            self._write_raw(RT.FEED_HOLD)
            if base == "Jog":
                self._write_raw(RT.JOG_CANCEL)
            self._write_raw(RT.SOFT_RESET)
            msg = (f"Fim de curso {', '.join(new)} acionado durante o movimento — a máquina foi parada pelo "
                   "BrokCAM (reset). Afaste a ferramenta da chave com o jog após destravar ($X). Ative o "
                   "limite físico na placa ($21=1) para a proteção não depender do computador.")
            self._emit_line("info", msg)
            self._emit("on_message", msg)
        elif not new:
            self._guard_tripped = False

    def _handle_status(self, line: str) -> None:
        st = parse_status(line, self.wco)
        self._limit_guard(st)
        self._last_status_time = time.monotonic()
        self._watchdog_warned = False
        with self._lock:
            if st.wco is not None:
                self.wco = st.wco
            old = self.state
            self.state = st.state
            self.status = st
            self._cv.notify_all()
        if self.echo_status:
            self._emit_line("rx", line)
        if old != st.state:
            self._emit("on_state_change", old, st.state)
        self._emit("on_status", st)

    def _handle_alarm(self, code: int) -> None:
        with self._lock:
            old = self.state
            self.state = "Alarm"
            self._abort_stream_locked(ok=False)
        self._emit("on_alarm", code, alarm_text(code))
        if old != "Alarm":
            self._emit("on_state_change", old, "Alarm")

    def _handle_banner(self, line: str) -> None:
        m = re.match(r"Grbl(?:HAL)?\s+(\S+)", line)
        with self._lock:
            self.version = m.group(1) if m else line
            self.banner = line
            self.firmware = fwmod.detect_firmware(line, self.build_info_lines)
            # reset: o GRBL esvaziou seus buffers — tudo que estava pendente se perdeu
            self._fail_pending_locked()
            if self._streaming and not self._check_exit_pending:
                self._abort_stream_locked(ok=False)
            self._check_exit_pending = False
            self._cv.notify_all()
        self._banner_event.set()
        self._emit_line("info", f"GRBL {self.version} pronto.")

    def _ack(self, code: int | None, line: str) -> None:
        err_cb = None
        progress = None
        with self._lock:
            if not self._pending:
                echo = True
                p = None
            else:
                p = self._pending.popleft()
                self._rx_used -= p.nbytes
                p.error = code
                p.done = True
                p.event.set()
                echo = p.line_no is None or self.echo_stream or code is not None
                if p.line_no is not None and p.stream_id == self._stream_id and not p.aborted:
                    self._stream_acked = p.line_no
                    self.last_stream_line = p.line_no
                    if code is not None or self._progress_due():
                        progress = (self._stream_sent, self._stream_acked, self._stream_total)
                    if code is not None:
                        self._stream_ok = False
                        if self.stop_on_error:
                            self._error_hold = True
                if code is not None and not (p.aborted):
                    err_cb = (code, error_text(code), p.line_no)
            self._cv.notify_all()
        if echo:
            self._emit_line("rx", line if code is None else f"{line} ({error_text(code)})")
        if err_cb is not None:
            if p is not None and p.line_no is not None and self.stop_on_error and self._streaming:
                self._write_raw(RT.FEED_HOLD)
            self._emit("on_error", *err_cb)
        if progress is not None:
            self._emit("on_progress", *progress)

    # ================================================================== envio
    def _write_raw(self, data: bytes) -> bool:
        tr = self._transport
        if tr is None or not self._running:
            return False
        try:
            with self._write_lock:
                tr.write(data)
            return True
        except (OSError, ValueError) as exc:
            if self._running:
                log.warning("Erro de escrita: %s", exc)
                threading.Thread(target=self._shutdown, args=(True,), daemon=True).start()
            return False

    def send_realtime(self, cmd: bytes) -> bool:
        """Envia um comando de tempo real (1 byte; não usa o buffer RX)."""
        if len(cmd) != 1:
            raise ValueError("Comando de tempo real deve ter 1 byte.")
        return self._write_raw(cmd)

    def _progress_due(self) -> bool:
        """Limita ``on_progress`` a ~20 por segundo (chamar com o lock).

        Com o buffer cheio o GRBL confirma centenas de linhas curtas por segundo; repassar
        cada uma à interface (sinal Qt + repintura do editor) não traz informação e rouba
        CPU da thread de leitura. O progresso final é sempre emitido no fim do envio.
        """
        now = time.monotonic()
        if now - self._progress_t >= 0.05:
            self._progress_t = now
            return True
        return False

    def _space_limit(self) -> int:
        return self.rx_buffer_size - 1

    def _enqueue_locked(self, text: str, line_no: int | None, timeout: float | None,
                        stream_id: int = 0, sr_mode: bool = False) -> _Pending:
        """Aguarda espaço no buffer RX e envia ``text``. Deve ser chamado com o lock."""
        data = (text + "\n").encode("ascii", "replace")
        n = len(data)
        if n > self._space_limit():
            raise GrblError(f"Linha com {n} bytes excede o buffer do GRBL ({self.rx_buffer_size}).", 11)
        deadline = None if timeout is None else time.monotonic() + timeout
        while True:
            if not self._running:
                raise GrblError("Não conectado.")
            if sr_mode:
                fits = not self._pending
            else:
                fits = self._rx_used + n <= self._space_limit()
            if fits:
                break
            if line_no is not None and (self._stream_stop or stream_id != self._stream_id):
                raise _StreamAborted()
            remaining = None if deadline is None else deadline - time.monotonic()
            if remaining is not None and remaining <= 0:
                raise TimeoutError("Tempo esgotado aguardando espaço no buffer do GRBL.")
            self._cv.wait(0.5 if remaining is None else min(0.5, remaining))
        p = _Pending(text=text, nbytes=n, line_no=line_no, stream_id=stream_id)
        self._pending.append(p)
        self._rx_used += n
        if not self._write_raw(data):
            raise GrblError("Falha ao escrever na porta.")
        return p

    def send_command(self, cmd: str, wait: bool = True, timeout: float = 10.0) -> CommandResult | None:
        """Envia uma linha (G-code ou $) e, se ``wait``, aguarda ``ok``/``error``.

        Retorna ``CommandResult`` com as linhas recebidas antes do ``ok`` (ex.: saída
        de ``$$``). Levanta ``TimeoutError`` se não houver resposta em ``timeout`` s e
        ``RuntimeError`` se um programa estiver sendo enviado.
        """
        text = _clean_line(cmd, strip_comments=False)
        if not text:
            return CommandResult(ok=True)
        if not self._banner_event.wait(min(timeout, 3.0)):
            self._emit_line("info", "Reset sem banner do GRBL; enviando assim mesmo.")
            self._banner_event.set()
        with self._lock:
            if self._streaming:
                raise RuntimeError("Há um programa em envio; aguarde terminar ou pare o envio.")
            p = self._enqueue_locked(text, None, timeout)
        self._emit_line("tx", text)
        if not wait:
            return None
        if not p.event.wait(timeout):
            raise TimeoutError(f"Sem resposta do GRBL para '{text}'.")
        if p.aborted:
            raise GrblError(f"Comando '{text}' perdido (reset ou desconexão).")
        return CommandResult(ok=p.error is None, error=p.error, lines=list(p.responses))

    def _command_ok(self, cmd: str, timeout: float = 10.0) -> CommandResult:
        r = self.send_command(cmd, wait=True, timeout=timeout)
        assert r is not None
        if not r.ok:
            raise GrblError(f"'{cmd}': error:{r.error} — {r.message}", r.error)
        return r

    def _fail_pending_locked(self) -> None:
        while self._pending:
            p = self._pending.popleft()
            p.aborted = True
            p.event.set()
        self._rx_used = 0

    # ================================================================== streaming
    def stream_program(self, lines: Iterable[str] | str, mode: str = "char",
                       check: bool = False, start_line: int = 1, *, m6_pause: bool = False,
                       restore_state: bool = True, safe_z: float = 5.0) -> list[str]:
        """Inicia o envio de um programa em segundo plano.

        ``lines``: lista de linhas (ou o texto completo). Linhas vazias/só comentário
        são puladas mas contam na numeração (o ``line_no`` dos callbacks é 1-based e
        corresponde à linha do arquivo). ``mode``: "char" (contagem de caracteres,
        recomendado) ou "sr" (envia e espera ``ok``). ``check``: executa em modo
        verificação ``$C`` (nada se move) e sai do modo no fim. ``start_line``:
        começa a partir dessa linha (retomar trabalho interrompido); com ``restore_state``
        (padrão) as linhas anteriores são lidas para reconstruir o estado modal (G90/G91,
        unidades, G54.., F, spindle) e um preâmbulo seguro sobe para ``safe_z``, liga o
        spindle, posiciona XY e desce devagar antes de continuar (como o Candle).
        ``m6_pause``: troca cada ``M6`` (que o GRBL recusa com error:20) por M5 + M0 (pausa
        para trocar a ferramenta), religando o spindle depois. Devolve avisos (texto).

        Progresso: ``on_progress(sent, acked, total)`` em números de linha.
        Fim: ``on_stream_done(ok)`` quando todas as linhas foram confirmadas e o GRBL
        voltou a ``Idle`` (movimentos concluídos); ``ok=False`` se houve erro, alarme,
        parada ou desconexão.
        """
        if mode not in ("char", "sr"):
            raise ValueError("mode deve ser 'char' ou 'sr'")
        if isinstance(lines, str):
            lines = lines.splitlines()
        lines = list(lines)
        if not self.is_connected:
            raise GrblError("Não conectado.")
        self._banner_event.wait(3.0)        # logo após um reset, espera o GRBL reiniciar
        with self._lock:
            if self._streaming:
                raise RuntimeError("Já existe um programa em envio.")
        if check and self.state != "Check":
            self._command_ok("$C")
        start_line = max(1, int(start_line))
        warnings: list[str] = []
        if m6_pause or (start_line > 1 and restore_state):
            pp = safety.prepare_program(lines, start_line, m6_pause=m6_pause, safe_z_mm=safe_z)
            source: list[tuple[int, str]] = pp.items
            warnings = pp.warnings
        else:
            source = [(i, ln) for i, ln in enumerate(lines, start=1) if i >= start_line]
        prepared: list[tuple[int, str]] = []
        for i, ln in source:
            s = _clean_line(ln, self.strip_comments)
            if s:
                if len(s) + 1 > self._space_limit():
                    raise GrblError(f"Linha {i} tem {len(s)} caracteres; o GRBL aceita no máximo "
                                    f"{self._space_limit() - 1}.", 11)
                prepared.append((i, s))
        for w in warnings:
            self._emit_line("info", f"Aviso: {w}")
        with self._lock:
            self._stream_id += 1
            self._streaming = True
            self._stream_stop = False
            self._error_hold = False
            self._stream_total = len(lines)
            self._stream_sent = start_line - 1
            self._stream_acked = start_line - 1
            self._stream_ok = True
            self._stream_done_event.clear()
            sid = self._stream_id
            t = threading.Thread(target=self._stream_loop, args=(sid, prepared, mode, check),
                                 name="GrblStream", daemon=True)
            self._stream_thread = t
        t.start()
        return warnings

    def _stream_loop(self, sid: int, prepared: list[tuple[int, str]], mode: str, check: bool) -> None:
        sr = mode == "sr"
        try:
            for line_no, text in prepared:
                with self._lock:
                    while self._error_hold and not self._stream_stop and sid == self._stream_id:
                        self._cv.wait(0.5)
                    if self._stream_stop or sid != self._stream_id or not self._running:
                        raise _StreamAborted()
                    self._enqueue_locked(text, line_no, None, stream_id=sid, sr_mode=sr)
                    self._stream_sent = line_no
                    progress = ((self._stream_sent, self._stream_acked, self._stream_total)
                                if self._progress_due() else None)
                if self.echo_stream:
                    self._emit_line("tx", text)
                if progress is not None:
                    self._emit("on_progress", *progress)
            # aguarda todas as confirmações
            with self._lock:
                while self._running and sid == self._stream_id and not self._stream_stop:
                    if not any(p.stream_id == sid and p.line_no is not None for p in self._pending):
                        break
                    self._cv.wait(0.5)
                if self._stream_stop or sid != self._stream_id or not self._running:
                    raise _StreamAborted()
                self._stream_acked = self._stream_total
                self._stream_sent = self._stream_total
                progress = (self._stream_sent, self._stream_acked, self._stream_total)
            self._emit("on_progress", *progress)
            # todas as linhas confirmadas: aguarda o GRBL terminar os movimentos (Idle)
            t_acked = time.monotonic()
            with self._lock:
                while True:
                    if self._stream_stop or sid != self._stream_id or not self._running:
                        raise _StreamAborted()
                    if self._last_status_time > t_acked and self.state in ("Idle", "Check"):
                        break
                    self._cv.wait(0.1)
                ok = self._stream_ok
                self._streaming = False
                if check:
                    self._check_exit_pending = True
            if check:
                try:
                    self._command_ok("$C")
                except Exception as exc:  # pragma: no cover - depende do hardware
                    self._emit_line("info", f"Falha ao sair do modo verificação: {exc}")
            self._finish_stream(sid, ok)
        except (_StreamAborted, GrblError, TimeoutError):
            with self._lock:
                if sid == self._stream_id:
                    self._streaming = False
                    self._stream_ok = False
            self._finish_stream(sid, False)

    def _finish_stream(self, sid: int, ok: bool) -> None:
        with self._lock:
            if sid != self._stream_id or self._stream_done_event.is_set():
                return
            self._stream_done_event.set()
        self._emit("on_stream_done", ok)

    def _finish_stream_callbacks(self) -> None:
        if not self._stream_done_event.is_set():
            self._stream_done_event.set()
            self._emit("on_stream_done", False)

    def _abort_stream_locked(self, ok: bool) -> None:
        if self._streaming:
            self._stream_stop = True
            self._stream_ok = ok
            self._streaming = False
            for p in self._pending:
                if p.line_no is not None:
                    p.aborted = True
            self._cv.notify_all()

    def wait_stream(self, timeout: float | None = None) -> bool:
        """Bloqueia até o envio terminar. Retorna ``True`` se terminou no prazo."""
        return self._stream_done_event.wait(timeout)

    def wait_idle(self, timeout: float = 30.0) -> bool:
        """Espera o GRBL ficar parado (``Idle``) com o buffer vazio."""
        deadline = time.monotonic() + timeout
        seen_after = time.monotonic()
        while time.monotonic() < deadline:
            st = self.status
            if (st is not None and st.state == "Idle" and not self._pending
                    and self._last_status_time > seen_after):
                return True
            time.sleep(min(0.05, self.status_interval))
        return False

    # ---- controle do envio
    def pause(self) -> None:
        """Pausa (feed hold ``!``): desacelera e para mantendo a posição."""
        self.send_realtime(RT.FEED_HOLD)

    def resume(self) -> None:
        """Retoma (cycle start ``~``) e libera o envio retido por erro."""
        with self._lock:
            self._error_hold = False
            self._cv.notify_all()
        self.send_realtime(RT.CYCLE_START)

    def stop(self) -> None:
        """Parada: soft reset (ctrl-X) e descarta a fila. Se a máquina estava em
        movimento o GRBL entra em ALARM:3 (refaça zero/homing)."""
        with self._lock:
            self._abort_stream_locked(ok=False)
            self._fail_pending_locked()
            self._banner_event.clear()     # comandos aguardam o banner do reset
        self.send_realtime(RT.SOFT_RESET)
        self._finish_stream_callbacks()

    soft_reset = stop

    def safe_stop(self, timeout: float = 15.0) -> bool:
        """Parada controlada (como o "Abort" do Candle): para de enviar, aplica feed hold,
        espera a máquina desacelerar até parar (``Hold:0``) e só então faz o soft reset.

        Um reset com os eixos em movimento faz o GRBL perder passos e entrar em ALARM:3
        (é preciso refazer homing/zero). Parando antes, a posição é preservada e não há
        alarme. Em jog usa o cancelamento de jog (0x85). Se a máquina não parar em
        ``timeout`` s, faz o reset assim mesmo. Bloqueia: chame fora da thread da GUI.
        Retorna ``True`` se o reset foi feito com a máquina parada (posição preservada).
        """
        with self._lock:
            self._abort_stream_locked(ok=False)      # não envia mais nenhuma linha
            state = self.state
        base = state.split(":")[0]
        stopped = True
        if base == "Jog":
            self.send_realtime(RT.JOG_CANCEL)
            stopped = self._wait_state(("Idle",), timeout)
        elif base in ("Run", "Hold"):
            if base == "Run" or state == "Hold:1":
                self.send_realtime(RT.FEED_HOLD)
            stopped = self._wait_state(("Hold:0", "Idle", "Alarm"), timeout)
        elif base == "Home":
            stopped = False                          # homing não aceita hold: reset direto
        self.stop()
        if not stopped:
            self._emit_line("info", "A máquina não parou a tempo; reset feito em movimento (refaça o zero).")
        return stopped

    def _wait_state(self, states: tuple[str, ...], timeout: float) -> bool:
        """Espera um status NOVO cujo estado esteja em ``states``."""
        t0 = time.monotonic()
        deadline = t0 + timeout
        while time.monotonic() < deadline and self._running:
            if self._last_status_time > t0 and self.state in states:
                return True
            self._write_raw(RT.STATUS)
            time.sleep(min(0.05, self.status_interval))
        return False

    def feed_hold(self) -> None:
        self.send_realtime(RT.FEED_HOLD)

    def cycle_start(self) -> None:
        self.send_realtime(RT.CYCLE_START)

    def set_check_mode(self, enabled: bool, timeout: float = 5.0) -> None:
        """Liga/desliga o modo verificação ``$C`` (sair causa um reset no GRBL)."""
        in_check = self.state == "Check"
        if enabled != in_check:
            if not enabled:
                with self._lock:
                    self._check_exit_pending = True
            self._command_ok("$C", timeout)

    # ================================================================== tempo real
    def feed_override(self, cmd: str) -> None:
        """Override de avanço: "reset", "+10", "-10", "+1", "-1"."""
        self.send_realtime({"reset": RT.FEED_100, "+10": RT.FEED_PLUS_10, "-10": RT.FEED_MINUS_10,
                            "+1": RT.FEED_PLUS_1, "-1": RT.FEED_MINUS_1}[cmd])

    def rapid_override(self, percent: int) -> None:
        """Override de rápidos: 100, 50 ou 25 (%)."""
        self.send_realtime({100: RT.RAPID_100, 50: RT.RAPID_50, 25: RT.RAPID_25}[int(percent)])

    def spindle_override(self, cmd: str) -> None:
        """Override do spindle: "reset", "+10", "-10", "+1", "-1"."""
        self.send_realtime({"reset": RT.SPINDLE_100, "+10": RT.SPINDLE_PLUS_10,
                            "-10": RT.SPINDLE_MINUS_10, "+1": RT.SPINDLE_PLUS_1,
                            "-1": RT.SPINDLE_MINUS_1}[cmd])

    def toggle_spindle_stop(self) -> None:
        """Liga/desliga o spindle durante uma pausa (só funciona em Hold)."""
        self.send_realtime(RT.SPINDLE_STOP)

    def toggle_flood(self) -> None:
        self.send_realtime(RT.FLOOD_TOGGLE)

    def toggle_mist(self) -> None:
        self.send_realtime(RT.MIST_TOGGLE)

    def safety_door(self) -> None:
        """Simula a abertura da porta de segurança (pausa com recuo)."""
        self.send_realtime(RT.SAFETY_DOOR)

    def jog_cancel(self) -> None:
        """Cancela o jog em andamento imediatamente (0x85)."""
        self.send_realtime(RT.JOG_CANCEL)

    # ================================================================== comandos de alto nível
    def jog(self, dx: float = 0.0, dy: float = 0.0, dz: float = 0.0, feed: float = 1000.0,
            incremental: bool = True, wait: bool = False) -> CommandResult | None:
        """Movimento manual ``$J=``. Incremental (G91) por padrão; com
        ``incremental=False`` os valores são coordenadas de TRABALHO absolutas."""
        words = []
        for axis, v in (("X", dx), ("Y", dy), ("Z", dz)):
            if incremental and abs(v) < 1e-12:
                continue
            words.append(f"{axis}{v:.3f}")
        if not words:
            return None
        mode = "G91" if incremental else "G90"
        return self.send_command(f"$J={mode} G21 {' '.join(words)} F{feed:.0f}", wait=wait)

    def unlock(self) -> CommandResult:
        """``$X``: libera o alarme sem homing (confira a posição depois!)."""
        return self._command_ok("$X")

    def limit_pins(self) -> str:
        """Eixos cujos fins de curso o GRBL lê como ACIONADOS agora (campo ``Pn`` do status)."""
        st = self.status
        return "".join(a for a in "XYZABC" if st is not None and a in (st.pins or ""))

    def homing_preflight(self) -> list[str]:
        """Problemas que impedem um homing seguro (lista vazia = pode fazer ``$H``).

        O caso mais comum (e perigoso): com a máquina parada longe das chaves, o GRBL já lê
        fins de curso acionados — chaves NF (normalmente fechadas) com ``$5=0``. Nessa
        situação o homing "estala" sem sair do lugar e o limite físico nunca dispara.
        """
        problems: list[str] = []
        pins = self.limit_pins()
        if pins:
            problems.append(
                f"Com a máquina parada, o GRBL já lê os fins de curso {', '.join(pins)} como ACIONADOS. "
                "Se a ferramenta não está encostada nessas chaves, a lógica está invertida: chaves NF "
                "(normalmente fechadas) precisam de $5=1 (Inverter fins de curso). Com isso errado, o homing "
                "estala sem sair do lugar e o limite físico ($21) nunca para a máquina.")
        hom = self.settings.get(22) if self.settings else None
        if hom is not None and not hom:
            problems.append("O homing está desligado na placa ($22=0). Ligue-o (e confira as chaves) antes do $H.")
        return problems

    def home(self, timeout: float = 120.0, force: bool = False) -> CommandResult:
        """``$H``: ciclo de homing (exige $22=1).

        Antes de enviar, verifica :meth:`homing_preflight`; se houver problema, levanta
        ``GrblError`` com a explicação (use ``force=True`` só se souber o que está fazendo).
        """
        if not force:
            if self.status is None:
                self.wait_status(1.0)
            problems = self.homing_preflight()
            if problems:
                raise GrblError("Homing não iniciado por segurança: " + " ".join(problems), -2)
        self._limit_guard_until = time.monotonic() + timeout     # durante o homing as chaves são esperadas
        try:
            return self._command_ok("$H", timeout)
        finally:
            self._limit_guard_until = 0.0

    def wait_status(self, timeout: float = 1.0) -> bool:
        """Pede um status e espera ele chegar."""
        t0 = time.monotonic()
        last = self._last_status_time
        self._write_raw(RT.STATUS)
        while time.monotonic() - t0 < timeout:
            if self._last_status_time != last and self.status is not None:
                return True
            time.sleep(0.02)
        return self.status is not None

    def set_work_zero(self, axes: str = "XYZ") -> CommandResult:
        """Define a posição atual como zero de trabalho (G54) nos eixos dados."""
        ax = [a for a in "XYZ" if a in axes.upper()]
        if not ax:
            raise ValueError("Informe ao menos um eixo.")
        return self._command_ok("G10 L20 P1 " + " ".join(f"{a}0" for a in ax))

    def goto_work_zero(self, z_safe: float = 5.0, z_final: float | None = None) -> None:
        """Vai para X0 Y0 do trabalho: sobe para ``z_safe``, move XY e, se
        ``z_final`` for dado, desce até ele."""
        self._command_ok(f"G90 G0 Z{z_safe:.3f}")
        self._command_ok("G0 X0 Y0")
        if z_final is not None:
            self._command_ok(f"G0 Z{z_final:.3f}")

    # ------------------------------------------------------------------ posição e coordenadas
    def work_offset(self, fresh: bool = False) -> tuple[float, float, float]:
        """Deslocamento atual de trabalho (MPos − WPos).

        Sem ``fresh``, usa o WCO do último status (o GRBL só o reenvia de tempos em tempos,
        então pode estar desatualizado logo após um G10/G92); com ``fresh``, lê ``$#``.
        """
        if self.wco is not None and not fresh:
            return tuple(float(v) for v in self.wco[:3])  # type: ignore[return-value]
        params = self.read_parameters()
        g54 = params.get("G54", (0.0, 0.0, 0.0))
        g92 = params.get("G92", (0.0, 0.0, 0.0))
        tlo = params.get("TLO", (0.0,))[0]
        return (g54[0] + g92[0], g54[1] + g92[1], g54[2] + g92[2] + tlo)

    def set_position(self, x: float | None = None, y: float | None = None, z: float | None = None,
                     wcs: int = 1) -> CommandResult:
        """Declara que a posição atual vale ``x``/``y``/``z`` no sistema G54..G59 (``wcs`` 1..6).

        Equivale a "Zero XY" quando os valores são 0; com outros valores serve, por
        exemplo, para dizer "a ponta está 1 mm acima da placa" (Z=1).
        """
        words = [f"{k}{float(v):.4f}" for k, v in (("X", x), ("Y", y), ("Z", z)) if v is not None]
        if not words:
            raise ValueError("Informe ao menos um eixo.")
        return self._command_ok(f"G10 L20 P{int(wcs)} " + " ".join(words))

    def adjust_work_z(self, delta: float, wcs: int = 1) -> CommandResult:
        """Ajuste fino do zero Z sem sondar: ``delta`` > 0 faz o programa cortar MAIS FUNDO.

        Desloca o zero de trabalho para baixo em ``delta`` mm (a posição atual passa a valer
        ``Z_atual + delta``). Exige a máquina parada.
        """
        wz = None
        if self.status is not None and self.status.wpos is not None:
            wz = float(self.status.wpos[2])
        elif self.status is not None and self.status.mpos is not None:
            wz = float(self.status.mpos[2]) - self.work_offset()[2]
        if wz is None:
            raise GrblError("Posição atual desconhecida — aguarde o status da máquina.", -1)
        return self._command_ok(f"G10 L20 P{int(wcs)} Z{wz + float(delta):.4f}")

    def goto(self, x: float | None = None, y: float | None = None, z: float | None = None,
             safe_z: float | None = 5.0, feed: float | None = None) -> None:
        """Vai para coordenadas de trabalho. Com ``safe_z``, sobe antes de mover XY."""
        if safe_z is not None and (x is not None or y is not None):
            self._command_ok(f"G90 G0 Z{float(safe_z):.4f}")
        xy = " ".join(f"{k}{float(v):.4f}" for k, v in (("X", x), ("Y", y)) if v is not None)
        mode = f"G1 F{float(feed):.1f}" if feed else "G0"
        if xy:
            self._command_ok(f"G90 {mode} {xy}")
        if z is not None:
            self._command_ok(f"G90 {mode} Z{float(z):.4f}")

    def select_wcs(self, n: int) -> CommandResult:
        """Ativa o sistema de coordenadas de trabalho G54 (1) … G59 (6)."""
        if not 1 <= int(n) <= 6:
            raise ValueError("Sistema de coordenadas deve ser 1 (G54) a 6 (G59).")
        return self._command_ok(f"G{53 + int(n)}")

    def spindle(self, on: bool, rpm: float = 10000.0, ccw: bool = False) -> CommandResult:
        """Liga (M3/M4 S…) ou desliga (M5) o spindle/laser."""
        if not on:
            return self._command_ok("M5")
        return self._command_ok(f"{'M4' if ccw else 'M3'} S{float(rpm):g}")

    def coolant(self, flood: bool | None = None, mist: bool | None = None) -> CommandResult:
        """M8 (refrigeração/ar), M7 (névoa) ou M9 (desliga tudo)."""
        if flood is False or mist is False:
            return self._command_ok("M9")
        return self._command_ok("M7" if mist else "M8")

    def run_lines(self, lines: Iterable[str] | str, timeout: float = 60.0) -> list[CommandResult]:
        """Executa uma macro: cada linha é enviada e confirmada antes da próxima."""
        if isinstance(lines, str):
            lines = lines.splitlines()
        out = []
        for ln in lines:
            code = re.sub(r"\([^)]*\)|;.*$", "", ln).strip()
            if code:
                out.append(self._command_ok(code, timeout))
        return out

    # ------------------------------------------------------------------ sonda
    def probe_point(self, depth: float = 5.0, feed: float = 50.0, timeout: float | None = None,
                    offset_z: float | None = None) -> float:
        """Sonda para baixo (G38.2) a partir da altura atual; devolve o Z de TRABALHO do contato.

        Não altera nenhum zero. Levanta ``GrblError`` se não houver contato.
        """
        if timeout is None:
            timeout = abs(depth) / max(feed, 1e-3) * 60.0 + 15.0
        self.last_probe = None
        r = self.send_command(f"G91 G38.2 Z-{abs(depth):.3f} F{feed:.1f}", timeout=timeout)
        assert r is not None
        self.send_command("G90", timeout=5.0)
        if not r.ok:
            raise GrblError(f"Sondagem recusada: error:{r.error} — {r.message}", r.error)
        probe = self.last_probe
        if probe is None or not probe[3] or self.state == "Alarm":
            raise GrblError("A sonda não tocou (ALARM:5). Verifique o fio da sonda e a profundidade máxima; "
                            "desbloqueie ($X) e tente novamente.", 5)
        oz = offset_z if offset_z is not None else self.work_offset(fresh=True)[2]
        return float(probe[2]) - oz

    def probe_grid(self, points: Iterable[tuple[float, float]], clearance: float = 2.0, depth: float = 4.0,
                   feed: float = 50.0, rapid_feed: float | None = None,
                   progress: Any = None, cancel: Any = None) -> list[float]:
        """Sonda cada ponto (x, y) de trabalho e devolve as alturas Z de trabalho do contato.

        Entre pontos sobe para ``clearance`` (Z de trabalho, acima da placa). ``depth`` é a
        descida máxima a partir de ``clearance``. ``progress(i, n, z)`` é chamado a cada ponto;
        se ``cancel()`` for verdadeiro, para, sobe e levanta ``GrblError``.
        """
        pts = list(points)
        out: list[float] = []
        oz = self.work_offset(fresh=True)[2]
        self._command_ok(f"G90 G0 Z{clearance:.3f}")
        for i, (x, y) in enumerate(pts):
            if cancel is not None and cancel():
                self._command_ok(f"G90 G0 Z{clearance:.3f}")
                raise GrblError("Sondagem cancelada pelo usuário.", -1)
            move = f"G90 G1 F{rapid_feed:.0f}" if rapid_feed else "G90 G0"
            self._command_ok(f"{move} X{x:.4f} Y{y:.4f}")
            z = self.probe_point(depth=clearance + depth, feed=feed, offset_z=oz)
            out.append(z)
            self._command_ok(f"G90 G0 Z{clearance:.3f}")
            if progress is not None:
                progress(i + 1, len(pts), z)
        return out

    def probe_z(self, depth: float = 10.0, feed: float = 50.0, plate_thickness: float = 0.0,
                retract: float = 2.0, timeout: float | None = None) -> float:
        """Sonda Z com G38.2 descendo até ``depth`` mm e define Z de trabalho como
        ``plate_thickness`` no ponto de contato (``G10 L20 P1 Z<espessura>``).

        Retorna o Z de máquina do contato. Levanta ``GrblError`` se a sonda não tocar
        (ALARM:5) ou o comando for recusado.
        """
        if timeout is None:
            timeout = abs(depth) / max(feed, 1e-3) * 60.0 + 15.0
        self.last_probe = None
        r = self.send_command(f"G91 G38.2 Z-{abs(depth):.3f} F{feed:.1f}", timeout=timeout)
        assert r is not None
        if not r.ok:
            self.send_command("G90", timeout=5.0)
            raise GrblError(f"Sondagem recusada: error:{r.error} — {r.message}", r.error)
        probe = self.last_probe
        if probe is None or not probe[3] or self.state == "Alarm":
            raise GrblError("A sonda não tocou na placa (ALARM:5). Desbloqueie ($X) e tente novamente.", 5)
        self._command_ok(f"G90 G10 L20 P1 Z{plate_thickness:.3f}")
        if retract > 0:
            self._command_ok(f"G91 G0 Z{retract:.3f}")
        self._command_ok("G90")
        return probe[2]

    def read_settings(self, timeout: float = 5.0) -> dict[int, float]:
        """Lê ``$$`` e devolve ``{número: valor}`` (também guarda em ``self.settings``)."""
        r = self._command_ok("$$", timeout)
        out: dict[int, float] = {}
        for ln in r.lines:
            m = re.match(r"\$(\d+)=([-+]?[\d.]+)", ln)
            if m:
                out[int(m.group(1))] = float(m.group(2))
        self.settings = out
        return out

    def write_setting(self, number: int, value: float, timeout: float = 5.0) -> CommandResult:
        """Grava ``$n=valor`` na EEPROM do GRBL."""
        v = int(value) if float(value).is_integer() else value
        r = self._command_ok(f"${int(number)}={v}", timeout)
        self.settings[int(number)] = float(value)
        return r

    def read_parameters(self, timeout: float = 5.0) -> dict[str, tuple[float, ...]]:
        """Lê ``$#``: ``{"G54": (x,y,z), ..., "TLO": (v,), "PRB": (x,y,z,ok)}``."""
        r = self._command_ok("$#", timeout)
        out: dict[str, tuple[float, ...]] = {}
        for ln in r.lines:
            m = re.match(r"\[(\w+):([^\]]*)\]", ln)
            if not m:
                continue
            key, val = m.group(1), m.group(2)
            if key == "PRB":
                coords, _, flag = val.rpartition(":")
                out[key] = _vec(coords) + (float(flag),)
            else:
                out[key] = _vec(val)
        return out

    def read_parser_state(self, timeout: float = 5.0) -> list[str]:
        """Lê ``$G`` e devolve as palavras modais (ex.: ['G0','G54','G17','G21',...])."""
        r = self._command_ok("$G", timeout)
        for ln in r.lines:
            if ln.startswith("[GC:"):
                return ln[4:].rstrip("]").split()
        return []

    def read_build_info(self, timeout: float = 5.0) -> list[str]:
        """Lê ``$I`` (versão e opções de compilação) e atualiza ``self.firmware``."""
        lines = self._command_ok("$I", timeout).lines
        self.build_info_lines = list(lines)
        self.firmware = fwmod.detect_firmware(self.banner, lines)
        return lines

    # ---------------------------------------------------------------- firmware / Grbl_Esp32
    def detect_firmware(self, timeout: float = 5.0) -> FirmwareInfo:
        """Identifica o firmware (banner + ``$I``): GRBL clássico, Grbl_Esp32, MKS DLC32...

        Se o ``$I`` falhar (ex.: máquina em movimento), devolve a detecção feita só
        pelo banner.
        """
        try:
            self.read_build_info(timeout)
        except (GrblError, TimeoutError) as exc:
            log.info("$I indisponível (%s); usando só o banner", exc)
            self.firmware = fwmod.detect_firmware(self.banner, self.build_info_lines)
        return self.firmware

    def read_extended_settings(self, timeout: float = 8.0) -> dict[str, str]:
        """Lê todas as configurações nomeadas (``$S``; Grbl_Esp32/DLC32).

        Devolve ``{"Sta/SSID": "MKS_DLC", "X/StepsPerMm": "80.000", ...}`` com os
        valores como texto (senhas aparecem como ``******``). Se o firmware não
        suportar ``$S`` (GRBL clássico responde ``error:3``) levanta ``GrblError``.
        """
        r = self.send_command("$S", wait=True, timeout=timeout)
        assert r is not None
        if not r.ok:
            raise GrblError(f"'$S': error:{r.error} — {r.message} (o firmware não tem "
                            f"configurações estendidas)", r.error)
        out = fwmod.parse_extended_settings(r.lines)
        self.extended_settings = out
        return out

    def write_extended_setting(self, name: str, value: str, timeout: float = 5.0) -> CommandResult:
        """Grava uma configuração nomeada: ``$Sta/SSID=MinhaRede`` (Grbl_Esp32/DLC32).

        O valor é enviado como texto (espaços internos preservados). Caracteres de
        tempo real (``? ! ~``) e quebras de linha são recusados com ``ValueError``,
        pois o firmware os interceptaria. Mudanças de WiFi só valem após reiniciar a
        placa (:meth:`restart_board`).
        """
        key = str(name).strip().lstrip("$")
        if not re.fullmatch(r"[A-Za-z][A-Za-z0-9_/.]*", key):
            raise ValueError(f"Nome de configuração inválido: {name!r}")
        text = "" if value is None else str(value)
        if any(c in text for c in "?!~\r\n"):
            raise ValueError("O valor contém caracteres de tempo real do GRBL (? ! ~) ou quebra de linha.")
        if any(ord(c) > 126 for c in text):
            raise ValueError("Use apenas caracteres ASCII no valor.")
        r = self._command_ok(f"${key}={text}", timeout)
        self.extended_settings[key] = text
        return r

    def restart_board(self) -> bool:
        """Reinicia a placa ESP32 (``$System/Control=RESTART``; FluidNC: ``$Bye``).

        Necessário para aplicar mudanças de WiFi. Pela conexão TCP a ligação cai; pela
        USB o ESP32 reinicia e envia um novo banner. Retorna ``False`` se o firmware
        detectado não tem comando de reinício.
        """
        cmd = self.firmware.restart_command
        if not cmd:
            return False
        self.send_command(cmd, wait=False)
        return True

    def list_wifi_networks(self, timeout: float = 15.0) -> list[dict]:
        """Procura redes WiFi visíveis pela placa (``$WiFi/ListAPs``; Grbl_Esp32/DLC32)."""
        r = self._command_ok("$WiFi/ListAPs", timeout)
        return fwmod.parse_ap_list(r.lines)

    # ================================================================== callbacks
    def _emit(self, name: str, *args: Any) -> None:
        cb = getattr(self, name, None)
        if cb is None:
            return
        try:
            cb(*args)
        except Exception:
            log.exception("Erro no callback %s", name)

    def _emit_line(self, direction: str, text: str) -> None:
        self._emit("on_line", direction, text)


class _StreamAborted(Exception):
    pass

