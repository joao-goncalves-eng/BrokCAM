"""Segurança de operação: retomar da linha, pausa em M6, curso da máquina e parada controlada."""
from __future__ import annotations

import math
import time

import pytest

from brokcam.machine import GrblController, GrblSimulator
from brokcam.machine.safety import (
    envelope_problems, prepare_program, program_bounds, resume_preamble, scan_modal_state,
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
    made: list[GrblController] = []

    def make(speed=math.inf, **kw):
        sim = GrblSimulator(speed=speed, **kw)
        ctl = GrblController(status_interval=0.05)
        ctl.connect("SIMULADOR", transport=sim)
        made.append(ctl)
        return ctl, sim
    yield make
    for c in made:
        c.disconnect()


PROG = [
    "G21 G90 G17 G55",
    "M3 S12000",
    "G0 Z2",
    "G0 X10 Y5",
    "G1 Z-0.1 F60",
    "G1 X20 F300",
    "G91",
    "G1 Y4",
    "G90",
    "G1 X30 Y9",
]


def test_scan_modal_state_tracks_everything():
    st = scan_modal_state(PROG, 8)               # depois de "G1 Y4" (incremental)
    assert (st.units, st.distance, st.plane, st.wcs) == ("G21", "G91", "G17", "G55")
    assert (st.spindle, st.rpm, st.feed) == ("M3", 12000, 300)
    assert (st.x, st.y, st.z) == (20, 9, -0.1)
    assert st.motion == "G1"


def test_resume_preamble_is_safe_order():
    st = scan_modal_state(PROG, 5)
    pre = resume_preamble(st, safe_z_mm=5.0, spindle_wait_s=1.5)
    assert pre[0] == "G21 G90 G17 G55"
    assert pre[1] == "G0 Z5"                      # sobe antes de qualquer XY
    assert pre[2] == "M3 S12000" and pre[3] == "G4 P1.5"
    assert pre[4] == "G0 X10 Y5"
    assert pre[5].startswith("G1 Z-0.1 F60")      # mergulho com avanço lento
    assert pre[-1] in ("G1", "F300", "G1 F300") or pre[-1].endswith("G1")


def test_prepare_program_resume_numbers_preamble_as_start_line():
    pp = prepare_program(PROG, start_line=7)
    nums = [n for n, _ in pp.items]
    assert nums[0] == 7 and all(n >= 7 for n in nums)
    texts = [t for _, t in pp.items]
    assert "G91" in texts and texts[-1] == "G1 X30 Y9"
    assert not pp.warnings


def test_prepare_program_m6_pause():
    lines = ["M3 S10000", "G1 X1 F100", "T2 M6", "G1 X2"]
    pp = prepare_program(lines, m6_pause=True, spindle_wait_s=2)
    texts = [t for _, t in pp.items]
    i = texts.index("M0")
    assert texts[i - 2] == "M5" and "T2" in texts[i - 1]
    assert texts[i + 1] == "T2"                   # linha original sem o M6
    assert texts[i + 2] == "M3 S10000" and texts[i + 3] == "G4 P2"
    assert pp.m6_pauses == 1 and not any("M6" in t for t in texts)


def test_program_bounds_handles_incremental_inches_and_arcs():
    b = program_bounds(["G20 G90", "G0 X0 Y0 Z0.1", "G91 G1 X1 Y1", "G90 G2 X1 Y-1 I0 J-1"])
    assert b[0] == pytest.approx(0) and b[1] == pytest.approx(-25.4) and b[3] == pytest.approx(50.8)   # círculo inteiro
    assert b[5] == pytest.approx(2.54)


def test_envelope_problems():
    s = {22: 1, 23: 0, 130: 300, 131: 180, 132: 45}
    ok = envelope_problems((0, 0, -2, 100, 50, 5), (-250, -150, -10), s)
    assert ok == []
    bad = envelope_problems((0, 0, -2, 100, 50, 5), (-50, -150, -10), s)
    assert len(bad) == 1 and bad[0].startswith("Eixo X")
    assert envelope_problems((0, 0, 0, 1, 1, 1), (0, 0, 0), {22: 0, 130: 1}) == []   # sem homing
    inv = envelope_problems((0, 0, 0, 10, 10, 1), (5, 5, -2), {22: 1, 23: 3, 130: 300, 131: 180, 132: 45})
    assert inv == []


def test_stream_resume_from_line(machine):
    ctl, sim = machine()
    ctl.echo_stream = False
    done = []
    ctl.on_stream_done = done.append
    ctl.stream_program(PROG, start_line=6, safe_z=3.0)
    assert ctl.wait_stream(10) and done == [True]
    ex = [ln for ln in sim.executed_lines if not ln.startswith("$")]
    assert ex[0].startswith("(MSG") and ex[1].startswith("G21 G90 G17 G55")
    assert "M3 S12000" in ex and ex[-1] == "G1 X30 Y9"
    assert "G1 X20 F300" in ex and "G0 X10 Y5" in ex   # preâmbulo levou a ferramenta ao ponto certo


def test_stream_m6_pauses_instead_of_error(machine):
    ctl, sim = machine()
    errors = []
    ctl.on_error = lambda c, m, ln: errors.append(c)
    ctl.stream_program(["M3 S1000", "G1 X1 F100", "T2 M6", "G1 X2"], m6_pause=True)
    assert wait_for(lambda: ctl.state.startswith("Hold"), 5)       # M0: pausa para trocar
    assert errors == []
    ctl.resume()
    assert ctl.wait_stream(10)
    assert sim.executed_lines[-1] == "G1 X2" and errors == []


def test_safe_stop_keeps_position_without_alarm(machine):
    ctl, sim = machine(speed=1.0)
    alarms = []
    ctl.on_alarm = lambda c, m: alarms.append(c)
    done = []
    ctl.on_stream_done = done.append
    ctl.stream_program(["G1 F300", "G1 X50", "G1 Y50"])
    assert wait_for(lambda: sim.state == "Run" and sim.mpos[0] > 0.5, 5)
    assert ctl.safe_stop(timeout=5) is True
    assert done == [False]
    assert wait_for(lambda: ctl.state == "Idle", 5)
    time.sleep(0.2)
    assert alarms == []                                # sem ALARM:3: posição preservada
    x = sim.mpos[0]
    time.sleep(0.2)
    assert sim.mpos[0] == x and 0 < x < 50
