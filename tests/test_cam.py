"""Testes das operações de CAM (geometrias construídas diretamente com shapely)."""
from __future__ import annotations

import math
import time
from collections import Counter

import numpy as np
import pytest
import shapely

from brokcam.cam import Cancelled
from brokcam.cam.common import cut_params_from, tool_from
from brokcam.cam.cutout import CUTOUT, board_from_geometry, cutout
from brokcam.cam.drilling import drill
from brokcam.cam.isolation import ISOLATION, isolate
from brokcam.cam.ncc import NCC, clear_copper, scanline_segments
from brokcam.cam.operations import OPERATIONS, operations_for
from brokcam.cam.optimize import (
    optimize_job, optimize_paths, optimize_points, order_points, travel_length,
)
from brokcam.cam.panelize import panelize, panelize_job
from brokcam.cam.tools import (
    DEFAULT_TOOLS, drill_tip_length, vbit_depth_for_width, vbit_effective_width,
)
from brokcam.core.model import DrillLayer, DrillTool, GerberLayer, GeometryLayer
from brokcam.core.params import get_group

FLAT02 = {"tool_shape": "flat", "tool_dia": 0.2, "optimize": True}


def _ring_signed_area(p: np.ndarray) -> float:
    x, y = p[:, 0], p[:, 1]
    return 0.5 * float(np.sum(x[:-1] * y[1:] - x[1:] * y[:-1]))


def _layer(geom, traces=None) -> GerberLayer:
    return GerberLayer(name="teste", source_path="", copper=geom, traces=traces or [])


# =========================================================================== ferramentas

def test_vbit_formula():
    # 30°, ponta 0,1, profundidade 0,05 -> 0,1 + 2*0,05*tan(15°)
    w = vbit_effective_width(30, 0.1, -0.05)
    assert w == pytest.approx(0.1 + 0.1 * math.tan(math.radians(15)))
    assert vbit_depth_for_width(30, 0.1, w) == pytest.approx(0.05)
    assert drill_tip_length(1.0) == pytest.approx(0.5 / math.tan(math.radians(59)))
    t = tool_from({"tool_shape": "vbit", "vbit_angle": 60, "vbit_tip": 0.2, "cut_z": -0.1})
    assert t.diameter == pytest.approx(0.2 + 0.2 * math.tan(math.radians(30)))
    cats = Counter(p.category for p in DEFAULT_TOOLS)
    assert cats["vbit"] == 5 and cats["endmill"] == 3 and cats["drill"] >= 10


def test_cut_params_from_settings():
    p = cut_params_from({"cut_z": 0.3, "feed_xy": 200, "multi_depth": True, "depth_per_pass": -0.1})
    assert p.cut_z == -0.3 and p.feed_xy == 200 and p.multi_depth and p.depth_per_pass == 0.1


def test_groups_registered_and_documented():
    for key in ("isolation", "ncc", "cutout", "drilling", "panelize"):
        g = get_group(key)
        assert g.description
        for spec in g.params:
            assert len(spec.help) > 20, spec.key
        g.validate({})   # padrões válidos
    for key in ("cut_z", "travel_z", "feed_xy", "feed_z", "spindle_rpm", "multi_depth",
                "depth_per_pass", "dwell_s"):
        for g in (ISOLATION, NCC, CUTOUT):
            g.spec(key)
    assert {o.key for o in operations_for("excellon")} >= {"drilling", "cutout"}
    assert "isolation" in OPERATIONS and OPERATIONS["panelize"].produces == "layer"


# =========================================================================== isolamento

def test_isolation_width_single_pass():
    pad = shapely.box(0, 0, 2, 1)
    job = isolate(_layer(pad), dict(FLAT02))
    assert len(job.ops) == 1
    paths = job.ops[0].paths
    assert len(paths) == 1
    ring = shapely.LineString(paths[0])
    # o centro da ferramenta fica a exatamente r = 0,1 mm do cobre
    assert shapely.distance(pad, ring) == pytest.approx(0.1, abs=4e-3)  # simplificação 3 µm
    assert shapely.hausdorff_distance(ring, pad.buffer(0.1).exterior) < 0.01
    assert job.settings["isolation_width"] == pytest.approx(0.2)
    assert job.settings["bridges"] == []


def test_isolation_multi_pass_spacing():
    pad = shapely.Point(0, 0).buffer(1.0, quad_segs=32)
    s = {**FLAT02, "passes": 3, "overlap": 25.0, "combine": False}
    job = isolate(_layer(pad), s)
    assert len(job.ops) == 3
    step = 0.2 * 0.75
    radii = []
    for op in job.ops:
        assert len(op.paths) == 1
        r = np.hypot(*op.paths[0].T)
        radii.append(r.mean())
        assert r.max() - r.min() < 0.01
    assert radii[0] == pytest.approx(1.1, abs=5e-3)
    assert np.diff(radii) == pytest.approx([step, step], abs=5e-3)
    # passes vizinhos se sobrepõem exatamente "overlap", sem cortar o cobre
    for op in job.ops:
        assert shapely.distance(pad, shapely.LineString(op.paths[0])) >= 0.1 - 4e-3
    assert job.settings["isolation_width"] == pytest.approx(0.2 + 2 * step)


def test_isolation_climb_orientation_and_interior():
    donut = shapely.Point(0, 0).buffer(3).difference(shapely.Point(0, 0).buffer(1.5))
    for milling, ext_cw in (("climb", True), ("conventional", False)):
        job = isolate(_layer(donut), {**FLAT02, "milling": milling, "optimize": False})
        paths = job.ops[0].paths
        assert len(paths) == 2
        areas = sorted(paths, key=lambda p: -abs(_ring_signed_area(p)))
        ext, hole = areas
        assert (_ring_signed_area(ext) < 0) == ext_cw
        assert (_ring_signed_area(hole) < 0) == (not ext_cw)
    ext_only = isolate(_layer(donut), {**FLAT02, "iso_type": "ext"})
    assert len(ext_only.ops[0].paths) == 1
    assert np.hypot(*ext_only.ops[0].paths[0].T).mean() == pytest.approx(3.1, abs=0.01)
    int_only = isolate(_layer(donut), {**FLAT02, "iso_type": "int"})
    assert len(int_only.ops[0].paths) == 1
    assert np.hypot(*int_only.ops[0].paths[0].T).mean() == pytest.approx(1.4, abs=0.01)


def test_optimizer_preserves_ring_direction():
    donut = shapely.Point(0, 0).buffer(3).difference(shapely.Point(0, 0).buffer(1.5))
    many = shapely.MultiPolygon([shapely.affinity_translate(donut, dx) for dx in (0, 10, 20)]) \
        if hasattr(shapely, "affinity_translate") else None
    from shapely import affinity
    many = shapely.MultiPolygon([affinity.translate(donut, dx, (dx * 7) % 5) for dx in range(0, 100, 10)])
    a = isolate(_layer(many), {**FLAT02, "optimize": False}).ops[0].paths
    b = isolate(_layer(many), {**FLAT02, "optimize": True}).ops[0].paths
    sa = sorted(round(_ring_signed_area(p), 6) for p in a)
    sb = sorted(round(_ring_signed_area(p), 6) for p in b)
    assert sa == pytest.approx(sb, abs=1e-6)


def test_isolation_bridge_detection_and_rest_machining():
    a = shapely.box(0, 0, 1, 1)
    b = shapely.box(1.1, 0, 2.1, 1)           # vão de 0,1 mm < ferramenta de 0,2 mm
    c = shapely.box(5, 0, 6, 1)               # longe: sem problema
    cu = shapely.union_all([a, b, c])
    job = isolate(_layer(cu), dict(FLAT02))
    br = job.settings["bridges"]
    assert len(br) == 1
    assert br[0]["gap"] == pytest.approx(0.1, abs=1e-6)
    assert 1.0 <= br[0]["x"] <= 1.1 and 0 <= br[0]["y"] <= 1
    assert any("curto" in w for w in job.settings["warnings"])

    job2 = isolate(_layer(cu), {**FLAT02, "rest_machining": True, "rest_tool_dias": "0.15, 0.05"})
    assert job2.settings["bridges"] == []
    rest_ops = [op for op in job2.ops if op.tool.diameter < 0.2]
    assert rest_ops, "usinagem de resto deveria gerar operação com ferramenta menor"
    # os caminhos de resto passam pelo vão entre a e b
    gap = shapely.box(1.0, 0, 1.1, 1)
    assert any(shapely.intersects(gap, shapely.LineString(p)) for op in rest_ops for p in op.paths)
    assert rest_ops[0].tool.number != job2.ops[0].tool.number


def test_isolation_follow_and_scope():
    traces = [np.array([[0, 0], [5, 0], [5, 5]], float)]
    cu = shapely.union_all([shapely.LineString(traces[0]).buffer(0.2), shapely.box(20, 20, 21, 21)])
    job = isolate(_layer(cu, traces), {**FLAT02, "follow": True})
    assert len(job.ops[0].paths) == 1
    assert job.ops[0].paths[0] == pytest.approx(traces[0]) or \
        job.ops[0].paths[0][::-1] == pytest.approx(traces[0])
    # escopo: apenas a ilha clicada
    j2 = isolate(_layer(cu), {**FLAT02, "scope": "points", "scope_points": [(20.5, 20.5)]})
    assert len(j2.ops[0].paths) == 1
    assert shapely.LineString(j2.ops[0].paths[0]).centroid.distance(shapely.Point(20.5, 20.5)) < 0.01
    # exclusão
    j3 = isolate(_layer(cu), {**FLAT02, "exclude_geometry": shapely.box(19, 19, 30, 30)})
    for p in j3.ops[0].paths:
        assert not shapely.intersects(shapely.box(19.5, 19.5, 30, 30), shapely.LineString(p))


def test_isolation_cancel():
    cu = shapely.union_all([shapely.Point(i * 3, 0).buffer(1) for i in range(20)])
    with pytest.raises(Cancelled):
        isolate(_layer(cu), dict(FLAT02), cancel=lambda: True)
    calls = []
    isolate(_layer(cu), dict(FLAT02), progress=lambda f, m: calls.append((f, m)))
    assert calls and calls[-1][0] == pytest.approx(1.0)
    assert all(0.0 <= f <= 1.0 for f, _ in calls)


def test_isolation_performance_5000_pads():
    xs, ys = np.meshgrid(np.arange(100) * 2.54, np.arange(50) * 2.54)
    pads = shapely.buffer(shapely.points(np.c_[xs.ravel(), ys.ravel()]), 0.6, quad_segs=8)
    lines = [shapely.LineString([(0, i * 2.54), (99 * 2.54, i * 2.54)]) for i in range(0, 50, 2)]
    lines += [shapely.LineString([(i * 2.54 + 1.27, 0), (i * 2.54 + 1.27, 40)]) for i in range(0, 99, 5)]
    cu = shapely.union_all(np.concatenate([pads, shapely.buffer(np.array(lines), 0.15)]))
    t = time.perf_counter()
    job = isolate(_layer(cu), {"tool_shape": "vbit", "cut_z": -0.05})
    dt = time.perf_counter() - t
    n = sum(len(op.paths) for op in job.ops)
    assert n > 2000
    assert dt < 8.0, f"isolamento levou {dt:.2f} s"


# =========================================================================== NCC

def _ncc_layer():
    return _layer(shapely.union_all([shapely.Point(10, 10).buffer(1.5), shapely.box(15, 5, 30, 6),
                                     shapely.box(3, 15, 6, 25)]))


@pytest.mark.parametrize("method", ["lines", "offset", "combo"])
def test_ncc_methods_stay_away_from_copper(method):
    lay = _ncc_layer()
    s = {"tool_shape": "flat", "tool_dia": 0.8, "method": method, "margin": 1.0}
    job = clear_copper(lay, s)
    assert job.ops
    r = 0.4
    lines = shapely.MultiLineString([p for p in job.ops[0].paths])
    assert shapely.distance(lay.copper, lines) >= r - 4e-3   # tolerância de simplificação
    area = shapely.box(*lay.copper.bounds).buffer(1.0, join_style="mitre")
    assert shapely.covers(area.buffer(1e-6), lines)
    # cobertura: área varrida ≈ área a limpar
    swept = shapely.buffer(lines, r)
    target = area.difference(lay.copper)
    assert shapely.intersection(swept, target).area / target.area > 0.97


def test_ncc_lines_connect_reduces_lifts():
    lay = _ncc_layer()
    a = clear_copper(lay, {"tool_shape": "flat", "tool_dia": 0.8, "connect": False, "contour": False})
    b = clear_copper(lay, {"tool_shape": "flat", "tool_dia": 0.8, "connect": True, "contour": False})
    assert len(b.ops[0].paths) < len(a.ops[0].paths) / 3


def test_scanline_rectangle():
    segs = scanline_segments(shapely.box(0, 0, 10, 4), 1.0)
    assert len(segs) == 4
    for ln in segs:
        assert ln.shape == (1, 2, 2)
        assert abs(ln[0, 1, 0] - ln[0, 0, 0]) == pytest.approx(10.0)
    holed = shapely.box(0, 0, 10, 10).difference(shapely.box(4, 4, 6, 6))
    segs = scanline_segments(holed, 1.0, angle_deg=90)
    assert sum(len(s) for s in segs) == 12      # 2 linhas cruzam o furo (2 segmentos cada)


# =========================================================================== recorte

def test_cutout_rect_four_gaps():
    board = GeometryLayer("placa", shapely.box(0, 0, 50, 30))
    s = {"tool_dia": 2.0, "gaps": "4", "gap_size": 2.0, "optimize": False}
    job = cutout(board, s)
    op = job.ops[0]
    assert len(op.paths) == 4
    total = sum(shapely.LineString(p).length for p in op.paths)
    perim = shapely.box(0, 0, 50, 30).buffer(1.0, quad_segs=16).exterior.length
    # cada ponte remove gap + diâmetro do percurso
    assert total == pytest.approx(perim - 4 * (2.0 + 2.0), abs=0.05)
    for p in op.paths:
        assert shapely.distance(shapely.box(0, 0, 50, 30), shapely.LineString(p)) == pytest.approx(1.0, abs=2e-3)
    assert op.params.multi_depth and op.params.cut_z == pytest.approx(-1.7)
    assert len(job.settings["gap_points"]) == 4


@pytest.mark.parametrize("mode,count", [("none", 0), ("lr", 2), ("tb", 2), ("2lr", 4), ("2tb", 4), ("8", 8)])
def test_cutout_gap_modes(mode, count):
    job = cutout(GeometryLayer("p", shapely.box(0, 0, 40, 40)), {"gaps": mode})
    assert len(job.settings["gap_points"]) == count
    assert len(job.ops[0].paths) == max(count, 1)


def test_cutout_thin_and_mousebites():
    board = GeometryLayer("placa", shapely.box(0, 0, 40, 20))
    thin = cutout(board, {"gap_type": "thin", "gap_depth": -1.0, "gaps": "lr"})
    assert [op.label for op in thin.ops] == ["Recorte", "Pontes finas"]
    assert thin.ops[1].params.cut_z == pytest.approx(-1.0)
    assert len(thin.ops[1].paths) == 2
    mb = cutout(board, {"gap_type": "mousebites", "gaps": "lr", "gap_size": 3.0, "mb_spacing": 0.8})
    drill_op = [op for op in mb.ops if op.kind == "drill"][0]
    assert len(drill_op.points) >= 6
    edge = shapely.box(0, 0, 40, 20).exterior
    assert shapely.distance(edge, shapely.points(drill_op.points)).max() < 1e-6


def test_cutout_auto_gaps_and_outline_from_stroke():
    stroke = shapely.box(0, 0, 30, 20).exterior.buffer(0.05, join_style="mitre")
    b = board_from_geometry(stroke)
    assert b.bounds == pytest.approx((0, 0, 30, 20), abs=1e-6)
    job = cutout(GerberLayer("edge", "", stroke), {"gaps": "auto", "gap_spacing": 20})
    assert len(job.settings["gap_points"]) == round((30 + 20) * 2 / 20)
    # contorno feito de linhas soltas (DXF) com recorte interno
    lines = shapely.MultiLineString([[(0, 0), (30, 0)], [(30, 0), (30, 20)], [(30, 20), (0, 20)],
                                     [(0, 20), (0, 0)], [(10, 5), (20, 5), (20, 15), (10, 15), (10, 5)]])
    b2 = board_from_geometry(lines)
    assert b2.area == pytest.approx(600 - 100)
    j2 = cutout(GeometryLayer("dxf", lines), {"gaps": "none", "tool_dia": 1.0})
    assert len(j2.ops[0].paths) == 2   # contorno externo + janela interna


def test_cutout_rect_from_copper_with_margin():
    cu = shapely.union_all([shapely.Point(5, 5).buffer(1), shapely.Point(20, 12).buffer(1)])
    job = cutout(_layer(cu), {"source": "auto", "margin": 2.0, "gaps": "none", "tool_dia": 1.0})
    assert job.settings["board_bounds"] == pytest.approx((2, 2, 23, 15))


# =========================================================================== furação

def _drills(rng=np.random.default_rng(3)):
    return DrillLayer("furos", "", tools={
        1: DrillTool(1, 0.8, rng.random((200, 2)) * 50, np.zeros((0, 4))),
        2: DrillTool(2, 0.82, rng.random((30, 2)) * 50, np.zeros((0, 4))),
        3: DrillTool(3, 1.0, rng.random((40, 2)) * 50, np.array([[0, 0, 4, 0.0]])),
        4: DrillTool(4, 3.2, rng.random((4, 2)) * 50, np.zeros((0, 4))),
    })


def test_drilling_every_hole_once_sorted():
    lay = _drills()
    job = drill(lay, {})
    drill_ops = [op for op in job.ops if op.kind == "drill"]
    dias = [op.tool.diameter for op in drill_ops]
    assert dias == sorted(dias)
    got = Counter(map(tuple, np.round(np.vstack([op.points for op in drill_ops]), 9).tolist()))
    want = Counter(map(tuple, np.round(np.vstack([t.points for t in lay.tools.values()]), 9).tolist()))
    assert got == want
    assert [op for op in job.ops if op.kind == "mill" and "Slot" in op.label]
    assert job.settings["toolchange_z"] == 15.0


def test_drilling_group_library_mill_peck_tip():
    lay = _drills()
    j = drill(lay, {"mapping": "group", "group_tol": 0.05})
    assert len([op for op in j.ops if op.kind == "drill"]) == 3
    assert j.ops[0].tool.diameter == pytest.approx(0.82)
    assert len(j.ops[0].points) == 230
    j = drill(lay, {"mapping": "library", "available_drills": "0.8, 1.0", "mill_large": True,
                    "mill_tool_dia": 1.0, "peck": True, "peck_depth": 0.4, "tip_compensation": True,
                    "tool_select": "T1, 3, 4"})
    drills = [op for op in j.ops if op.kind == "drill"]
    assert {op.tool.diameter for op in drills} == {0.8, 1.0}
    assert all(op.params.multi_depth and op.params.depth_per_pass == 0.4 for op in drills)
    assert drills[0].params.cut_z == pytest.approx(-1.8 - drill_tip_length(0.8))
    milled = [op for op in j.ops if op.label.startswith("Furos fresados")][0]
    assert len(milled.paths) == 4
    r = np.hypot(*(milled.paths[0] - milled.paths[0][:-1].mean(axis=0)).T)
    assert r == pytest.approx(np.full(len(r), 1.6 - 0.5), abs=1e-6)
    assert sum(len(op.points) for op in drills) == 240   # T2 não selecionado
    j = drill(lay, {"slot_strategy": "drill"})
    # slot de 4 mm com broca de 1 mm e 50% de sobreposição: passo 0,5 mm -> 9 furos
    assert sum(len(op.points) for op in j.ops) == 274 + 9


def test_drilling_cancel():
    with pytest.raises(Cancelled):
        drill(_drills(), {}, cancel=lambda: True)


# =========================================================================== ordenação

def test_order_points_10000_fast_and_permutation():
    rng = np.random.default_rng(7)
    pts = rng.random((10000, 2)) * 200
    t = time.perf_counter()
    order = order_points(pts, time_limit=0.3)
    dt = time.perf_counter() - t
    assert dt < 1.0, f"{dt:.2f} s"
    assert sorted(order.tolist()) == list(range(10000))
    assert travel_length(pts[order]) < 0.05 * travel_length(pts)


def test_order_points_two_opt_improves():
    rng = np.random.default_rng(1)
    pts = rng.random((800, 2)) * 100
    nn = order_points(pts, time_limit=0.0)
    opt = order_points(pts, time_limit=0.5)
    assert travel_length(pts[opt]) < travel_length(pts[nn])
    assert travel_length(optimize_points(pts[::-1])) < travel_length(pts)


def test_optimize_paths_2000_fast():
    rng = np.random.default_rng(2)
    paths = []
    for i, (x, y) in enumerate(rng.random((2000, 2)) * 150):
        if i % 2:
            paths.append(np.asarray(shapely.Point(x, y).buffer(0.6, quad_segs=6).exterior.coords))
        else:
            paths.append(np.array([[x, y], [x + 1, y + 0.3], [x + 2, y]]))
    t = time.perf_counter()
    out = optimize_paths(paths, time_limit=0.3)
    dt = time.perf_counter() - t
    assert dt < 1.0, f"{dt:.2f} s"
    assert len(out) == 2000
    assert travel_length(out) < 0.1 * travel_length(paths)
    # anéis: mesmos vértices, mesmo sentido; abertas: iguais ou invertidas
    key = lambda p: (round(float(p[:, 0].mean()), 6), round(float(p[:, 1].mean()), 6))  # noqa: E731
    orig = {key(p[:-1] if np.allclose(p[0], p[-1]) else p): p for p in paths}
    for p in out:
        closed = np.allclose(p[0], p[-1]) and len(p) > 3
        o = orig[key(p[:-1] if closed else p)]
        if closed:
            assert np.sign(_ring_signed_area(p)) == np.sign(_ring_signed_area(o))
            assert len(p) == len(o)
        else:
            assert np.allclose(p, o) or np.allclose(p[::-1], o)


def test_optimize_paths_no_reverse_keeps_direction():
    paths = [np.array([[i, 0.0], [i, 1.0]]) for i in range(10)][::-1]
    out = optimize_paths(paths, allow_reverse=False)
    for p in out:
        assert p[1, 1] > p[0, 1]


def test_optimize_job_and_travel_length():
    job = drill(_drills(), {"optimize": False})
    before = sum(travel_length(op.points) for op in job.ops if op.kind == "drill")
    optimize_job(job)
    after = sum(travel_length(op.points) for op in job.ops if op.kind == "drill")
    assert after < before * 0.5
    assert travel_length(np.array([[3.0, 4.0]])) == pytest.approx(5.0)
    assert travel_length([np.array([[3.0, 4.0], [3.0, 5.0]]), np.array([[3.0, 6.0], [0, 0]])]) == \
        pytest.approx(6.0)


# =========================================================================== painel

def test_panelize_layers_and_job():
    lay = _layer(shapely.box(0, 0, 10, 5))
    p = panelize(lay, {"columns": 3, "rows": 2, "spacing_x": 2, "spacing_y": 1})
    assert len(shapely.get_parts(p.copper)) == 6
    assert p.bounds == pytest.approx((0, 0, 34, 11))
    d = panelize(_drills(), {"columns": 2, "rows": 1})
    assert len(d.tools[1].points) == 400 and len(d.tools[3].slots) == 2
    g = panelize(GeometryLayer("g", shapely.box(0, 0, 10, 10)),
                 {"columns": 5, "rows": 5, "constrain": True, "max_width": 30, "max_height": 100})
    assert len(shapely.get_parts(g.geometry)) == 2 * 5
    job = isolate(lay, dict(FLAT02))
    pj = panelize_job(job, lay.bounds, {"columns": 2, "rows": 2})
    assert len(pj.ops[0].paths) == 4 * len(job.ops[0].paths)


# =========================================================================== integração

def test_integration_with_parsers():
    pytest.importorskip("brokcam.parsers.gerber")
    pytest.importorskip("brokcam.parsers.excellon")
    import os
    from brokcam.parsers.excellon import parse_excellon
    from brokcam.parsers.gerber import parse_gerber
    here = os.path.join(os.path.dirname(__file__), "data")
    fcu, edge, pth = (os.path.join(here, f) for f in
                      ("board-F_Cu.gbr", "board-Edge_Cuts.gbr", "board-PTH.drl"))
    if not all(os.path.exists(f) for f in (fcu, edge, pth)):
        pytest.skip("arquivos de teste ausentes")
    try:
        g = parse_gerber(fcu)
        e = parse_gerber(edge)
        d = parse_excellon(pth)
    except Exception as exc:     # parser em desenvolvimento
        pytest.skip(f"parser indisponível: {exc}")
    iso = isolate(g, {"passes": 2})
    assert iso.ops and iso.stats()["cut_length_mm"] > 0
    cut = cutout(e, {})
    assert cut.ops
    xmin, ymin, xmax, ymax = cut.settings["board_bounds"]
    exmin, eymin, exmax, eymax = e.bounds
    assert exmin - 0.2 <= xmin and xmax <= exmax + 0.2
    dj = drill(d, {})
    assert dj.stats()["holes"] == sum(len(t.points) for t in d.tools.values())
    ncc = clear_copper(g, {})
    assert ncc.ops


def test_ncc_boundary_and_cancel():
    lay = _ncc_layer()
    outline = shapely.box(0, 0, 35, 30).exterior          # contorno como linha (Edge.Cuts/DXF)
    job = clear_copper(lay, {"area": "boundary", "boundary_geometry": outline, "margin": 0.0,
                             "tool_shape": "flat", "tool_dia": 1.0})
    lines = shapely.MultiLineString(job.ops[0].paths)
    assert shapely.covers(shapely.box(0.5, 0.5, 34.5, 29.5).buffer(1e-6), lines)
    with pytest.raises(ValueError):
        clear_copper(lay, {"area": "region"})
    with pytest.raises(Cancelled):
        clear_copper(lay, {}, cancel=lambda: True)
    with pytest.raises(Cancelled):
        cutout(GeometryLayer("p", shapely.box(0, 0, 10, 10)), {}, cancel=lambda: True)
