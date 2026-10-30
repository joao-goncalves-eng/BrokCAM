"""Testes do controlador GRBL usando o simulador (sem hardware)."""
from __future__ import annotations

import math
import threading
import time

import pytest

from brokcam.machine import (
    ALARMS, ERRORS, SETTINGS, STATES, GrblController, GrblError, GrblSimulator, parse_status,
)


def wait_for(cond, timeout=5.0):
    end = time.monotonic() + timeout
    while time.monotonic() < end:
        if cond():
            return True
        time.sleep(0.01)
    return False


@pytest.fixture
def machine():
    def make(speed=math.inf, **kw):
        sim = GrblSimulator(speed=speed, **kw)
        ctl = GrblController(status_interval=0.05)
        ctl.connect("SIMULADOR", transport=sim)
        made.append(ctl)
        return ctl, sim
    made: list[GrblController] = []
    yield make
    for c in made:
        c.disconnect()


def test_tables_complete():
    assert set(range(1, 18)) - {18, 19} <= set(ERRORS)
    assert set(range(20, 39)) <= set(ERRORS)
    assert set(range(1, 11)) <= set(ALARMS)
    for n in (0, 1, 10, 22, 32, 100, 110, 120, 130, 132):
        assert n in SETTINGS and SETTINGS[n].help
    for s in ("Idle", "Run", "Hold:0", "Hold:1", "Jog", "Alarm", "Door:0", "Door:3", "Check", "Home", "Sleep"):
        assert s in STATES


def test_parse_status_mpos_wco():
    st = parse_status("<Run|MPos:10.000,5.000,-1.000|FS:500,10000|WCO:2.000,1.000,-3.000|Ov:110,100,90|Pn:XP|Bf:15,128>")
    assert st.state == "Run" and st.feed == 500 and st.spindle == 10000
    assert st.wpos == pytest.approx((8.0, 4.0, 2.0))
    assert st.overrides == (110, 100, 90)
    assert st.pins == "XP" and st.planner_free == 15 and st.rx_free == 128
    st2 = parse_status("<Hold:0|WPos:1.000,2.000,3.000|FS:0,0>", wco_cache=(1.0, 1.0, 1.0))
    assert st2.base_state == "Hold" and st2.mpos == pytest.approx((2.0, 3.0, 4.0))


def test_connect_banner_and_queries(machine):
    ctl, sim = machine()
    assert ctl.version == "1.1h"
    s = ctl.read_settings()
    assert s[110] == 1000.0 and s[22] == 0
    ctl.write_setting(110, 1500)
    assert ctl.read_settings()[110] == 1500.0
    assert "G54" in ctl.read_parameters()
    assert "G21" in ctl.read_parser_state()
    assert wait_for(lambda: ctl.status is not None and ctl.status.state == "Idle")


def test_stream_5000_lines_char_counting(machine):
    ctl, sim = machine()
    lines = ["G21 G90", "G1 F800"] + [f"G1 X{i % 50}.{i % 1000:03d} Y{i % 37}.5 (c{i})" for i in range(5000)]
    progress = []
    ctl.on_progress = lambda s, a, t: progress.append((s, a, t))
    done = []
    ctl.on_stream_done = done.append
    ctl.echo_stream = False
    ctl.stream_program(lines)
    assert ctl.wait_stream(60)
    assert done == [True]
    sent = [ln for ln in sim.executed_lines if not ln.startswith("$")]
    expected = [ln.split(" (")[0] for ln in lines]
    assert sent == expected                     # nada perdido, duplicado ou fora de ordem
    assert sim.overflow_count == 0
    assert sim.max_rx_used <= 128
    assert sim.max_rx_used > 64                 # o buffer foi realmente aproveitado
    assert progress[-1] == (len(lines), len(lines), len(lines))
    assert ctl.rx_used == 0


def test_stream_send_response_mode(machine):
    ctl, sim = machine()
    lines = ["G1 F500"] + [f"G1 X{i}" for i in range(200)]
    ctl.stream_program(lines, mode="sr")
    assert ctl.wait_stream(30)
    assert [ln for ln in sim.executed_lines] == lines


def test_pause_resume(machine):
    ctl, sim = machine(speed=5.0)
    ctl.stream_program(["G1 F600", "G1 X20", "G1 X0"])
    assert wait_for(lambda: sim.state == "Run")
    ctl.pause()
    assert wait_for(lambda: ctl.state == "Hold:0", 3)
    x = sim.mpos[0]
    time.sleep(0.2)
    assert sim.mpos[0] == x                     # parado durante a pausa
    ctl.resume()
    assert wait_for(lambda: ctl.state == "Run", 3)
    assert ctl.wait_stream(10)
    assert wait_for(lambda: sim.state == "Idle" and sim.mpos[0] == 0.0, 15)


def test_error_reported_with_line_number(machine):
    ctl, sim = machine()
    ctl.stop_on_error = False
    errors = []
    ctl.on_error = lambda code, msg, line: errors.append((code, line))
    lines = ["G1 F500"] + [f"G1 X{i}" for i in range(50)] + ["G99 X1"] + [f"G1 Y{i}" for i in range(50)]
    ctl.stream_program(lines)
    assert ctl.wait_stream(20)
    assert errors == [(20, 52)]
    assert len(sim.executed_lines) == len(lines)


def test_error_holds_stream_until_resume(machine):
    ctl, sim = machine()
    errors = []
    ctl.on_error = lambda code, msg, line: errors.append((code, line))
    lines = ["G1 F500", "M6", "G1 X1", "G1 X2"]
    ctl.stream_program(lines)
    assert wait_for(lambda: errors == [(20, 2)])
    assert not ctl.wait_stream(0.3) or len(sim.executed_lines) <= 4
    ctl.resume()
    assert ctl.wait_stream(5)
    assert sim.executed_lines[-1] == "G1 X2"


def test_stop_soft_reset(machine):
    ctl, sim = machine(speed=1.0)
    done = []
    ctl.on_stream_done = done.append
    ctl.stream_program(["G1 F100", "G1 X50", "G1 Y50"])
    assert wait_for(lambda: sim.state == "Run")
    alarms = []
    ctl.on_alarm = lambda c, m: alarms.append(c)
    ctl.stop()
    assert done == [False]
    assert wait_for(lambda: alarms == [3])      # reset em movimento -> ALARM:3
    ctl.unlock()
    assert wait_for(lambda: ctl.state == "Idle")


def test_jog_and_cancel(machine):
    ctl, sim = machine(speed=1.0)
    ctl.jog(dx=10, feed=6000, wait=True)
    assert wait_for(lambda: sim.mpos[0] == pytest.approx(10.0), 5)
    ctl.jog(dx=100, feed=600)
    assert wait_for(lambda: sim.state == "Jog" and sim.mpos[0] > 10.5, 3)
    ctl.jog_cancel()
    assert wait_for(lambda: ctl.state == "Idle", 3)
    assert sim.mpos[0] < 100


def test_work_zero_and_probe(machine):
    ctl, sim = machine(probe_surface_z=-12.0)
    ctl.jog(dx=5, dy=7, feed=1000, wait=True)
    assert wait_for(lambda: sim.state == "Idle")
    ctl.set_work_zero("XY")
    z = ctl.probe_z(depth=20, feed=100, plate_thickness=1.5, retract=2.0)
    assert z == pytest.approx(-12.0)
    assert wait_for(lambda: ctl.status is not None and ctl.status.wpos is not None
                    and ctl.status.wpos[2] == pytest.approx(3.5, abs=1e-3), 3)
    assert ctl.status.wpos[0] == pytest.approx(0.0)
    with pytest.raises(GrblError):
        ctl.probe_z(depth=1, feed=100)          # não alcança a superfície -> ALARM:5
    assert wait_for(lambda: ctl.state == "Alarm")
    ctl.unlock()
    ctl.goto_work_zero(z_safe=5)


def test_alarm_soft_limit_and_homing(machine):
    ctl, sim = machine(homing_enabled=True, settings={20: 1})
    assert wait_for(lambda: ctl.state == "Alarm")
    r = ctl.send_command("G0 X1")
    assert r is not None and r.error == 9       # bloqueado em alarme
    ctl.home()
    assert wait_for(lambda: ctl.state == "Idle")
    alarms = []
    ctl.on_alarm = lambda c, m: alarms.append(c)
    ctl.send_command("G0 X10", wait=False)      # fora do curso (curso é 0..-300)
    assert wait_for(lambda: alarms == [2])
    ctl.stop()
    ctl.unlock()                                # aguarda o banner do reset antes de enviar
    r = ctl.send_command("$J=G91 X50 F100")
    assert r is not None and r.error == 15


def test_check_mode(machine):
    ctl, sim = machine(speed=1.0)
    lines = ["G1 F100", "G1 X100", "G1 Y100"]
    ctl.stream_program(lines, check=True)
    assert ctl.wait_stream(5)
    assert sim.mpos == (0.0, 0.0, 0.0)
    assert wait_for(lambda: ctl.state == "Idle", 3)


def test_disconnect_detected(machine):
    ctl, sim = machine()
    events = []
    ctl.on_connection = events.append
    sim.simulate_disconnect()
    assert wait_for(lambda: events == [False], 3)
    assert not ctl.is_connected


def test_state_change_callback_threadsafe(machine):
    ctl, sim = machine(speed=20.0)
    states = []
    lock = threading.Lock()

    def on_change(old, new):
        with lock:
            states.append(new)
    ctl.on_state_change = on_change
    ctl.stream_program(["G1 F600", "G1 X50"])
    assert ctl.wait_stream(5)
    assert wait_for(lambda: "Idle" in states and "Run" in states, 5)


def test_serial_port_listing_and_errors(monkeypatch):
    from types import SimpleNamespace
    from brokcam.machine import transport

    fake = [
        SimpleNamespace(device="COM3", description="Serial Padrão por link Bluetooth (COM3)", vid=None,
                        hwid=r"BTHENUM\{0000}"),
        SimpleNamespace(device="COM19", description="USB-SERIAL CH340 (COM19)", vid=0x1A86,
                        hwid="USB VID:PID=1A86:7523"),
        SimpleNamespace(device="COM10", description="Outro", vid=None, hwid="ACPI"),
    ]
    import serial.tools.list_ports as lp
    monkeypatch.setattr(lp, "comports", lambda: fake)
    ports = transport.list_serial_ports()
    assert ports[0][0] == "COM19" and "provável CNC" in ports[0][1]
    assert "Bluetooth (não é a CNC)" in dict(ports)["COM3"]
    msg = transport.explain_serial_error("COM19", Exception("PermissionError(13, 'Acesso negado.', None, 5)"))
    assert "em uso por outro programa" in msg and "Candle" in msg
    assert "não existe" in transport.explain_serial_error("COM7", Exception("FileNotFoundError(2, ...)"))
