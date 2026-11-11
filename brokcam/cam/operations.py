"""Registro das operações de CAM para a interface.

A interface lista :data:`OPERATIONS` (ou :func:`operations_for` um tipo de camada),
monta o formulário a partir de ``op.group`` (``ParamGroup``) e executa
``op.func(layer, settings, progress, cancel)`` numa thread de trabalho.

``produces`` indica o que a função devolve: ``"job"`` (um :class:`Job` pronto para o
pós-processador — ou uma LISTA de jobs, ex.: furação com um trabalho por broca) ou
``"layer"`` (uma nova camada, ex.: painelização).
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Callable

from brokcam.cam.cutout import CUTOUT, cutout
from brokcam.cam.drilling import DRILLING, drill_auto
from brokcam.cam.isolation import ISOLATION, isolate
from brokcam.cam.ncc import NCC, clear_copper
from brokcam.cam.panelize import PANELIZE, panelize
from brokcam.core.params import ParamGroup, set_conditions


@dataclass(frozen=True)
class Operation:
    key: str
    title: str
    description: str
    group: ParamGroup
    func: Callable[..., Any]
    accepts: tuple[str, ...]          # kinds de camada: "gerber", "excellon", "geometry"
    produces: str = "job"             # "job" | "layer"


# --------------------------------------------------------------------------- dependências entre campos
# A interface só mostra um campo quando ele tem efeito com os valores atuais.
_OPT = {"optimize_time": (("optimize", (True,)),), "start_x": (("optimize", (True,)),),
        "start_y": (("optimize", (True,)),)}
set_conditions(ISOLATION, {
    **_OPT,
    "overlap": (("passes", tuple(range(2, 100))),),
    "combine": (("passes", tuple(range(2, 100))),),
    "scope_xmin": (("scope", ("bbox",)),), "scope_ymin": (("scope", ("bbox",)),),
    "scope_xmax": (("scope", ("bbox",)),), "scope_ymax": (("scope", ("bbox",)),),
    "clip_to_scope": (("scope", ("bbox", "region", "points")),),
    "rest_tool_dias": (("rest_machining", (True,)),),
})
set_conditions(NCC, {
    **_OPT,
    "line_angle": (("method", ("lines", "combo")),),
    "connect": (("method", ("lines", "combo")),),
    "combo_rings": (("method", ("combo",)),),
    "max_rings": (("method", ("offset", "combo")),),
})
set_conditions(CUTOUT, {
    **_OPT,
    "margin": (("source", ("rect", "convex", "auto")),),
    "gap_size": (("gaps", ("lr", "tb", "4", "2lr", "2tb", "8", "auto")),),
    "gap_type": (("gaps", ("lr", "tb", "4", "2lr", "2tb", "8", "auto")),),
    "gap_spacing": (("gaps", ("auto",)),),
    "gap_depth": (("gap_type", ("thin",)), ("gaps", ("lr", "tb", "4", "2lr", "2tb", "8", "auto"))),
    "mb_dia": (("gap_type", ("mousebites",)), ("gaps", ("lr", "tb", "4", "2lr", "2tb", "8", "auto"))),
    "mb_spacing": (("gap_type", ("mousebites",)), ("gaps", ("lr", "tb", "4", "2lr", "2tb", "8", "auto"))),
})
set_conditions(DRILLING, {
    **_OPT,
    "group_tol": (("mapping", ("group",)),),
    "available_drills": (("mapping", ("library", "manual")),),
    "library_rule": (("mapping", ("library", "manual")),),
    "bit_map": (("mapping", ("manual",)),),
    "slot_overlap": (("slot_strategy", ("mill",)),),
    "mill_tool_dia": (("mill_large", (True,)),),
    "mill_min_dia": (("mill_large", (True,)),),
    "mill_threshold": (("mill_large", (True,)),),
    "mill_feed_xy": (("mill_large", (True,)),),
    "mill_depth_per_pass": (("mill_large", (True,)),),
    "peck_depth": (("peck", (True,)),),
    "tip_angle": (("tip_compensation", (True,)),),
})
set_conditions(PANELIZE, {
    "ref_width": (("use_reference", (True,)),), "ref_height": (("use_reference", (True,)),),
    "max_width": (("constrain", (True,)),), "max_height": (("constrain", (True,)),),
})


OPERATIONS: dict[str, Operation] = {
    op.key: op for op in (
        Operation("isolation", ISOLATION.title, ISOLATION.description, ISOLATION, isolate,
                  ("gerber", "geometry")),
        Operation("ncc", NCC.title, NCC.description, NCC, clear_copper, ("gerber", "geometry")),
        Operation("cutout", CUTOUT.title, CUTOUT.description, CUTOUT, cutout,
                  ("gerber", "geometry", "excellon")),
        Operation("drilling", DRILLING.title, DRILLING.description, DRILLING, drill_auto, ("excellon",)),
        Operation("panelize", PANELIZE.title, PANELIZE.description, PANELIZE, panelize,
                  ("gerber", "excellon", "geometry"), produces="layer"),
    )
}


def operations_for(kind: str) -> list[Operation]:
    """Operações aplicáveis a uma camada do tipo ``kind`` ("gerber", "excellon", "geometry")."""
    return [op for op in OPERATIONS.values() if kind in op.accepts]
