"""Biblioteca de ferramentas, edição manual de caminhos e serialização de jobs."""
import numpy as np
import pytest

from brokcam.cam import edit
from brokcam.cam.library import TOOL_ENTRY, LibraryTool, ToolLibrary, default_tools
from brokcam.cam.operations import OPERATIONS
from brokcam.core.model import CutParams, PathOp, Tool
from brokcam.core.serialize import ops_from_json, ops_to_json


# --------------------------------------------------------------------------- biblioteca

def test_default_library_has_all_kinds():
    lib = ToolLibrary()
    shapes = {t.shape for t in lib}
    assert {"vbit", "flat", "drill", "laser"} <= shapes
    assert lib.for_operation("drilling") and all("drilling" in t.operations for t in lib.for_operation("drilling"))
    assert not any(t.shape == "drill" for t in lib.for_operation("isolation"))


def test_every_library_field_is_explained():
    for spec in TOOL_ENTRY.params:
        assert len(spec.help) > 40, spec.key


def test_library_roundtrip(tmp_path):
    lib = ToolLibrary(path=tmp_path / "tools.json")
    t = lib.add(LibraryTool(name="Minha V 20°", shape="vbit", vbit_angle=20, vbit_tip=0.1, cut_z=-0.07))
    lib.duplicate(t.uid)
    lib.save()
    lib2 = ToolLibrary.load(tmp_path / "tools.json")
    assert [x.to_json() for x in lib2] == [x.to_json() for x in lib]
    assert lib2.get(t.uid).name == "Minha V 20°"


def test_library_import_export(tmp_path):
    lib = ToolLibrary()
    uid = lib.tools[0].uid
    lib.export_file(tmp_path / "x.json", [uid])
    other = ToolLibrary(tools=[])
    assert other.import_file(tmp_path / "x.json") == 1
    assert other.tools[0].uid == uid
    with pytest.raises(ValueError):
        (tmp_path / "bad.json").write_text('{"a": 1}', encoding="utf-8")
        other.import_file(tmp_path / "bad.json")


def test_form_values_roundtrip():
    t = LibraryTool(name="X", shape="flat", diameter=0.8, operations=["ncc", "cutout"])
    back = LibraryTool.from_form_values(TOOL_ENTRY.validate(t.form_values()), uid=t.uid)
    assert back.to_json() == t.to_json()


def test_apply_to_isolation_uses_vbit_geometry():
    op = OPERATIONS["isolation"]
    t = LibraryTool(name="V 20", shape="vbit", vbit_angle=20, vbit_tip=0.05, cut_z=-0.08,
                    feed_xy=90, spindle_rpm=11000, number=4)
    s = t.apply("isolation", op.group.defaults(), op.group)
    assert s["tool_shape"] == "vbit" and s["vbit_angle"] == 20 and s["vbit_tip"] == 0.05
    # Z da V-bit agora é derivado da largura do sulco (arredondada a 0,001 mm): difere < 1 µm
    assert s["cut_z"] == pytest.approx(-0.08, abs=1e-3) and s["feed_xy"] == 90 and s["tool_number"] == 4
    assert s["vbit_width"] == t.vbit_width
    assert s["library_tool"] == t.uid
    job = op.func(_square_layer(), {k: v for k, v in s.items() if k != "library_tool"})
    assert job.ops[0].tool.shape == "vbit"


def test_apply_to_drilling_lists_library_drills():
    op = OPERATIONS["drilling"]
    lib = ToolLibrary()
    drill = lib.for_operation("drilling")[0]
    drill = next(t for t in lib if t.shape == "drill")
    s = drill.apply("drilling", op.group.defaults(), op.group, library=lib)
    listed = {float(x) for x in s["available_drills"].split(",")}
    assert listed == {round(t.diameter, 3) for t in lib if t.shape == "drill"}
    assert set(s) <= set(op.group.defaults()) | {"library_tool"}


def test_apply_laser_sets_laser_mode():
    op = OPERATIONS["cutout"]
    laser = next(t for t in default_tools() if t.shape == "laser")
    s = laser.apply("cutout", op.group.defaults(), op.group)
    assert s["laser_mode"] is True


def _square_layer():
    from shapely.geometry import box
    from brokcam.core.model import GeometryLayer
    return GeometryLayer("sq", box(0, 0, 5, 5))


# --------------------------------------------------------------------------- edição

def _ops():
    ring = np.array([[0, 0], [10, 0], [10, 10], [0, 10], [0, 0]], float)
    line = np.array([[20, 0], [30, 0]], float)
    far = np.array([[50, 50], [60, 50]], float)
    mill = PathOp("mill", Tool(0.2), CutParams(), paths=[ring, line, far])
    drill = PathOp("drill", Tool(0.8, shape="drill"), CutParams(cut_z=-1.8),
                   points=np.array([[5.0, 5.0], [40.0, 40.0]]))
    return [mill, drill]


def test_pick_path_and_hole():
    ops = _ops()
    assert edit.pick(ops, 10.05, 5, 0.2) == (0, "path", 0)
    assert edit.pick(ops, 25, 0.1, 0.2) == (0, "path", 1)
    assert edit.pick(ops, 5.1, 5.1, 0.2) == (1, "point", 0)
    assert edit.pick(ops, 100, 100, 0.5) is None


def test_select_rect_modes():
    ops = _ops()
    touch = edit.select_rect(ops, 8, -1, 22, 1, "touch")
    assert (0, "path", 0) in touch and (0, "path", 1) in touch
    inside = edit.select_rect(ops, 8, -1, 22, 1, "inside")
    assert inside == set()
    inside = edit.select_rect(ops, 19, -1, 45, 45, "inside")
    assert inside == {(0, "path", 1), (1, "point", 1)}


def test_delete_does_not_mutate_and_drops_empty():
    ops = _ops()
    new = edit.delete(ops, [(0, "path", 1), (1, "point", 0), (1, "point", 1)])
    assert len(ops[0].paths) == 3 and len(ops[1].points) == 2      # original intacto
    assert len(new) == 1 and len(new[0].paths) == 2                 # op de furos vazia some
    assert edit.count(new) == {"paths": 2, "points": 0}


def test_keep_only():
    ops = _ops()
    new = edit.keep_only(ops, [(0, "path", 2)])
    assert edit.count(new) == {"paths": 1, "points": 0}


def test_trim_rect_splits_ring_into_one_open_path():
    ops = _ops()
    # apaga o meio do lado de baixo do quadrado (x 4..6, y -1..1)
    new = edit.trim_rect(ops, 4, -1, 6, 1)
    ring_parts = [p for p in new[0].paths if p[:, 0].max() <= 10.001]
    assert len(ring_parts) == 1
    part = ring_parts[0]
    assert np.allclose(part[0], [6, 0]) and np.allclose(part[-1], [4, 0])
    length = np.hypot(*np.diff(part, axis=0).T).sum()
    assert length == pytest.approx(38.0)
    # sentido preservado: anti-horário (área com sinal positivo do polígono fechado)
    x, y = part[:, 0], part[:, 1]
    assert 0.5 * np.sum(x[:-1] * y[1:] - x[1:] * y[:-1]) > 0
    # furo dentro? (5,5) não está no retângulo → mantido
    assert len(new[1].points) == 2


def test_trim_rect_removes_holes_and_whole_paths():
    ops = _ops()
    new = edit.trim_rect(ops, 19, -1, 45, 45)
    assert edit.count(new) == {"paths": 2, "points": 1}


def test_reverse_and_reoptimize():
    ops = _ops()
    rev = edit.reverse(ops, [(0, "path", 1)])
    assert np.allclose(rev[0].paths[1][0], [30, 0])
    opt = edit.reoptimize(ops)
    assert edit.count(opt) == edit.count(ops)


def test_edit_performance():
    rng = np.random.default_rng(1)
    paths = [np.cumsum(rng.normal(0, 0.3, (40, 2)), axis=0) + rng.uniform(0, 200, 2) for _ in range(3000)]
    ops = [PathOp("mill", Tool(0.2), CutParams(), paths=paths)]
    import time
    idx = edit.SegmentIndex.build(ops)
    t0 = time.perf_counter()
    for _ in range(20):
        edit.pick(ops, 100, 100, 0.5, idx)
    assert (time.perf_counter() - t0) / 20 < 0.05
    t0 = time.perf_counter()
    edit.trim_rect(ops, 50, 50, 150, 150)
    assert time.perf_counter() - t0 < 3.0


# --------------------------------------------------------------------------- serialização

def test_ops_json_roundtrip():
    ops = _ops()
    back = ops_from_json(ops_to_json(ops))
    assert len(back) == 2 and back[1].kind == "drill"
    assert np.allclose(back[0].paths[0], ops[0].paths[0])
    assert np.allclose(back[1].points, ops[1].points)
    assert back[1].tool.shape == "drill" and back[1].params.cut_z == -1.8
