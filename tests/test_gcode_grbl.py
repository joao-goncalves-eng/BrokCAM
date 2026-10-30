"""Testes do pós-processador GRBL e do interpretador de pré-visualização."""
from __future__ import annotations

import re
import time

import numpy as np
import pytest

from brokcam.core.model import CutParams, Job, PathOp, Tool
from brokcam.core.params import get_group
from brokcam.gcode import GRBL_POST, generate_gcode, generate_program, parse_gcode

SQUARE = np.array([[0, 0], [10, 0], [10, 10], [0, 10], [0, 0]], dtype=float)
OPEN = np.array([[20, 0], [25, 0], [25, 3]], dtype=float)
VBIT = Tool(0.2, "V-bit", 1, "vbit")
DRILL = Tool(0.8, "Broca 0.8", 2, "drill")


def mill_job(**params) -> Job:
    p = CutParams(**params)
    return Job("iso", 1, [PathOp("mill", VBIT, p, paths=[SQUARE, OPEN], label="iso")])


def drill_job(**params) -> Job:
    p = CutParams(cut_z=-1.8, **params)
    pts = np.array([[1, 1], [5, 5], [9, 1]], dtype=float)
    return Job("furos", 2, [PathOp("drill", DRILL, p, points=pts)])


def cmd_lines(text: str) -> list[str]:
    return [ln for ln in text.splitlines() if ln and not ln.startswith("(")]


def test_group_registered_and_documented():
    assert get_group("grbl_post") is GRBL_POST
    for spec in GRBL_POST.params:
        assert len(spec.help) > 40, spec.key
    keys = {p.key for p in GRBL_POST.params}
    for k in ("variant", "coord_decimals", "feed_decimals", "start_gcode", "end_gcode", "end_z",
              "toolchange_z", "toolchange_mode", "spindle_warmup_s", "comments", "rapid_z_g0",
              "return_home", "offset_x", "offset_y", "mirror_x", "max_line_length", "line_numbers",
              "compact", "avoid_retract"):
        assert k in keys


def test_header_and_no_unsupported_codes():
    g = generate_gcode(mill_job())
    lines = cmd_lines(g)
    assert lines[0] == "G21 G90 G94 G17"
    for bad in ("G83", "G81", "G41", "G42", "M6"):
        assert not any(re.search(rf"\b{bad}\b", ln) for ln in lines), bad
    assert lines[-1] == "M2"
    assert "M5" in lines


def test_multidepth_passes_and_rapids_only_above_travel():
    g = generate_gcode(mill_job(cut_z=-0.25, multi_depth=True, depth_per_pass=0.1, travel_z=1.0))
    z_cuts = re.findall(r"G1 Z(-?[\d.]+)", g)
    assert [float(z) for z in z_cuts][:3] == [-0.1, -0.2, -0.25]
    # G0 em XY só com Z >= travel_z
    pv = parse_gcode(g)
    for seg in pv.rapids:
        if np.any(seg[0, :2] != seg[1, :2]):
            assert seg[0, 2] >= 1.0 - 1e-9 and seg[1, 2] >= 1.0 - 1e-9


def test_open_path_alternates_direction():
    g = generate_gcode(mill_job(cut_z=-0.2, multi_depth=True, depth_per_pass=0.1))
    seg = g.split("G0 X20.0000")[1]
    # 2º passe do caminho aberto volta pelo caminho invertido (sem retrair)
    after_second_plunge = seg.split("G1 Z-0.2000")[1].splitlines()[1:3]
    assert after_second_plunge[0].startswith("G1 Y0.0000")


def test_drill_expansion_and_peck():
    g = generate_gcode(drill_job())
    lines = cmd_lines(g)
    assert sum(1 for ln in lines if ln.startswith("G1 Z-1.8")) == 3
    g2 = generate_gcode(drill_job(multi_depth=True, depth_per_pass=0.6))
    assert len(re.findall(r"G1 Z-0\.6", g2)) == 3
    assert len(re.findall(r"G1 Z-1\.8", g2)) == 3
    assert "G83" not in g2


def test_toolchange_between_jobs():
    g = generate_program([mill_job(), drill_job()])
    lines = cmd_lines(g)
    assert "T2" in lines and "M0" in lines and "M6" not in lines
    assert any(ln.startswith("(MSG,") for ln in g.splitlines())
    i = lines.index("M0")
    assert "G0 Z15.0000" in lines[i - 4:i]
    g6 = generate_program([mill_job(), drill_job()], {"toolchange_mode": "m6_m0"})
    assert "M6" in cmd_lines(g6)
    gn = generate_program([mill_job(), drill_job()], {"variant": "grbl_no_toolchange"})
    assert "M0" not in cmd_lines(gn)


def test_spindle_warmup_dwell():
    g = generate_gcode(mill_job(), {"spindle_warmup_s": 2.5})
    lines = cmd_lines(g)
    i = next(i for i, ln in enumerate(lines) if ln.startswith("M3"))
    assert lines[i + 1] == "G4 P2.5"


def test_laser_variant():
    g = generate_gcode(mill_job(laser_power=800), {"variant": "grbl_laser"})
    lines = cmd_lines(g)
    assert not any("Z" in ln for ln in lines)
    assert "M4 S800.0" in lines
    assert "M3" not in " ".join(lines)
    assert "M5" in lines
    g2 = generate_gcode(mill_job(laser_power=800), {"variant": "grbl_laser", "laser_off_mode": "m5"})
    assert g2.count("M4 S800.0") == 2


def test_offset_mirror_and_bounds():
    g = generate_gcode(mill_job(), {"mirror_x": True, "mirror_axis_x": 15.0, "offset_y": 5})
    pv = parse_gcode(g)
    feeds = pv.feeds.reshape(-1, 3)
    assert feeds[:, 0].min() == pytest.approx(5.0)
    assert feeds[:, 0].max() == pytest.approx(30.0)
    assert feeds[:, 1].min() == pytest.approx(5.0)


def test_compact_line_numbers_and_length():
    g = generate_gcode(mill_job(), {"compact": True, "line_numbers": True, "comments": False})
    lines = g.splitlines()
    assert all(ln.startswith("N") for ln in lines)
    assert all(" " not in ln for ln in lines)
    with pytest.raises(ValueError):
        generate_gcode(mill_job(), {"start_gcode": "G0 " + "X1 " * 40, "max_line_length": 60})


def test_avoid_retract():
    a = np.array([[0, 0], [5, 0]], float)
    b = np.array([[5.1, 0], [9, 0]], float)
    job = Job("j", 1, [PathOp("mill", VBIT, CutParams(), paths=[a, b])])
    g_on = generate_gcode(job, {"avoid_retract": True})
    g_off = generate_gcode(job, {"avoid_retract": False})
    assert cmd_lines(g_on).count("G0 Z1.0000") < cmd_lines(g_off).count("G0 Z1.0000")


def test_preview_roundtrip_and_time():
    g = generate_gcode(mill_job(feed_xy=600))
    pv = parse_gcode(g)
    assert pv.feeds.shape[1:] == (2, 3)
    assert pv.cut_length_mm > 40
    assert pv.estimate_time_s > 0
    assert len(pv.line_of_segment) == len(pv.segments)
    text_lines = g.splitlines()
    for li in pv.feed_lines[:5]:
        assert text_lines[li].startswith("G1")
    pv_acc = parse_gcode(g, accel_mm_s2=50.0)
    assert pv_acc.estimate_time_s > pv.estimate_time_s


def test_preview_arcs_incremental_inches_comments():
    text = "\n".join([
        "G21 G90 (comentário)",
        "G0 X0 Y0 Z1 ; ok",
        "G2 X10 Y0 I5 J0 F100",
        "G91",
        "G1 X1",
        "G20 G1 X1",
        "T3 M6",
    ])
    pv = parse_gcode(text)
    arc = pv.feeds[pv.feed_lines == 2]
    assert len(arc) > 10
    assert arc[:, :, 1].max() == pytest.approx(5.0, abs=0.05)   # G2 de (0,0) a (10,0) passa por y=+5
    assert pv.feeds[-1, 1, 0] == pytest.approx(10 + 1 + 25.4)
    assert pv.tool_changes == [(6, 3)]


def test_preview_dwell_time():
    pv = parse_gcode("G1 X10 F600\nG4 P2\nG1 X20")
    assert pv.estimate_time_s == pytest.approx(1 + 2 + 1)
    assert pv.time_at_line(0) == pytest.approx(1)


def test_benchmark_500k_segments():
    rng = np.random.default_rng(1)
    paths = [np.cumsum(rng.random((501, 2)), axis=0) for _ in range(1000)]
    job = Job("bench", 1, [PathOp("mill", VBIT, CutParams(), paths=paths)])
    t = time.perf_counter()
    g = generate_gcode(job)
    dt = time.perf_counter() - t
    assert g.count("\n") > 500_000
    assert dt < 2.0, f"geração levou {dt:.2f}s"


def test_cam_conventions():
    # mesmo tool.number em PathOps consecutivos -> sem troca, mesmo entre jobs
    fresa = Tool(2.0, "Fresa", 3, "flat")
    cut = PathOp("mill", fresa, CutParams(cut_z=-1.6, multi_depth=True, depth_per_pass=0.6), paths=[SQUARE])
    bridges = PathOp("mill", Tool(2.0, "Fresa", 3, "flat"), CutParams(cut_z=-0.5), paths=[OPEN])
    j1 = Job("recorte", 1, [cut, bridges], settings={"warnings": ["Contorno aberto corrigido"]})
    j2 = Job("recorte2", 1, [PathOp("mill", fresa, CutParams(), paths=[SQUARE])])
    g = generate_program([j1, j2])
    assert "M0" not in cmd_lines(g)
    assert "(AVISO: Contorno aberto corrigido)" in g
    # anel fechado mantém a orientação (não invertido) nos passes
    seq = [ln for ln in cmd_lines(g) if ln.startswith(("G1 X", "G1 Y"))][:8]
    assert seq[:4] == ["G1 X10.0000 F120.0", "G1 Y10.0000", "G1 X0.0000", "G1 Y0.0000"]
    assert seq[4:8] == seq[:4] or seq[4] == "G1 X10.0000"
    # Z de troca vindo de job.settings["toolchange_z"]
    dj = drill_job()
    dj.settings["toolchange_z"] = 33.0
    lines = cmd_lines(generate_program([mill_job(), dj]))
    i = lines.index("M0")
    assert "G0 Z33.0000" in lines[i - 4:i]


def test_same_number_different_tool_triggers_toolchange():
    """T1 V-bit seguido de T1 broca é outra ferramenta física: deve pausar para troca."""
    import numpy as np
    from brokcam.core.model import CutParams, Job, PathOp, Tool
    from brokcam.gcode.grbl import generate_program
    iso = PathOp("mill", Tool(0.15, "V-bit", 1, "vbit"), CutParams(),
                 paths=[np.array([[0, 0], [5, 0], [5, 5], [0, 0]], float)])
    drl = PathOp("drill", Tool(0.8, "Broca", 1, "drill"), CutParams(cut_z=-1.8),
                 points=np.array([[1.0, 1.0]]))
    g = generate_program([Job("a", 0, [iso]), Job("b", 0, [drl])])
    assert "M0" in g.splitlines()
