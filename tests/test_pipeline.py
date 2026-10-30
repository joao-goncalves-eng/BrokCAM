"""Teste de ponta a ponta: Gerber/Excellon -> CAM -> G-code GRBL -> simulador."""
import math
from pathlib import Path

from brokcam.cam.operations import OPERATIONS
from brokcam.gcode.grbl import generate_program
from brokcam.gcode.preview import parse_gcode
from brokcam.machine.grbl import GrblController
from brokcam.machine.simulator import GrblSimulator
from brokcam.parsers.detect import load_file

DATA = Path(__file__).parent / "data"


def _run(op_key, layer, **overrides):
    op = OPERATIONS[op_key]
    settings = op.group.defaults()
    settings.update(overrides)
    return op.func(layer, settings)


def test_full_board_to_grbl():
    top = load_file(DATA / "board-F_Cu.gbr")
    edge = load_file(DATA / "board-Edge_Cuts.gbr")
    pth = load_file(DATA / "board-PTH.drl")

    jobs = [
        _run("isolation", top),
        _run("drilling", pth),
        _run("cutout", edge),
    ]
    for j in jobs:
        assert j.ops, j.name
    stats = jobs[0].stats()
    assert stats["cut_length_mm"] > 50

    gcode = generate_program(jobs)
    assert "G21" in gcode and "M3" in gcode
    for forbidden in ("G83", "G81", "G41", "G42"):
        assert forbidden not in gcode

    prev = parse_gcode(gcode)
    assert not [w for w in prev.warnings if "desconhecido" in w.lower()]
    xmin, ymin, _, xmax, ymax, _ = prev.bounds
    # tudo dentro da placa 30x20 com folga da ferramenta de recorte
    assert -3 < xmin and xmax < 33 and -3 < ymin and ymax < 23
    assert prev.estimate_time_s > 0 and math.isfinite(prev.estimate_time_s)

    ctrl = GrblController()
    errors = []
    ctrl.on_error = lambda code, msg, line: errors.append((code, msg, line))
    ctrl.connect("SIMULADOR", transport=GrblSimulator(speed=math.inf))
    try:
        # M0 de troca de ferramenta pausa o GRBL; retoma automaticamente no teste
        lines = [ln for ln in gcode.splitlines() if ln.strip() != "M0"]
        ctrl.stream_program(lines)
        assert ctrl.wait_stream(60)
    finally:
        ctrl.disconnect()
    assert not errors, errors
