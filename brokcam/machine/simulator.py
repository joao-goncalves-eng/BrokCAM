"""GRBL 1.1 virtual: uma máquina simulada que fala o mesmo protocolo serial.

``GrblSimulator`` implementa o protocolo ``Transport`` e pode ser usado no lugar da
porta serial (``GrblController.connect("SIMULADOR")``). Ele reproduz o comportamento
relevante do GRBL 1.1h:

* buffer serial RX de 128 bytes (bytes excedentes são PERDIDOS, como no hardware —
  ``overflow_count`` registra isso) e buffer do planejador de 15 blocos;
* respostas ``ok``/``error:N``, ``ALARM:N``, ``[MSG:...]``, relatórios de status
  ``<...>`` com MPos/WPos, FS, WCO, Ov, Bf conforme ``$10``;
* comandos em tempo real (``?``, ``!``, ``~``, ctrl-X, 0x84, 0x85, 0x90–0xA1);
* movimento simulado com a velocidade de avanço (pode ser acelerado por ``speed``),
  pausa M0, G4, sonda G38.x, homing, jog, modo verificação ``$C``, ``$$``, ``$#``,
  ``$G``, ``$I``, ``$X``, ``$H``, limites de software, alarmes.

Com ``firmware="mks_dlc32"`` (ou ``"grbl_esp32"``) ele imita uma placa ESP32 com
Grbl_Esp32 — banner ``Grbl 1.1``/``Grbl 1.3a``, ``$I`` com ``[MSG:Using machine:...]`` e
a mensagem de WiFi, configurações estendidas ``$S``/``$+``/``$SC``, gravação
``$Nome=valor``, ``$WiFi/ListAPs``, ``$RST=@`` e ``$System/Control=RESTART`` — para
testar a aba de configurações sem hardware.

Não é uma emulação ciclo a ciclo: aceleração e arcos são aproximados.
"""
from __future__ import annotations

import math
import re
import threading
import time
from collections import deque
from dataclasses import dataclass, field
from typing import Any

__all__ = ["GrblSimulator", "DEFAULT_SETTINGS", "DLC32_SETTINGS", "ESP32_EXTENDED"]

DEFAULT_SETTINGS: dict[int, float] = {
    0: 10, 1: 25, 2: 0, 3: 0, 4: 0, 5: 0, 6: 0, 10: 1, 11: 0.010, 12: 0.002, 13: 0,
    20: 0, 21: 0, 22: 0, 23: 0, 24: 25.0, 25: 500.0, 26: 250, 27: 1.0,
    30: 1000, 31: 0, 32: 0,
    100: 800.0, 101: 800.0, 102: 800.0,
    110: 1000.0, 111: 1000.0, 112: 600.0,
    120: 30.0, 121: 30.0, 122: 30.0,
    130: 300.0, 131: 180.0, 132: 45.0,
}

# Padrões de fábrica da MKS DLC32 (Machines/i2s_out_xyz_mks_dlc32.h). $34–$36 só
# aparecem no $+ (são configurações "estendidas" com número).
DLC32_SETTINGS: dict[int, float] = {
    0: 10, 1: 25, 2: 0, 3: 1, 4: 0, 5: 1, 6: 1, 10: 1, 11: 0.010, 12: 0.002, 13: 0,
    20: 0, 21: 0, 22: 0, 23: 1, 24: 300.0, 25: 1000.0, 26: 250.0, 27: 1.0, 28: 1000.0,
    30: 1000.0, 31: 0.0, 32: 0, 34: 0.0, 35: 0.0, 36: 100.0, 38: 1, 40: 1,
    100: 80.0, 101: 80.0, 102: 80.0,
    110: 6000.0, 111: 6000.0, 112: 6000.0,
    120: 500.0, 121: 500.0, 122: 500.0,
    130: 450.0, 131: 450.0, 132: 50.0,
}
_ESP32_EXTENDED_NUMBERED = {34, 35, 36}

# Configurações nomeadas (não numeradas) do Grbl_Esp32/DLC32 e seus padrões, no
# formato de texto do $S. Senhas ficam guardadas mas são mostradas como ******.
ESP32_EXTENDED: dict[str, str] = {
    "Radio/Mode": "AP", "Sta/SSID": "MKS_DLC", "Sta/Password": "12345678", "Sta/IPMode": "DHCP",
    "Sta/IP": "0.0.0.0", "Sta/Gateway": "0.0.0.0", "Sta/Netmask": "0.0.0.0",
    "AP/SSID": "MKS_DLC", "AP/Password": "12345678", "AP/IP": "192.168.4.1", "AP/Channel": "1",
    "System/Hostname": "grblesp", "Telnet/Enable": "ON", "Telnet/Port": "23",
    "Http/Enable": "ON", "Http/Port": "80",
    "Spindle/Type": "PWM", "Spindle/PWM/Invert": "Off", "Spindle/Enable/Invert": "Off",
    "Spindle/Enable/OffWithSpeed": "Off", "Spindle/Delay/SpinUp": "0.000",
    "Spindle/Delay/SpinDown": "0.000", "Coolant/Delay/TurnOn": "1.000", "Laser/FullPower": "1000",
    "Homing/Cycle0": "XY", "Homing/Cycle1": "", "Homing/Cycle2": "", "Homing/Cycle3": "",
    "Homing/Cycle4": "", "Homing/Cycle5": "", "Homing/Squared": "",
    "X/Home/Mpos": "0.000", "Y/Home/Mpos": "0.000", "Z/Home/Mpos": "0.000",
    "Stepper/Direction/Delay": "0", "Stepper/Enable/Delay": "0",
    "GCode/Line0": "", "GCode/Line1": "", "Errors/Verbose": "Off", "Message/Level": "Info",
    "Firmware/Build": "", "User/Macro0": "", "User/Macro1": "", "User/Macro2": "", "User/Macro3": "",
}
_PASSWORDS = {"sta/password", "ap/password"}
_ESP32_INT = {"ap/channel", "telnet/port", "http/port", "laser/fullpower",
              "stepper/direction/delay", "stepper/enable/delay"}

_INT_SETTINGS = {0, 1, 2, 3, 4, 5, 6, 10, 13, 20, 21, 22, 23, 26, 32, 38, 40}
_WORD = re.compile(r"([A-Z])([-+]?(?:\d+\.?\d*|\.\d+)?)")
_COMMENT = re.compile(r"\([^)]*\)|;.*$")
_MSG = re.compile(r"\(\s*MSG\s*,([^)]*)\)", re.IGNORECASE)
_LINE_BUFFER = 80
_SUPPORTED_G = {0, 1, 2, 3, 4, 10, 17, 18, 19, 20, 21, 28, 30, 28.1, 30.1, 38.2, 38.3, 38.4, 38.5,
                40, 43.1, 49, 53, 54, 55, 56, 57, 58, 59, 61, 80, 90, 91, 91.1, 92, 92.1, 93, 94}
_SUPPORTED_M = {0, 1, 2, 30, 3, 4, 5, 7, 8, 9, 56}
_MOTION_G = {0, 1, 2, 3, 38.2, 38.3, 38.4, 38.5, 80}


@dataclass
class _Block:
    kind: str                         # "move" | "dwell" | "home" | "probe"
    target: tuple[float, float, float] = (0.0, 0.0, 0.0)
    rate: float = 0.0                 # mm/min (0 = rápido)
    rapid: bool = False
    jog: bool = False
    seconds: float = 0.0
    defer_ok: bool = False            # "ok" só ao terminar (G4, $H, G38.x)
    probe_mode: float = 38.2
    extra: dict = field(default_factory=dict)


def setting_value_number(kind: str, value: str) -> str:
    from brokcam.machine.firmware import to_number
    try:
        v = to_number(value, kind)
    except ValueError:
        return value
    return f"{v:g}"


class GrblSimulator:
    """Máquina GRBL 1.1 virtual compatível com ``Transport``.

    Parâmetros:
        speed: fator de aceleração do tempo (1 = tempo real; ``math.inf`` = instantâneo).
        homing_enabled: liga ``$22=1`` (inicia em alarme pedindo $H/$X).
        probe_surface_z: Z de MÁQUINA onde a sonda encosta na placa (G38.x).
        settings: sobrescreve configurações ``$n``.
    """

    RX_BUFFER_SIZE = 128
    PLANNER_SIZE = 15
    VERSION = "1.1h"

    def __init__(self, speed: float = 1.0, homing_enabled: bool = False,
                 probe_surface_z: float = -20.0, settings: dict[int, float] | None = None,
                 tick: float = 0.002, firmware: str = "grbl",
                 extended: dict[str, str] | None = None) -> None:
        self.speed = float(speed)
        self.tick = tick
        self.probe_surface_z = probe_surface_z
        # superfície opcional em função de (x, y) de máquina — simula placa empenada (heightmap)
        self.probe_surface: Any = None
        # fins de curso lidos como acionados (ex.: "XYZ" simula chaves NF com $5=0); função opcional
        # pins(mpos) -> str permite acionar a chave quando o eixo passa de um ponto
        self.limit_pins: str = ""
        self.limit_pins_fn: Any = None
        if firmware not in ("grbl", "grbl_esp32", "mks_dlc32"):
            raise ValueError("firmware deve ser 'grbl', 'grbl_esp32' ou 'mks_dlc32'")
        self.firmware = firmware
        self.esp32 = firmware != "grbl"
        if self.esp32:
            base = dict(DLC32_SETTINGS)
            if firmware == "grbl_esp32":
                base.pop(38)
                base.pop(40)
        else:
            base = dict(DEFAULT_SETTINGS)
        self._defaults = dict(base)
        self.line_buffer = 256 if self.esp32 else _LINE_BUFFER
        self.ext: dict[str, str] = dict(ESP32_EXTENDED) if self.esp32 else {}
        if extended:
            self.ext.update(extended)
        self.restarts = 0
        self.settings: dict[int, float] = dict(base)
        if homing_enabled:
            self.settings[22] = 1
        if settings:
            self.settings.update(settings)

        self._lock = threading.RLock()
        self._out_cv = threading.Condition(self._lock)
        self._wake = threading.Event()
        self._rx = bytearray()
        self._out = bytearray()
        self._planner: deque[_Block] = deque()
        self._closed = False
        self._disconnected = False

        # estatísticas para testes
        self.overflow_count = 0
        self.max_rx_used = 0
        self.executed_lines: list[str] = []
        self.errors: list[tuple[str, int]] = []

        with self._lock:
            self._init_state(cold=True)
            self._emit_banner()
        self._thread = threading.Thread(target=self._run, name="GrblSimulator", daemon=True)
        self._thread.start()

    # ================================================================== Transport
    def write(self, data: bytes) -> None:
        with self._lock:
            if self._disconnected or self._closed:
                raise OSError("simulador desconectado")
            for b in data:
                if b == 0x3F:            # '?'
                    self._status_report()
                elif b == 0x21:          # '!'
                    self._feed_hold()
                elif b == 0x7E:          # '~'
                    self._cycle_start()
                elif b == 0x18:          # ctrl-X
                    self._soft_reset()
                elif b >= 0x80:
                    self._realtime_ext(b)
                else:
                    if len(self._rx) >= self.RX_BUFFER_SIZE:
                        self.overflow_count += 1      # GRBL real perderia o byte
                        continue
                    self._rx.append(b)
                    if len(self._rx) > self.max_rx_used:
                        self.max_rx_used = len(self._rx)
        self._wake.set()

    def read(self) -> bytes:
        with self._out_cv:
            if self._disconnected:
                raise OSError("simulador desconectado")
            if not self._out and not self._closed:
                self._out_cv.wait(0.03)
            if self._disconnected:
                raise OSError("simulador desconectado")
            data = bytes(self._out)
            self._out.clear()
            return data

    def close(self) -> None:
        with self._lock:
            self._closed = True
            self._out_cv.notify_all()
        self._wake.set()

    @property
    def in_waiting(self) -> int:
        with self._lock:
            if self._disconnected:
                raise OSError("simulador desconectado")
            return len(self._out)

    # ================================================================== testes/UI
    def simulate_disconnect(self) -> None:
        """Simula o cabo USB sendo removido (leituras/escritas passam a falhar)."""
        with self._lock:
            self._disconnected = True
            self._out_cv.notify_all()

    def trigger_alarm(self, code: int = 1) -> None:
        """Dispara um alarme (ex.: 1 = fim de curso físico) como o hardware faria."""
        with self._lock:
            self._planner.clear()
            self._rx.clear()
            self._defer = None
            self._state = "Alarm"
            self._out_line(f"ALARM:{code}")
            if code in (1, 2):
                # alarmes críticos: o GRBL ignora tudo até um reset (ctrl-X)
                self._critical = True
                self._out_line("[MSG:Reset to continue]")

    @property
    def mpos(self) -> tuple[float, float, float]:
        with self._lock:
            return tuple(self._mpos)  # type: ignore[return-value]

    @property
    def state(self) -> str:
        with self._lock:
            return self._state

    # ================================================================== estado
    def _init_state(self, cold: bool) -> None:
        if cold:
            self._mpos = [0.0, 0.0, 0.0]
            self._wcs = {i: [0.0, 0.0, 0.0] for i in range(54, 60)}
            self._g28 = [0.0, 0.0, 0.0]
            self._g30 = [0.0, 0.0, 0.0]
            self._g92 = [0.0, 0.0, 0.0]
            self._prb = [0.0, 0.0, 0.0]
            self._prb_ok = 0
            self._state = "Alarm" if self.settings.get(22) else "Idle"
        self._ppos = list(self._mpos)         # posição do parser (fim do último bloco)
        self._motion = 0
        self._absolute = True
        self._scale = 1.0
        self._feed = 0.0
        self._spindle = "M5"
        self._sval = 0.0
        self._tool = 0
        self._coolant = "M9"
        self._wcs_sel = 54
        self._plane = 17
        self._check = False
        self._m0_pause = False
        self._critical = False
        self._defer: _Block | None = None
        self._ov = [100, 100, 100]
        self._status_count = 0
        self._last_wco: tuple | None = None

    @property
    def version(self) -> str:
        return {"mks_dlc32": "1.1", "grbl_esp32": "1.3a"}.get(self.firmware, self.VERSION)

    def _emit_banner(self) -> None:
        self._out_line("")
        self._out_line(f"Grbl {self.version} ['$' for help]")
        if self.settings.get(22) and self._state == "Alarm":
            self._out_line("[MSG:'$H'|'$X' to unlock]")

    def _out_line(self, text: str) -> None:
        self._out += text.encode("ascii", "replace") + b"\r\n"
        self._out_cv.notify_all()

    def _wco(self) -> list[float]:
        w = self._wcs[self._wcs_sel]
        return [w[i] + self._g92[i] for i in range(3)]

    # ================================================================== tempo real
    def _status_report(self) -> None:
        mp = self._mpos
        wco = self._wco()
        mask = int(self.settings.get(10, 1))
        parts = [self._state]
        if mask & 1:
            parts.append("MPos:%.3f,%.3f,%.3f" % tuple(mp))
        else:
            parts.append("WPos:%.3f,%.3f,%.3f" % tuple(mp[i] - wco[i] for i in range(3)))
        if mask & 2:
            parts.append(f"Bf:{self.PLANNER_SIZE - len(self._planner)},{self.RX_BUFFER_SIZE - len(self._rx)}")
        running = self._state in ("Run", "Jog", "Home") and self._planner
        rate = 0.0
        if running:
            b = self._planner[0]
            rate = self._block_rate(b) if b.kind in ("move", "probe") else 0.0
        sp = self._sval * self._ov[2] / 100.0 if self._spindle != "M5" else 0.0
        parts.append(f"FS:{rate:.0f},{sp:.0f}")
        pins = self.limit_pins + (self.limit_pins_fn(tuple(mp)) if self.limit_pins_fn else "")
        if pins:
            parts.append("Pn:" + "".join(sorted(set(pins), key="XYZPDHRS".find)))
        self._status_count += 1
        wco_t = tuple(round(v, 3) for v in wco)
        if wco_t != self._last_wco or self._status_count % 10 == 0:
            parts.append("WCO:%.3f,%.3f,%.3f" % wco_t)
            self._last_wco = wco_t
        elif self._status_count % 10 == 1:
            parts.append(f"Ov:{self._ov[0]},{self._ov[1]},{self._ov[2]}")
        self._out_line("<" + "|".join(parts) + ">")

    def _feed_hold(self) -> None:
        st = self._state
        if st == "Jog":
            self._planner = deque(b for b in self._planner if not b.jog)
            self._ppos = list(self._mpos)
            self._state = "Idle"
        elif st == "Run":
            self._state = "Hold:1"
        elif st == "Idle":
            self._state = "Hold:0"

    def _cycle_start(self) -> None:
        if self._state.startswith("Hold") or self._state.startswith("Door"):
            self._m0_pause = False
            self._state = "Run" if self._planner else "Idle"
        elif self._state == "Idle" and self._planner:
            self._state = "Run"

    def _soft_reset(self) -> None:
        moving = self._state in ("Run", "Jog", "Home", "Hold:1") and bool(self._planner)
        self._planner.clear()
        self._rx.clear()
        was_alarm = self._state == "Alarm"
        self._init_state(cold=False)
        if moving:
            self._out_line("ALARM:3")
        self._state = "Alarm" if (moving or was_alarm) else "Idle"
        self._emit_banner()

    def _realtime_ext(self, b: int) -> None:
        ov = self._ov
        if b == 0x84:                                  # porta de segurança
            if self._state in ("Run", "Idle", "Hold:0", "Hold:1"):
                self._state = "Door:0"
        elif b == 0x85:                                # cancelar jog
            if self._state == "Jog":
                self._planner = deque(x for x in self._planner if not x.jog)
                self._ppos = list(self._mpos)
                self._state = "Idle"
        elif b == 0x90:
            ov[0] = 100
        elif b == 0x91:
            ov[0] = min(200, ov[0] + 10)
        elif b == 0x92:
            ov[0] = max(10, ov[0] - 10)
        elif b == 0x93:
            ov[0] = min(200, ov[0] + 1)
        elif b == 0x94:
            ov[0] = max(10, ov[0] - 1)
        elif b == 0x95:
            ov[1] = 100
        elif b == 0x96:
            ov[1] = 50
        elif b == 0x97:
            ov[1] = 25
        elif b == 0x99:
            ov[2] = 100
        elif b == 0x9A:
            ov[2] = min(200, ov[2] + 10)
        elif b == 0x9B:
            ov[2] = max(10, ov[2] - 10)
        elif b == 0x9C:
            ov[2] = min(200, ov[2] + 1)
        elif b == 0x9D:
            ov[2] = max(10, ov[2] - 1)
        # 0x9E (parar spindle), 0xA0/0xA1 (refrigeração): aceitos sem efeito visível

    # ================================================================== laço principal
    def _run(self) -> None:
        last = time.perf_counter()
        while True:
            with self._lock:
                if self._closed:
                    return
                now = time.perf_counter()
                dt = now - last
                last = now
                if not self._disconnected:
                    self._advance(dt)
                    self._parse_lines()
                    self._advance(0.0)
                busy = bool(self._planner) or bool(self._rx)
            self._wake.wait(self.tick if busy else 0.02)
            self._wake.clear()

    def _block_rate(self, b: _Block) -> float:
        if b.rapid:
            rmax = min(self.settings[110], self.settings[111], self.settings[112])
            return rmax * self._ov[1] / 100.0
        r = b.rate * (1.0 if b.jog else self._ov[0] / 100.0)
        return min(r, max(self.settings[110], self.settings[111]))

    def _advance(self, dt: float) -> None:
        if self._state == "Hold:1":
            self._state = "Hold:0"
            return
        if self._state not in ("Run", "Jog", "Home", "Idle", "Check"):
            return
        if self._state == "Idle" and self._planner:
            b0 = self._planner[0]
            self._state = "Jog" if b0.jog else ("Home" if b0.kind == "home" else "Run")
        budget = math.inf if math.isinf(self.speed) else dt * self.speed
        while self._planner and budget > 0:
            b = self._planner[0]
            self._state = "Jog" if b.jog else ("Home" if b.kind == "home" else "Run")
            if b.kind == "dwell":
                if budget >= b.seconds:
                    budget -= b.seconds
                    self._finish_block(b)
                else:
                    b.seconds -= budget
                    budget = 0
                continue
            if b.kind == "home":
                if budget >= b.seconds:
                    budget -= b.seconds
                    self._mpos = [0.0, 0.0, 0.0]
                    self._ppos = [0.0, 0.0, 0.0]
                    self._finish_block(b)
                else:
                    b.seconds -= budget
                    budget = 0
                continue
            # move / probe
            tgt = b.target
            pos = self._mpos
            if b.kind == "probe":
                # contato quando cruza a superfície em Z (G38.2/38.3 em direção a ela)
                sz = (float(self.probe_surface(pos[0], pos[1])) if self.probe_surface is not None
                      else self.probe_surface_z)
                if pos[2] >= sz > tgt[2] or pos[2] <= sz < tgt[2]:
                    frac = (pos[2] - sz) / (pos[2] - tgt[2])
                    tgt = (pos[0] + (tgt[0] - pos[0]) * frac, pos[1] + (tgt[1] - pos[1]) * frac, sz)
                    b.extra["contact"] = True
            d = math.dist(pos, tgt)
            v = self._block_rate(b) / 60.0
            need = d / v if v > 0 else 0.0
            if budget >= need:
                budget -= need
                self._mpos = list(tgt)
                self._finish_block(b)
            else:
                f = budget * v / d
                self._mpos = [pos[i] + (tgt[i] - pos[i]) * f for i in range(3)]
                budget = 0
        if not self._planner and self._state in ("Run", "Jog", "Home"):
            self._state = "Idle"

    def _finish_block(self, b: _Block) -> None:
        self._planner.popleft()
        if b.kind == "probe":
            contact = bool(b.extra.get("contact"))
            self._prb = list(self._mpos)
            self._prb_ok = 1 if contact else 0
            self._ppos = list(self._mpos)
            if not contact and b.probe_mode in (38.2, 38.4):
                self._planner.clear()
                self._out_line("ALARM:5")
                self._state = "Alarm"
            self._out_line("[PRB:%.3f,%.3f,%.3f:%d]" % (*self._prb, self._prb_ok))
        if b is self._defer:
            self._defer = None
            self._out_line("ok")

    # ================================================================== parser
    def _parse_lines(self) -> None:
        while True:
            if self._defer is not None or self._m0_pause or self._critical:
                return
            if self._state == "Sleep":
                return
            nl = -1
            for i, c in enumerate(self._rx):
                if c == 10 or c == 13:
                    nl = i
                    break
            if nl < 0:
                return
            raw = bytes(self._rx[:nl]).decode("ascii", "replace")
            stripped = _COMMENT.sub("", raw).replace(" ", "").replace("\t", "").upper()
            # sincronização: alguns comandos esperam o planejador esvaziar
            if self._needs_sync(stripped) and self._planner and not self._check:
                return
            if stripped and not stripped.startswith("$") and len(self._planner) >= self.PLANNER_SIZE:
                return
            del self._rx[:nl + 1]
            if not raw.strip():
                continue
            self.executed_lines.append(raw)
            for m in _MSG.finditer(raw):
                self._out_line(f"[MSG:{m.group(1).strip()}]")
            resp = self._execute(raw, stripped)
            if resp is None:
                continue          # ok será enviado depois (bloco diferido)
            if resp.startswith("error:"):
                self.errors.append((raw, int(resp[6:])))
            self._out_line(resp)

    @staticmethod
    def _needs_sync(s: str) -> bool:
        if not s:
            return False
        if s.startswith("$"):
            return s[:2] in ("$H", "$C", "$X") or s.startswith("$SLP") or (len(s) > 1 and s[1].isdigit())
        for letter, val in _WORD.findall(s):
            if not val or letter not in "GM":
                continue
            v = float(val)
            if letter == "G" and (v in (4, 10, 28, 30, 92) or 38 < v < 39):
                return True
            if letter == "M" and v in (0, 1, 2, 30):
                return True
        return False

    # ----------------------------------------------------------------- $ comandos
    def _execute(self, raw: str, s: str) -> str | None:
        if len(s) >= self.line_buffer:
            return "error:11"
        if not s:
            return "ok"
        if self.esp32 and raw.strip().startswith("$"):
            r = self._esp32_system(raw.strip())
            if r != "":
                return r
        if s.startswith("$"):
            return self._system(s)
        if self._state == "Alarm" or self._state == "Jog":
            return "error:9"
        return self._gcode(s)

    def _system(self, s: str) -> str | None:
        st = self._state
        idle_like = st in ("Idle", "Alarm", "Check") or st.startswith("Hold:0") and not self._planner
        if s == "$":
            self._out_line("[HLP:$$ $# $G $I $N $x=val $Nx=line $J=line $SLP $C $X $H ~ ! ? ctrl-x]")
            return "ok"
        if s == "$$" or (self.esp32 and s == "$+"):
            for k in sorted(self.settings):
                if self.esp32 and s == "$$" and k in _ESP32_EXTENDED_NUMBERED:
                    continue
                v = self.settings[k]
                self._out_line(f"${k}={int(v)}" if k in _INT_SETTINGS else f"${k}={v:.3f}")
            return "ok"
        if s == "$#":
            for i in range(54, 60):
                self._out_line("[G%d:%.3f,%.3f,%.3f]" % (i, *self._wcs[i]))
            self._out_line("[G28:%.3f,%.3f,%.3f]" % tuple(self._g28))
            self._out_line("[G30:%.3f,%.3f,%.3f]" % tuple(self._g30))
            self._out_line("[G92:%.3f,%.3f,%.3f]" % tuple(self._g92))
            self._out_line("[TLO:0.000]")
            self._out_line("[PRB:%.3f,%.3f,%.3f:%d]" % (*self._prb, self._prb_ok))
            return "ok"
        if s == "$G":
            motion = {0: "G0", 1: "G1", 2: "G2", 3: "G3", 80: "G80"}.get(self._motion, "G0")
            words = [motion, f"G{self._wcs_sel}", f"G{self._plane}", "G21" if self._scale == 1 else "G20",
                     "G90" if self._absolute else "G91", "G94", self._spindle, self._coolant,
                     f"T{self._tool}", f"F{self._feed:g}", f"S{self._sval:g}"]
            self._out_line("[GC:" + " ".join(words) + "]")
            return "ok"
        if s == "$I":
            if self.esp32:
                build = "2022042101" if self.firmware == "mks_dlc32" else "20211103"
                self._out_line(f"[VER:{self.version}.{build}:]")
                self._out_line("[OPT:PHW*#EW]")
                machine = "MKS DLC32" if self.firmware == "mks_dlc32" else "Test Drive - Demo Only No I/O!"
                self._out_line(f"[MSG:Using machine:{machine}]")
                self._out_line(self._wifi_info())
                return "ok"
            self._out_line(f"[VER:{self.VERSION}.20190830:]")
            self._out_line(f"[OPT:V,{self.PLANNER_SIZE},{self.RX_BUFFER_SIZE}]")
            return "ok"
        if s == "$N":
            self._out_line("$N0=")
            self._out_line("$N1=")
            return "ok"
        if s.startswith("$J="):
            return self._jog(s[3:])
        if not idle_like:
            return "error:8"
        if s == "$X":
            if st == "Alarm":
                self._out_line("[MSG:Caution: Unlocked]")
                self._state = "Idle"
            return "ok"
        if s == "$H":
            if not self.settings.get(22):
                return "error:5"
            dist = math.dist(self._mpos, (0.0, 0.0, 0.0))
            secs = 0.3 + dist / (self.settings[25] / 60.0)
            blk = _Block("home", seconds=secs, defer_ok=True)
            self._planner.append(blk)
            self._defer = blk
            self._state = "Home"
            return None
        if s == "$C":
            if self._check:
                self._out_line("[MSG:Disabled]")
                self._out_line("ok")
                self._soft_reset()
                return None
            self._check = True
            self._state = "Check"
            self._out_line("[MSG:Enabled]")
            return "ok"
        if s == "$SLP":
            self._out_line("[MSG:Sleeping]")
            self._state = "Sleep"
            return "ok"
        if s.startswith("$RST="):
            if self.esp32 and s[5:] in ("@", "WIFI", "*"):
                for k, v in ESP32_EXTENDED.items():
                    if k.split("/")[0] in ("Radio", "Sta", "AP", "System", "Telnet", "Http"):
                        self.ext[k] = v
            if s[5:] in ("$", "*"):
                self.settings = dict(self._defaults)
            if s[5:] in ("#", "*"):
                self._wcs = {i: [0.0, 0.0, 0.0] for i in range(54, 60)}
            return "ok"
        m = re.fullmatch(r"\$(\d+)=([-+]?\d*\.?\d+)", s)
        if m:
            k = int(m.group(1))
            if k not in self._defaults:
                return "error:3"
            v = float(m.group(2))
            if v < 0:
                return "error:4"
            if k == 0 and v < 3:
                return "error:6"
            self.settings[k] = v
            return "ok"
        if re.fullmatch(r"\$N\d=.*", s):
            return "ok"
        return "error:3"

    # ----------------------------------------------------------------- Grbl_Esp32
    def _wifi_info(self) -> str:
        mode = self.ext.get("Radio/Mode", "AP").upper()
        if mode == "STA":
            return (f"[MSG:Mode=STA:SSID={self.ext.get('Sta/SSID', '')}:Status=Connected:"
                    f"IP=192.168.0.77:MAC=24-6F-28-00-00-01]")
        if mode == "AP":
            return (f"[MSG:Mode=AP:SSDI={self.ext.get('AP/SSID', '')}:IP={self.ext.get('AP/IP', '')}:"
                    f"MAC=24-6F-28-00-00-02]")
        return "[MSG:No Wifi]"

    def _alias_number(self, name: str) -> int | None:
        from brokcam.machine.firmware import NAME_ALIASES
        n = NAME_ALIASES.get(name.lower())
        return int(n) if n is not None and int(n) in self.settings else None

    def _ext_display(self, name: str) -> str:
        if name.lower() in _PASSWORDS:
            return "******"
        return self.ext[name]

    def _numbered_display(self, k: int) -> str:
        from brokcam.machine.firmware import NUMBERED
        v = self.settings[k]
        kind = NUMBERED[str(k)].kind if str(k) in NUMBERED else "float"
        if kind == "bool":
            return "On" if v else "Off"
        if kind == "mask" and k != 10:
            return "".join(a for i, a in enumerate("XYZ") if int(v) & (1 << i))
        return f"{int(v)}" if k in _INT_SETTINGS else f"{v:.3f}"

    def _esp32_system(self, raw: str) -> str:
        """Comandos nomeados do Grbl_Esp32. Retorna "" se não for um deles."""
        from brokcam.machine.firmware import NUMBERED, setting_def, validate_value
        body = raw[1:]
        up = body.upper()
        if up in ("S", "SC"):
            for k in sorted(self.settings):
                alias = NUMBERED[str(k)].alias if str(k) in NUMBERED else ""
                if not alias:
                    continue
                val = self._numbered_display(k)
                if up == "SC":
                    if abs(self.settings[k] - self._defaults.get(k, self.settings[k])) < 1e-9:
                        continue
                    self._out_line(f"${alias}={val}    (Default=?)")
                else:
                    self._out_line(f"${alias}={val}")
            for name in self.ext:
                if up == "SC" and self.ext[name] == ESP32_EXTENDED.get(name):
                    continue
                self._out_line(f"${name}={self._ext_display(name)}")
            if up == "SC":
                self._out_line("(Passwords not shown)")
            return "ok"
        if up == "WIFI/LISTAPS":
            self._out_line('{"AP_LIST":[{"SSID":"Oficina","SIGNAL":"82","IS_PROTECTED":"1"},'
                           '{"SSID":"Visitantes","SIGNAL":"40","IS_PROTECTED":"0"}]}')
            return "ok"
        if up == "SYSTEM/CONTROL=RESTART" or up == "BYE":
            if up == "BYE":
                return "error:3"
            self.restarts += 1
            self._planner.clear()
            self._rx.clear()
            self._init_state(cold=True)
            self._out_line("[MSG:Restart ongoing]")
            self._emit_banner()
            return None  # type: ignore[return-value]
        m = re.fullmatch(r"([A-Za-z][A-Za-z0-9_/.]*)=(.*)", body)
        if m is None:
            return ""
        name, value = m.group(1), m.group(2).strip()
        if name.upper() in ("RST", "J") or re.fullmatch(r"N\d", name.upper()):
            return ""
        n = self._alias_number(name)
        if n is not None:
            return self._system(f"${n}={setting_value_number(NUMBERED[str(n)].kind, value)}")
        real = next((k for k in self.ext if k.lower() == name.lower()), None)
        if real is None:
            return "error:3"
        d = setting_def(real)
        try:
            norm = validate_value(d, value)
        except ValueError:
            return "error:81" if d.kind not in ("int", "float") else "error:80"
        if d.kind == "password" and norm == "":
            norm = ""
        if d.kind == "bool":
            norm = "On" if norm == "1" else "Off"
        elif d.kind == "mask":
            norm = "".join(a for i, a in enumerate("XYZ") if int(norm) & (1 << i))
        elif d.kind == "float":
            norm = f"{float(norm):.3f}"
        elif real.lower() in _ESP32_INT:
            norm = str(int(float(norm)))
        self.ext[real] = norm
        return "ok"

    def _jog(self, body: str) -> str:
        if self._state not in ("Idle", "Jog"):
            return "error:8"
        words = _WORD.findall(body)
        if "".join(a + b for a, b in words) != body:
            return "error:16"
        absolute = self._absolute
        scale = self._scale
        machine = False
        axes: dict[str, float] = {}
        feed = None
        for letter, val in words:
            if val == "":
                return "error:2"
            v = float(val)
            if letter == "G":
                if v == 91:
                    absolute = False
                elif v == 90:
                    absolute = True
                elif v == 20:
                    scale = 25.4
                elif v == 21:
                    scale = 1.0
                elif v == 53:
                    machine = True
                else:
                    return "error:16"
            elif letter in "XYZ":
                axes[letter] = v * scale
            elif letter == "F":
                feed = v * scale
            else:
                return "error:16"
        if feed is None or feed <= 0:
            return "error:16"
        if not axes:
            return "error:26"
        tgt = self._target(axes, absolute, machine)
        if not self._within_limits(tgt):
            return "error:15"
        self._planner.append(_Block("move", tuple(tgt), rate=feed, jog=True))
        self._ppos = list(tgt)
        self._state = "Jog"
        return "ok"

    def _target(self, axes: dict[str, float], absolute: bool, machine: bool) -> list[float]:
        wco = [0.0, 0.0, 0.0] if machine else self._wco()
        tgt = list(self._ppos)
        for i, a in enumerate("XYZ"):
            if a in axes:
                tgt[i] = axes[a] + wco[i] if (absolute or machine) else self._ppos[i] + axes[a]
        return tgt

    def _within_limits(self, tgt: list[float]) -> bool:
        if not self.settings.get(20):
            return True
        for i, k in enumerate((130, 131, 132)):
            if tgt[i] > 1e-6 or tgt[i] < -self.settings[k] - 1e-6:
                return False
        return True

    # ----------------------------------------------------------------- G-code
    def _gcode(self, s: str) -> str | None:
        words = _WORD.findall(s)
        if "".join(a + b for a, b in words) != s:
            return "error:1"
        gs: list[float] = []
        ms: list[int] = []
        vals: dict[str, float] = {}
        for letter, val in words:
            if val == "":
                return "error:2"
            v = float(val)
            if letter == "G":
                g = round(v, 1)
                if g not in _SUPPORTED_G:
                    return "error:20"
                gs.append(g)
            elif letter == "M":
                if v != int(v) or int(v) not in _SUPPORTED_M:
                    return "error:20"
                ms.append(int(v))
            elif letter in "XYZFSTPLRIJKN":
                if letter in vals:
                    return "error:25"
                vals[letter] = v
            else:
                return "error:20"
        if "N" in vals and not (0 <= vals["N"] <= 9_999_999):
            return "error:27"
        if "T" in vals and vals["T"] > 255:
            return "error:38"
        motions = [g for g in gs if g in _MOTION_G or g in (28, 30)]
        if len(motions) > 1:
            return "error:21"

        # ---- estados modais simples
        for g in gs:
            if g == 90:
                self._absolute = True
            elif g == 91:
                self._absolute = False
            elif g == 20:
                self._scale = 25.4
            elif g == 21:
                self._scale = 1.0
            elif g in (17, 18, 19):
                self._plane = int(g)
            elif 54 <= g <= 59:
                self._wcs_sel = int(g)
        sc = self._scale
        if "F" in vals:
            self._feed = vals["F"] * sc
        if "S" in vals:
            self._sval = vals["S"]
        if "T" in vals:
            self._tool = int(vals["T"])
        for m in ms:
            if m in (3, 4, 5):
                self._spindle = f"M{m}"
            elif m in (7, 8, 9):
                self._coolant = f"M{m}"
        axes = {a: vals[a] * sc for a in "XYZ" if a in vals}

        # ---- comandos não modais
        if 4 in gs:
            if "P" not in vals:
                return "error:28"
            if self._check:
                return "ok"
            blk = _Block("dwell", seconds=float(vals["P"]), defer_ok=True)
            self._planner.append(blk)
            self._defer = blk
            return None
        if 10 in gs:
            L = vals.get("L")
            P = int(vals.get("P", 0))
            if L not in (2, 20) or "P" not in vals:
                return "error:28"
            idx = 54 + (P - 1) if P > 0 else self._wcs_sel
            if idx not in self._wcs:
                return "error:29"
            w = self._wcs[idx]
            for i, a in enumerate("XYZ"):
                if a in axes:
                    w[i] = axes[a] if L == 2 else self._ppos[i] - axes[a] - self._g92[i]
            return "ok"
        if 92 in gs:
            wco = self._wcs[self._wcs_sel]
            for i, a in enumerate("XYZ"):
                if a in axes:
                    self._g92[i] = self._ppos[i] - wco[i] - axes[a]
            return "ok"
        if 92.1 in gs:
            self._g92 = [0.0, 0.0, 0.0]
            return "ok"
        if 28.1 in gs or 30.1 in gs:
            (self._g28 if 28.1 in gs else self._g30)[:] = list(self._ppos)
            return "ok"
        if 28 in gs or 30 in gs:
            store = self._g28 if 28 in gs else self._g30
            if axes:
                mid = self._target(axes, self._absolute, False)
                self._plan_move(mid, rapid=True)
            self._plan_move(list(store), rapid=True)
            return "ok"

        for m in ms:
            if m in (0, 1):
                if not self._check:
                    self._m0_pause = True
                    self._state = "Hold:0"
            elif m in (2, 30):
                self._spindle = "M5"
                self._coolant = "M9"
                self._absolute = True
                self._g92 = [0.0, 0.0, 0.0]
                self._motion = 1
                self._wcs_sel = 54

        motion_word = motions[0] if motions else None
        if motion_word is not None:
            self._motion = int(motion_word) if motion_word in (0, 1, 2, 3, 80) else self._motion
        if not axes:
            if motion_word in (2, 3) or (motion_word is not None and 38 < motion_word < 39):
                return "error:26"
            return "ok"
        mode = motion_word if motion_word is not None else self._motion
        if mode == 80:
            return "error:31"
        machine = 53 in gs
        tgt = self._target(axes, self._absolute, machine)
        if mode in (1, 2, 3) or (38 < mode < 39):
            if self._feed <= 0:
                return "error:22"
        if not self._within_limits(tgt):
            self._planner.clear()
            self._state = "Alarm"
            self._critical = True
            self._out_line("ALARM:2")
            self._out_line("[MSG:Reset to continue]")
            return None
        if 38 < mode < 39:
            if self._check:
                return "ok"
            blk = _Block("probe", tuple(tgt), rate=self._feed, defer_ok=True, probe_mode=mode)
            self._planner.append(blk)
            self._defer = blk
            return None
        if mode in (2, 3):
            if self._plane == 17 and not any(k in vals for k in "IJR"):
                return "error:35"
            length = self._arc_length(tgt, vals, mode == 2)
            self._plan_move(tgt, rapid=False, path_len=length)
            return "ok"
        self._plan_move(tgt, rapid=(mode == 0))
        return "ok"

    def _arc_length(self, tgt: list[float], vals: dict[str, float], cw: bool) -> float:
        p = self._ppos
        chord = math.dist(p[:2], tgt[:2])
        if "R" in vals:
            r = abs(vals["R"] * self._scale)
            if r <= 0 or chord > 2 * r + 1e-3:
                return chord
            ang = 2 * math.asin(min(1.0, chord / (2 * r)))
            return max(chord, r * ang)
        cx = p[0] + vals.get("I", 0.0) * self._scale
        cy = p[1] + vals.get("J", 0.0) * self._scale
        r = math.hypot(p[0] - cx, p[1] - cy)
        a0 = math.atan2(p[1] - cy, p[0] - cx)
        a1 = math.atan2(tgt[1] - cy, tgt[0] - cx)
        sweep = (a0 - a1) if cw else (a1 - a0)
        if sweep <= 1e-9:
            sweep += 2 * math.pi
        return math.hypot(r * sweep, tgt[2] - p[2])

    def _plan_move(self, tgt: list[float], rapid: bool, path_len: float | None = None) -> None:
        if self._check:
            self._ppos = list(tgt)
            return
        rate = self._feed
        if path_len is not None:
            d = math.dist(self._ppos, tgt)
            if d > 1e-9 and path_len > d:
                rate = self._feed * d / path_len   # mantém o tempo do arco real
        self._planner.append(_Block("move", tuple(tgt), rate=rate, rapid=rapid))
        self._ppos = list(tgt)
        if self._state == "Idle":
            self._state = "Run"
