"""Furação com um trabalho (e um G-code) por broca: ``split_jobs`` / ``bit_filter``."""
from __future__ import annotations

from collections import Counter

import numpy as np

from brokcam.cam.drilling import DRILLING, drill, drill_auto, drill_split
from brokcam.cam.operations import OPERATIONS
from brokcam.core.model import DrillLayer, DrillTool
from brokcam.gcode.grbl import generate_gcode


def _layer() -> DrillLayer:
    rng = np.random.default_rng(7)
    return DrillLayer("placa.drl", "", tools={
        1: DrillTool(1, 0.8, rng.random((20, 2)) * 40, np.zeros((0, 4))),
        2: DrillTool(2, 0.82, rng.random((5, 2)) * 40, np.zeros((0, 4))),
        3: DrillTool(3, 1.0, rng.random((12, 2)) * 40, np.zeros((0, 4))),
        4: DrillTool(4, 3.2, rng.random((3, 2)) * 40, np.zeros((0, 4))),
    })


def _pts(jobs):
    return Counter(map(tuple, np.round(np.vstack([op.points for j in jobs for op in j.ops
                                                  if op.kind == "drill"]), 9).tolist()))


def test_default_is_single_and_backward_compatible():
    lay = _layer()
    assert DRILLING.defaults()["split_jobs"] == "single" and DRILLING.defaults()["bit_filter"] == 0.0
    out = drill_auto(lay, {})                          # settings antigos, sem as chaves novas
    assert not isinstance(out, list)
    assert len([op for op in out.ops if op.kind == "drill"]) == 4
    assert out.name == "placa.drl — furação (4 brocas · 40 furos)"
    assert OPERATIONS["drilling"].func is drill_auto


def test_per_bit_one_job_per_bit_same_holes():
    lay = _layer()
    single = drill(lay, {})
    jobs = drill_auto(lay, {"split_jobs": "per_bit"})
    assert isinstance(jobs, list) and len(jobs) == 4
    assert sum(j.stats()["holes"] for j in jobs) == single.stats()["holes"] == 40
    assert _pts(jobs) == _pts([single])
    for j in jobs:
        assert len(j.ops) == 1 and j.operation == "drilling"
        assert j.settings["bit_filter"] == j.ops[0].tool.diameter
    assert [j.name for j in jobs] == [
        "Furação Ø0,8 mm (T1 · 20 furos)", "Furação Ø0,82 mm (T2 · 5 furos)",
        "Furação Ø1 mm (T3 · 12 furos)", "Furação Ø3,2 mm (T4 · 3 furos)"]


def test_per_bit_after_grouping_and_milled_job():
    lay = _layer()
    jobs = drill_split(lay, {"mapping": "group", "group_tol": 0.05, "mill_large": True})
    # 0,8 + 0,82 viram uma broca de 0,82; 3,2 mm é fresado. O nome cita os furos quando
    # a broca não tem o diâmetro deles.
    assert [j.name for j in jobs] == [
        "Furação broca Ø0,82 mm (furos Ø0,8 e 0,82 · T1 T2 · 25 furos)", "Furação Ø1 mm (T3 · 12 furos)",
        "Furos fresados com fresa Ø1 mm (T4 · 3 furos)"]
    assert jobs[-1].settings["bit_filter"] == -1.0
    assert jobs[-1].ops[0].kind == "mill" and len(jobs[-1].ops[0].paths) == 3


def test_bit_filter_regenerates_identically():
    lay = _layer()
    jobs = drill_split(lay, {"mapping": "group", "group_tol": 0.05, "mill_large": True})
    for j in jobs:
        again = drill_auto(lay, j.settings)            # como ao reabrir o .bcam
        assert not isinstance(again, list)
        assert again.name == j.name
        assert len(again.ops) == len(j.ops)
        for a, b in zip(again.ops, j.ops):
            assert a.tool == b.tool and a.kind == b.kind
            assert np.allclose(a.points, b.points)
            assert len(a.paths) == len(b.paths)


def test_bit_filter_isolates_one_bit_and_keeps_numbering():
    lay = _layer()
    j = drill(lay, {"bit_filter": 1.0})
    assert len(j.ops) == 1 and j.ops[0].tool.diameter == 1.0 and len(j.ops[0].points) == 12
    # numeração da sequência completa (T3), com o T original do arquivo no nome da ferramenta
    assert j.ops[0].tool.number == 3 and "arquivo T3" in j.ops[0].tool.name
    none = drill(lay, {"bit_filter": 2.0})
    assert not none.ops and any("2 mm" in w for w in none.settings["warnings"])


def test_each_split_gcode_has_no_toolchange_pause():
    lay = _layer()
    single = generate_gcode(drill(lay, {}), {})
    assert single.split().count("M0") == 3
    assert "arquivo T2" in single              # T original na mensagem de troca
    for j in drill_split(lay, {"mill_large": True}):
        g = generate_gcode(j, {})
        assert "M0" not in g.split() and "MSG" not in g


def test_bits_metadata_and_ranks():
    lay = _layer()
    j = drill(lay, {})
    bits = j.settings["bits"]
    assert [b["bit"] for b in bits] == [0.8, 0.82, 1.0, 3.2]
    assert [b["rank"] for b in bits] == [0, 1, 2, 3]
    assert [b["sources"] for b in bits] == [[1], [2], [3], [4]]
    assert sum(b["holes"] for b in bits) == 40
    # desc: ordem de execução muda, mas a cor (rank) continua pelo diâmetro crescente
    jd = drill(lay, {"tool_order": "desc"})
    assert [(b["bit"], b["rank"]) for b in jd.settings["bits"]][0] == (3.2, 3)
    # job separado mantém o rank do diâmetro na camada inteira
    assert drill(lay, {"bit_filter": 1.0}).settings["bits"][0]["rank"] == 2
    assert drill(lay, {"tool_select": "3"}).settings["bits"][0]["rank"] == 2


def test_split_cancel_and_empty_layer():
    import pytest
    from brokcam.cam import Cancelled
    with pytest.raises(Cancelled):
        drill_split(_layer(), {}, cancel=lambda: True)
    assert drill_split(DrillLayer("vazia", ""), {}) == []


def test_tool_select_uses_same_bit_as_full_program():
    """'Gerar só este diâmetro' (tool_select) dá a mesma broca que o programa completo."""
    from brokcam.cam.drilling import bit_assignment
    lay = _layer()
    s = {"mapping": "group", "group_tol": 0.05}
    only_t1 = drill(lay, {**s, "tool_select": "T1"})
    assert only_t1.ops[0].tool.diameter == 0.82          # agrupado com o T2, como no programa completo
    assert only_t1.name == "Furação broca Ø0,82 mm (furos Ø0,8 · T1 · 20 furos)"
    assert bit_assignment(lay, s) == {1: (0.82, False), 2: (0.82, False), 3: (1.0, False), 4: (3.2, False)}
    lib = {"mapping": "library", "available_drills": "0.8, 1.0", "library_rule": "larger"}
    assert bit_assignment(lay, lib)[1] == (0.8, False) and bit_assignment(lay, lib)[2] == (1.0, False)
    m = bit_assignment(lay, {"mill_large": True})
    assert m[4] == (1.0, True)
    assert drill(lay, {**lib, "tool_select": "T2"}).ops[0].tool.diameter == 1.0


def test_gcode_comment_separates_file_and_machine_tool():
    lay = _layer()
    g = generate_gcode(drill(lay, {"bit_filter": 1.0}), {})
    line = next(ln for ln in g.splitlines() if ln.startswith("(Op "))
    assert line == "(Op T3 da maquina drill D1mm: Furos broca 1 mm - arquivo T3)"
    assert len(line) <= 70


def test_bit_map_parse_and_format():
    from brokcam.cam.drilling import format_bit_map, parse_bit_map
    m = parse_bit_map("0,79=0,8; 1.02 : 1,0\n3,2=fresa; lixo; 1=x; 0,5->0,6")
    assert m == {0.79: 0.8, 1.02: 1.0, 3.2: "fresa", 0.5: 0.6}
    assert parse_bit_map(format_bit_map(m)) == m
    assert format_bit_map({0.8: 1.0, 3.2: "fresa"}) == "0,8=1; 3,2=fresa"


def test_manual_mapping_one_program_with_toolchanges():
    """Vários diâmetros escolhidos à mão para a mesma broca → um programa, uma troca por broca."""
    lay = _layer()
    s = {"mapping": "manual", "bit_map": "0,8=1,0; 0,82=1,0; 3,2=fresa", "mill_large": False,
         "mill_tool_dia": 1.0, "available_drills": "0.8, 1.0"}
    job = drill(lay, s)
    drills = [op for op in job.ops if op.kind == "drill"]
    assert [(op.tool.number, op.tool.diameter, len(op.points)) for op in drills] == [(1, 1.0, 37)]
    mills = [op for op in job.ops if op.kind == "mill"]
    assert len(mills) == 1 and mills[0].tool.shape == "flat" and len(mills[0].paths) == 3
    g = generate_gcode(job, {})
    assert g.count("T2") >= 1                                  # fresa = 2ª ferramenta, com troca
    # diâmetro sem escolha segue a regra da lista (0,82 → mais próxima 0,8)
    job2 = drill(lay, {"mapping": "manual", "bit_map": "3,2=1,0", "available_drills": "0.8, 1.0"})
    assert [(op.tool.diameter, len(op.points)) for op in job2.ops] == [(0.8, 25), (1.0, 15)]
    # escolhido à mão não é fresado automaticamente, mesmo com "Fresar furos grandes"
    job3 = drill(lay, {"mapping": "manual", "bit_map": "3,2=3,0", "mill_large": True,
                       "available_drills": "0.8, 1.0"})
    assert any(op.tool.diameter == 3.0 and op.kind == "drill" for op in job3.ops)
    # um job por broca também respeita a escolha
    jobs = drill_split(lay, {**s, "split_jobs": "per_bit"})
    assert len(jobs) == 2 and _pts(jobs[:1]) == _pts([job])
