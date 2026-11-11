"""Importação de G-code → caminhos editáveis + geometria (``brokcam.gcode.importer``)."""
from __future__ import annotations

from pathlib import Path

import numpy as np
import pytest

from brokcam.cam.cutout import cutout
from brokcam.cam.drilling import drill
from brokcam.cam.isolation import isolate
from brokcam.gcode.grbl import generate_gcode
from brokcam.gcode.importer import expand_canned_cycles, import_gcode, import_gcode_file
from brokcam.parsers import load_file

DATA = Path(__file__).parent / "data"


def _roundtrip(job):
    return import_gcode(generate_gcode(job, {}), "x.nc")


def test_isolation_roundtrip_keeps_paths_tool_and_depth():
    j = isolate(load_file(str(DATA / "board-F_Cu.gbr")), {})
    r = _roundtrip(j)
    (op,) = r.job.ops
    assert op.kind == "mill" and op.tool.shape == "vbit" and abs(op.tool.diameter - 0.15) < 1e-3
    assert len(op.paths) == len(j.ops[0].paths)
    assert abs(r.job.stats()["cut_length_mm"] - j.stats()["cut_length_mm"]) < 0.05
    assert abs(op.params.cut_z - j.ops[0].params.cut_z) < 1e-3 and op.params.feed_xy == 120.0
    assert r.job.settings["edited"] and r.job.settings["imported"] and r.job.gcode
    assert r.geometry is not None and r.job.source_id == r.geometry.id
    assert not r.geometry.geometry.is_empty


def test_multipass_cutout_is_merged_into_multidepth():
    j = cutout(load_file(str(DATA / "board-Edge_Cuts.gbr")), {})
    r = _roundtrip(j)
    (op,) = r.job.ops
    assert len(op.paths) == len(j.ops[0].paths)
    assert op.params.multi_depth and op.params.depth_per_pass == pytest.approx(j.ops[0].params.depth_per_pass)
    assert op.params.cut_z == pytest.approx(j.ops[0].params.cut_z)
    assert abs(r.job.stats()["cut_length_mm"] - j.stats()["cut_length_mm"]) < 0.05
    # o G-code regerado volta a descer em etapas
    g = generate_gcode(r.job, {})
    assert g.count("Z-0.4000") >= 1 and g.count("Z-1.7000") >= 1


def test_drill_roundtrip_one_op_per_bit_and_peck():
    lay = load_file(str(DATA / "board-PTH.drl"))
    j = drill(lay, {"peck": True, "peck_depth": 0.5})
    r = _roundtrip(j)
    assert [(o.kind, o.tool.number, o.tool.diameter, len(o.points)) for o in r.job.ops] == \
        [(o.kind, o.tool.number, o.tool.diameter, len(o.points)) for o in j.ops]
    assert all(o.params.multi_depth and o.params.depth_per_pass == 0.5 for o in r.job.ops)
    for a, b in zip(j.ops, r.job.ops):
        assert np.allclose(np.sort(a.points, axis=0), np.sort(b.points, axis=0))


def test_foreign_gcode_inch_canned_cycles_and_comments():
    txt = "(Tool Dia: 0.9)\nG20 G90\nG0 Z0.1\nT1 M6\nG81 X0.1 Y0.1 Z-0.07 R0.04 F5\nX0.2 Y0.1\nX0.3\nG80\nM30\n"
    out, changed = expand_canned_cycles(txt)
    assert changed and "G81" not in out.replace("ciclo G81", "")
    r = import_gcode(txt, "fc.nc")
    (op,) = r.job.ops
    assert op.kind == "drill" and op.tool.diameter == 0.9
    assert np.allclose(op.points, [[2.54, 2.54], [5.08, 2.54], [7.62, 2.54]])
    assert op.params.cut_z == pytest.approx(-1.778)


def test_laser_and_arcs():
    txt = "G21 G90\nM4 S800\nG0 X0 Y0\nG1 X10 Y0 F1000\nG2 X20 Y0 I5 J0\nG0 X0 Y5\nG1 X10 Y5\nM5\n"
    r = import_gcode(txt, "laser.nc", {"default_tool_dia": 0.1})
    (op,) = r.job.ops
    assert op.params.laser_mode and op.params.laser_power == 800 and op.params.feed_xy == 1000
    assert len(op.paths) == 2 and op.tool.diameter == 0.1
    assert r.job.stats()["cut_length_mm"] == pytest.approx(10 + 5 * np.pi + 10, abs=0.05)


def test_depth_levels_become_separate_ops_same_tool():
    txt = ("G21 G90\nM3 S9000\nG0 Z1\nG0 X0 Y0\nG1 Z-0.1 F50\nG1 X5 F200\nG0 Z1\n"
           "G0 X0 Y3\nG1 Z-0.5 F50\nG1 X5 F200\nG0 Z1\nM5\n")
    r = import_gcode(txt, "z.nc")
    assert [(o.params.cut_z, o.tool.number, o.params.spindle_rpm) for o in r.job.ops] == [(-0.1, 1, 9000), (-0.5, 1, 9000)]
    g = generate_gcode(r.job, {})
    assert "M0" not in g.split()                          # mesma ferramenta: sem pausa de troca


def test_empty_or_no_cuts_raises(tmp_path):
    with pytest.raises(ValueError):
        import_gcode("G0 X1 Y1\n", "a.nc")
    with pytest.raises(ValueError):
        import_gcode("(nada)\n", "b.nc")
    p = tmp_path / "c.nc"
    p.write_text("G21\nG0 Z1\nG0 X0 Y0\nG1 Z-0.2 F60\nG1 X3 Y0 F100\nG0 Z1\n")
    r = import_gcode_file(str(p))
    assert r.job.settings["source_file"] == str(p.resolve()) and r.job.name == "c.nc (importado)"
