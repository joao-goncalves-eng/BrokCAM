"""Detecção de firmware, parsers, catálogo, simulador DLC32 e transporte TCP."""
from __future__ import annotations

import math
import socket
import threading
import time

import pytest

from brokcam.machine import GrblController, GrblSimulator
from brokcam.machine import firmware as fw
from brokcam.machine.transport import TcpTransport, Transport, is_tcp_url


# --------------------------------------------------------------------------- detecção

DLC32_I = ["[VER:1.1.2022042101:]", "[OPT:PHW*#EW]", "[MSG:Using machine:MKS DLC32]",
           "[MSG:Mode=AP:SSDI=MKS_DLC:IP=192.168.4.1:MAC=24-6F-28-00-00-02]"]


@pytest.mark.parametrize("banner, info, kind, version", [
    ("Grbl 1.1h ['$' for help]", ["[VER:1.1h.20190830:]", "[OPT:V,15,128]"], "grbl", "1.1h"),
    ("Grbl 1.1f ['$' for help]", None, "grbl", "1.1f"),
    ("Grbl 1.1 ['$' for help]", DLC32_I, "mks_dlc32", "1.1"),
    ("Grbl 1.1 ['$' for help]", None, "mks_dlc32", "1.1"),
    ("Grbl 1.3a ['$' for help]", ["[VER:1.3a.20211103:]", "[OPT:PHSW]",
                                  "[MSG:Using machine:ESP32 Dev Controller V4]",
                                  "[MSG:Mode=STA:SSID=Casa:Status=Connected:IP=192.168.0.9:MAC=AA-BB]"],
     "grbl_esp32", "1.3a"),
    ("Grbl 3.7.8 [FluidNC v3.7.8 (wifi) '$' for help]", None, "fluidnc", "3.7.8"),
    ("GrblHAL 1.1f ['$' or '$HELP' for help]", None, "grblhal", "1.1f"),
    ("algo estranho", None, "desconhecido", ""),
])
def test_detect_firmware(banner, info, kind, version):
    f = fw.detect_firmware(banner, info)
    assert f.kind == kind
    assert f.version == version
    assert f.label


def test_build_info_and_wifi_parsing():
    f = fw.detect_firmware("Grbl 1.1 ['$' for help]", DLC32_I)
    assert f.build == "2022042101" and f.machine == "MKS DLC32"
    assert f.supports_extended and f.is_esp32
    assert f.restart_command == "$System/Control=RESTART"
    assert f.wifi == [{"mode": "AP", "ssid": "MKS_DLC", "ip": "192.168.4.1", "mac": "24:6F:28:00:00:02"}]
    classic = fw.parse_build_info(["[VER:1.1h.20190830:]", "[OPT:V,15,128]"])
    assert classic["planner_blocks"] == 15 and classic["rx_buffer"] == 128
    assert fw.parse_wifi_message("No Wifi") == {"mode": "OFF"}
    assert fw.parse_wifi_message("Caution: Unlocked") is None
    assert ("W", "WiFi") in fw.decode_options("PHW*#EW", "mks_dlc32")
    assert ("V", "Spindle com velocidade variável (PWM)") in fw.decode_options("V", "grbl")


def test_parsers():
    ext = fw.parse_extended_settings([
        "$Sta/SSID=Minha Rede", "$Sta/Password=******", "$Homing/Cycle1=",
        "$X/MaxRate=6000.000", "$Radio/Mode=AP    (Default=STA)", "ok", "lixo", "[MSG:x]",
    ])
    assert ext == {"Sta/SSID": "Minha Rede", "Sta/Password": "******", "Homing/Cycle1": "",
                   "X/MaxRate": "6000.000", "Radio/Mode": "AP"}
    assert fw.parse_numbered_settings(["$110=1000.000", "$0=10", "ok"]) == {110: "1000.000", 0: "10"}
    p = fw.parse_parameters(["[G54:1.000,2.000,-3.000]", "[TLO:0.500]", "[PRB:1.000,2.000,3.000:1]"])
    assert p["G54"] == (1.0, 2.0, -3.0) and p["TLO"] == (0.5,) and p["PRB"] == (1.0, 2.0, 3.0, 1.0)
    words = fw.parse_parser_state(["[GC:G0 G54 G17 G21 G90 G94 M5 M9 T0 F0 S0]"])
    items = fw.describe_parser_state(words)
    groups = {i.group: i.meaning for i in items}
    assert "Milímetros" in groups["Unidades"] and "Absoluta" in groups["Distância"]
    assert "Desligado" in groups["Spindle"] and groups["Sistema de coordenadas"].startswith("Coordenadas")
    pins = fw.describe_pins("XP")
    assert pins["X"] and pins["P"] and not pins["Y"]
    aps = fw.parse_ap_list(['{"AP_LIST":[{"SSID":"A","SIGNAL":"20","IS_PROTECTED":"0"},'
                            '{"SSID":"B","SIGNAL":"90","IS_PROTECTED":"1"}]}'])
    assert [a["ssid"] for a in aps] == ["B", "A"] and aps[0]["protected"]
    assert fw.split_tcp_url("tcp://192.168.4.1:23") == ("192.168.4.1", 23)
    assert fw.split_tcp_url("telnet://placa.local") == ("placa.local", 23)
    with pytest.raises(ValueError):
        fw.split_tcp_url("tcp://host:99999")


def test_catalog_has_explanations():
    for key in list(range(0, 7)) + [10, 11, 12, 13, 20, 21, 22, 23, 24, 25, 26, 27, 30, 31, 32] + \
            list(range(100, 103)) + list(range(110, 113)) + list(range(120, 123)) + list(range(130, 133)):
        d = fw.NUMBERED[str(key)]
        assert d.help.strip() and d.name and d.category in fw.CATEGORIES, key
        assert d.typical, key
    for d in list(fw.NUMBERED.values()) + list(fw.EXTENDED.values()):
        assert len(d.help) > 20 and d.category in fw.CATEGORIES, d.key
        assert d.kind in ("int", "float", "bool", "mask", "enum", "string", "password", "ip")
        if d.kind == "enum":
            assert d.choices
    for name in ("Radio/Mode", "Sta/SSID", "Sta/Password", "Sta/IPMode", "AP/SSID", "AP/IP",
                 "Telnet/Port", "Http/Port", "System/Hostname", "Spindle/Type"):
        d = fw.EXTENDED[name]
        assert d.category == "Rede / WiFi" or name == "Spindle/Type"
    assert "rede" in fw.EXTENDED["Sta/IPMode"].risk.lower()
    assert fw.EXTENDED["Radio/Mode"].restart
    # aliases do Grbl_Esp32 e eixos extras
    assert fw.numbered_equivalent("Stepper/Pulse") == "0"
    assert fw.numbered_equivalent("Spindle/PWM/Max") == "36"
    assert fw.numbered_equivalent("A/StepsPerMm") == "103"
    assert fw.setting_def("$110").name.startswith("Velocidade")
    assert fw.setting_def("Desconhecida/Coisa").risk
    cats = fw.categorize(["110", "Sta/SSID", "0", "Spindle/Type"])
    assert list(cats) == ["Motores e passos", "Velocidades e acelerações", "Spindle / Laser", "Rede / WiFi"]
    # o perfil de referência só referencia configurações conhecidas
    for key, pv in fw.DLC32_PROFILE.items():
        d = fw.setting_def(key)
        assert d.key == key and pv.note
        fw.validate_value(d, pv.value)


def test_validate_value():
    v = fw.validate_value
    assert v(fw.NUMBERED["110"], "1500,5") == "1500.5"
    assert v(fw.NUMBERED["0"], "10") == "10"
    with pytest.raises(ValueError):
        v(fw.NUMBERED["0"], "2")                  # abaixo do mínimo
    with pytest.raises(ValueError):
        v(fw.NUMBERED["1"], "1.5")                # inteiro
    with pytest.raises(ValueError):
        v(fw.NUMBERED["110"], "abc")
    assert v(fw.NUMBERED["3"], "5") == "5"
    with pytest.raises(ValueError):
        v(fw.NUMBERED["3"], "8")                  # máscara XYZ vai até 7
    assert v(fw.NUMBERED["22"], "1") == "1"
    assert v(fw.EXTENDED["Radio/Mode"], "sta") == "STA"
    with pytest.raises(ValueError):
        v(fw.EXTENDED["Radio/Mode"], "XX")
    assert v(fw.EXTENDED["AP/IP"], "192.168.4.1") == "192.168.4.1"
    with pytest.raises(ValueError):
        v(fw.EXTENDED["AP/IP"], "300.1.1.1")
    with pytest.raises(ValueError):
        v(fw.EXTENDED["Sta/Password"], "curta")    # mínimo 8
    with pytest.raises(ValueError):
        v(fw.EXTENDED["Sta/SSID"], "rede?")        # caractere de tempo real
    with pytest.raises(ValueError):
        v(fw.EXTENDED["System/Hostname"], "nome com espaço")
    assert v(fw.EXTENDED["Homing/Cycle0"], "XY") == "3"
    assert fw.values_equal(fw.NUMBERED["110"], "1000", "1000.000")
    assert fw.values_equal(fw.EXTENDED["Spindle/PWM/Invert"], "Off", "0")
    assert not fw.values_equal(fw.NUMBERED["110"], "1000", "1500")


def test_backup_roundtrip():
    f = fw.detect_firmware("Grbl 1.1 ['$' for help]", DLC32_I)
    data = fw.backup_payload({"110": "1000", "0": "10"},
                             {"Sta/SSID": "x", "Sta/Password": "******", "Stepper/Pulse": "10"}, f)
    assert "Sta/Password" not in data["extended"] and "Stepper/Pulse" not in data["extended"]
    num, ext = fw.read_backup(data)
    assert num == {"0": "10", "110": "1000"} and ext == {"Sta/SSID": "x"}
    with pytest.raises(ValueError):
        fw.read_backup({"format": "outro"})


# --------------------------------------------------------------------------- simulador DLC32

@pytest.fixture
def dlc32():
    sim = GrblSimulator(speed=math.inf, firmware="mks_dlc32")
    ctl = GrblController(status_interval=0.05)
    ctl.connect("SIMULADOR-DLC32", transport=sim)
    yield ctl, sim
    ctl.disconnect()


def test_simulator_dlc32(dlc32):
    ctl, sim = dlc32
    assert ctl.version == "1.1" and ctl.firmware.kind == "mks_dlc32"
    info = ctl.detect_firmware()
    assert info.kind == "mks_dlc32" and info.machine == "MKS DLC32" and info.wifi[0]["ip"] == "192.168.4.1"
    s = ctl.read_settings()
    assert s[100] == 80.0 and s[38] == 1 and 36 not in s     # $36 só aparece no $+/$S
    e = ctl.read_extended_settings()
    assert e["Radio/Mode"] == "AP" and e["Sta/Password"] == "******" and e["Spindle/PWM/Max"] == "100.000"
    ctl.write_extended_setting("Sta/SSID", "Oficina 2")
    assert ctl.read_extended_settings()["Sta/SSID"] == "Oficina 2"
    ctl.write_extended_setting("Stepper/Pulse", "12")        # nome alternativo de $0
    assert ctl.read_settings()[0] == 12
    r = ctl.send_command("$Sta/IPMode=Talvez")
    assert r is not None and r.error == 81
    with pytest.raises(ValueError):
        ctl.write_extended_setting("Sta/SSID", "x?y")
    assert ctl.list_wifi_networks()[0]["ssid"] == "Oficina"
    ctl.send_command("$RST=@")
    assert ctl.read_extended_settings()["Sta/SSID"] == "MKS_DLC"
    assert ctl.restart_board()
    assert wait_for(lambda: sim.restarts == 1)


def test_classic_simulator_has_no_extended():
    sim = GrblSimulator(speed=math.inf)
    ctl = GrblController(status_interval=0.05)
    ctl.connect("SIMULADOR", transport=sim)
    try:
        assert ctl.detect_firmware().kind == "grbl"
        assert not ctl.firmware.supports_extended
        with pytest.raises(Exception):
            ctl.read_extended_settings()
        assert not ctl.restart_board()
    finally:
        ctl.disconnect()


def test_list_ports_includes_dlc32_entries():
    names = [p for p, _ in GrblController.list_ports()]
    assert "SIMULADOR" in names and "SIMULADOR-DLC32" in names and "tcp://192.168.4.1:23" in names


# --------------------------------------------------------------------------- TCP

def wait_for(cond, timeout=5.0):
    end = time.monotonic() + timeout
    while time.monotonic() < end:
        if cond():
            return True
        time.sleep(0.01)
    return False


class _BridgeServer:
    """Servidor TCP local que liga cada cliente a um GrblSimulator (imita o Telnet da DLC32)."""

    def __init__(self, telnet_noise: bool = False) -> None:
        self.srv = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        self.srv.bind(("127.0.0.1", 0))
        self.srv.listen(1)
        self.port = self.srv.getsockname()[1]
        self.telnet_noise = telnet_noise
        self.sim: GrblSimulator | None = None
        self.stop = False
        self.thread = threading.Thread(target=self._serve, daemon=True)
        self.thread.start()

    def _serve(self) -> None:
        conn, _ = self.srv.accept()
        conn.settimeout(0.02)
        self.sim = sim = GrblSimulator(speed=math.inf, firmware="mks_dlc32")
        if self.telnet_noise:
            conn.sendall(bytes([255, 251, 1, 255, 253, 3]))   # IAC WILL ECHO, IAC DO SGA
        while not self.stop:
            out = sim.read()
            if out:
                try:
                    conn.sendall(out)
                except OSError:
                    break
            try:
                data = conn.recv(4096)
                if not data:
                    break
                sim.write(data)
            except socket.timeout:
                pass
            except OSError:
                break
        conn.close()
        sim.close()

    def close(self) -> None:
        self.stop = True
        self.srv.close()


def test_tcp_transport_raw():
    srv = socket.socket()
    srv.bind(("127.0.0.1", 0))
    srv.listen(1)
    port = srv.getsockname()[1]
    got = []

    def serve():
        c, _ = srv.accept()
        c.sendall(b"\xff\xfb\x01Grbl 1.1 ['$' for help]\r\n")
        got.append(c.recv(100))
        c.close()

    t = threading.Thread(target=serve, daemon=True)
    t.start()
    tr = TcpTransport.from_url(f"tcp://127.0.0.1:{port}")
    assert isinstance(tr, Transport) and is_tcp_url(tr.url)
    buf = b""
    end = time.monotonic() + 3
    while b"\n" not in buf and time.monotonic() < end:
        buf += tr.read()
    assert buf == b"Grbl 1.1 ['$' for help]\r\n"          # negociação IAC descartada
    tr.write(b"$I\n")
    t.join(3)
    assert got == [b"$I\n"]
    with pytest.raises(OSError):
        for _ in range(100):                               # servidor fechou -> OSError
            tr.read()
    tr.close()
    tr.close()
    srv.close()


def test_tcp_connection_refused():
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    with pytest.raises(OSError):
        TcpTransport("127.0.0.1", port, connect_timeout=1.0)


def test_controller_over_tcp():
    server = _BridgeServer(telnet_noise=True)
    ctl = GrblController(status_interval=0.05)
    try:
        ctl.connect(f"tcp://127.0.0.1:{server.port}", timeout=3.0)
        assert ctl.connection_kind == "wifi"
        assert ctl.detect_firmware().kind == "mks_dlc32"
        assert ctl.read_settings()[110] == 6000.0
        ctl.write_setting(110, 3000)
        assert ctl.read_settings()[110] == 3000.0
        assert ctl.read_extended_settings()["Telnet/Port"] == "23"
        assert wait_for(lambda: ctl.status is not None and ctl.status.state == "Idle")
    finally:
        ctl.disconnect()
        server.close()
