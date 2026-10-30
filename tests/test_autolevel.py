"""Heightmap/autolevel, comandos de posição e offset Z."""
import math

import numpy as np
import pytest

from brokcam.gcode.autolevel import (
    HeightMap, apply_heightmap, grid_axes, grid_counts_for_spacing, probe_order,
)
from brokcam.gcode.preview import parse_gcode
from brokcam.machine.grbl import GrblController, GrblError
from brokcam.machine.simulator import GrblSimulator


def tilted(x, y):
    """Placa inclinada + ondulação (Z de máquina da superfície)."""
    return -20.0 + 0.004 * x - 0.003 * y + 0.02 * math.sin(x / 10.0)


@pytest.fixture()
def ctrl():
    sim = GrblSimulator(speed=math.inf)
    sim.probe_surface = tilted
    c = GrblController()
    c.connect("SIMULADOR", transport=sim)
    yield c
    c.disconnect()


# --------------------------------------------------------------------------- mapa

def test_bilinear_is_exact_on_planes():
    xs, ys = grid_axes(0, 0, 30, 20, 4, 3)
    X, Y = np.meshgrid(xs, ys)
    hm = HeightMap(xs, ys, 0.01 * X - 0.02 * Y + 0.5)
    for x, y in [(0, 0), (7.3, 11.1), (30, 20), (15, 5)]:
        assert hm.at(x, y) == pytest.approx(0.01 * x - 0.02 * y + 0.5)
    assert hm.at(-10, -10) == pytest.approx(hm.at(0, 0))          # fora da grade: borda


def test_grid_helpers():
    assert grid_counts_for_spacing(0, 0, 30, 20, 10) == (4, 3)
    order = probe_order(np.arange(3), np.arange(2))
    assert order == [(0, 0), (0, 1), (0, 2), (1, 2), (1, 1), (1, 0)]


def test_json_roundtrip(tmp_path):
    xs, ys = grid_axes(0, 0, 10, 10, 3, 3)
    hm = HeightMap(xs, ys, np.arange(9.0).reshape(3, 3) / 100)
    hm.save(str(tmp_path / "h.json"))
    hm2 = HeightMap.load(str(tmp_path / "h.json"))
    assert np.allclose(hm2.z, hm.z) and np.allclose(hm2.xs, hm.xs)


# --------------------------------------------------------------------------- aplicação

def test_apply_heightmap_segments_and_corrects():
    xs, ys = grid_axes(0, 0, 20, 20, 3, 3)
    X, Y = np.meshgrid(xs, ys)
    hm = HeightMap(xs, ys, 0.01 * X)                 # sobe 0,01 mm por mm em X
    g = "G21 G90\nG0 Z1\nG0 X0 Y0\nG1 Z-0.1 F60\nG1 X20 Y0 F120\n(comentário Z9)\nM5\n"
    res = apply_heightmap(g, hm, segment=1.0)
    pv = parse_gcode(res.text)
    cuts = pv.feeds.reshape(-1, 3)
    at_end = cuts[np.isclose(cuts[:, 0], 20)]
    assert at_end[:, 2].max() == pytest.approx(-0.1 + 0.2, abs=1e-6)
    assert res.lines_out > 20                       # 20 mm em segmentos de 1 mm
    assert "(comentário Z9)" in res.text and "M5" in res.text
    assert res.max_correction == pytest.approx(0.2, abs=1e-6)


def test_apply_heightmap_arcs_and_reference():
    xs, ys = grid_axes(-10, -10, 10, 10, 3, 3)
    hm = HeightMap(xs, ys, np.full((3, 3), 0.3))     # placa plana 0,3 mm acima
    g = "G21 G90\nG0 X5 Y0 Z1\nG1 Z-0.05 F50\nG2 X5 Y0 I-5 J0 F100\n"
    res = apply_heightmap(g, hm, segment=0.5)
    pv = parse_gcode(res.text)
    # referência no zero: placa plana => correção nula
    assert abs(res.max_correction) < 1e-9 and abs(res.min_correction) < 1e-9
    r = np.hypot(pv.feeds[:, 1, 0], pv.feeds[:, 1, 1])
    assert np.allclose(r[r > 1], 5.0, atol=0.02)


def test_apply_rejects_inches_and_incomplete():
    xs, ys = grid_axes(0, 0, 1, 1, 2, 2)
    with pytest.raises(ValueError):
        apply_heightmap("G20\nG1 X1\n", HeightMap(xs, ys, np.zeros((2, 2))))
    with pytest.raises(ValueError):
        apply_heightmap("G1 X1\n", HeightMap(xs, ys, np.array([[0, np.nan], [0, 0]])))


# --------------------------------------------------------------------------- máquina (simulador)

def test_probe_grid_measures_warped_board(ctrl):
    ctrl.set_position(x=0, y=0, z=5)                 # ponta 5 mm acima do zero declarado
    xs, ys = grid_axes(0, 0, 40, 30, 3, 3)
    pts = [(xs[i], ys[j]) for j, i in probe_order(xs, ys)]
    zs = ctrl.probe_grid(pts, clearance=2.0, depth=25.0, feed=200)
    off = ctrl.work_offset(fresh=True)
    expected = [tilted(x + off[0], y + off[1]) - off[2] for x, y in pts]
    assert np.allclose(zs, expected, atol=0.01)


def test_probe_point_without_contact_raises(ctrl):
    ctrl.set_position(x=0, y=0, z=0)
    with pytest.raises(GrblError):
        ctrl.probe_point(depth=1.0, feed=200)       # superfície ~20 mm abaixo


def test_set_position_goto_and_adjust_z(ctrl):
    ctrl.set_position(x=10, y=20, z=3)
    ctrl.wait_idle(5)
    ctrl.goto(x=0, y=0, safe_z=4)
    ctrl.wait_idle(5)
    ctrl.send_command("?", wait=False)
    import time
    t0 = time.time()
    while time.time() - t0 < 2 and (ctrl.status is None or ctrl.status.wpos is None
                                    or abs(ctrl.status.wpos[0]) > 1e-3):
        time.sleep(0.02)
    assert ctrl.status.wpos[0] == pytest.approx(0, abs=1e-3)
    z_before = ctrl.status.wpos[2]
    ctrl.adjust_work_z(0.05)                         # cortar 0,05 mm mais fundo
    t0 = time.time()
    while time.time() - t0 < 2 and abs(ctrl.status.wpos[2] - (z_before + 0.05)) > 1e-3:
        time.sleep(0.02)
    assert ctrl.status.wpos[2] == pytest.approx(z_before + 0.05, abs=1e-3)
    ctrl.select_wcs(2)
    ctrl.select_wcs(1)
    ctrl.spindle(True, 8000)
    ctrl.spindle(False)
    ctrl.coolant(flood=True)
    ctrl.coolant(flood=False)
    assert len(ctrl.run_lines("G90 (macro)\nG0 Z5 ; sobe\n")) == 2


def test_post_z_offset():
    from brokcam.core.model import CutParams, Job, PathOp, Tool
    from brokcam.gcode.grbl import generate_gcode
    job = Job("j", 0, [PathOp("mill", Tool(0.2), CutParams(cut_z=-0.05, travel_z=1.0),
                              paths=[np.array([[0, 0], [5, 0]], float)])])
    a = generate_gcode(job, {"z_offset": 0.0})
    b = generate_gcode(job, {"z_offset": -0.02})
    za = [float(w[1:]) for ln in a.splitlines() if not ln.startswith("(") for w in ln.split() if w.startswith("Z")]
    zb = [float(w[1:]) for ln in b.splitlines() if not ln.startswith("(") for w in ln.split() if w.startswith("Z")]
    assert np.allclose(np.array(zb), np.array(za) - 0.02)
