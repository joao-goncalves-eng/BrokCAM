"""Conversão de ``PathOp``/``Job`` para JSON e de volta.

Usada para guardar no projeto os caminhos editados manualmente — que não podem ser
regenerados a partir dos parâmetros sem perder a edição.
"""
from __future__ import annotations

from dataclasses import asdict, fields
from typing import Any

import numpy as np

from .model import CutParams, PathOp, Tool


def _round(a: np.ndarray, decimals: int) -> list:
    return np.round(np.asarray(a, dtype=float), decimals).tolist()


def op_to_json(op: PathOp, decimals: int = 5) -> dict[str, Any]:
    return {
        "kind": op.kind,
        "label": op.label,
        "tool": asdict(op.tool),
        "params": asdict(op.params),
        "paths": [_round(p, decimals) for p in op.paths],
        "points": _round(np.asarray(op.points).reshape(-1, 2), decimals),
    }


def _filtered(cls: type, d: dict[str, Any]) -> dict[str, Any]:
    names = {f.name for f in fields(cls)}
    return {k: v for k, v in (d or {}).items() if k in names}


def op_from_json(d: dict[str, Any]) -> PathOp:
    return PathOp(
        kind=d.get("kind", "mill"),
        tool=Tool(**_filtered(Tool, d.get("tool", {}))),
        params=CutParams(**_filtered(CutParams, d.get("params", {}))),
        paths=[np.asarray(p, dtype=float).reshape(-1, 2) for p in d.get("paths", []) if len(p)],
        points=np.asarray(d.get("points", []), dtype=float).reshape(-1, 2),
        label=d.get("label", ""),
    )


def ops_to_json(ops: list[PathOp]) -> list[dict[str, Any]]:
    return [op_to_json(op) for op in ops]


def ops_from_json(items: list[dict[str, Any]]) -> list[PathOp]:
    return [op_from_json(d) for d in items or []]


# --------------------------------------------------------------------------- camadas geradas
# Camadas criadas no programa (pinos de alinhamento, painelização…) não têm arquivo de
# origem: são gravadas inteiras no projeto.

def layer_to_json(layer: Any) -> dict[str, Any]:
    import shapely
    from .model import DrillLayer, GeometryLayer, GerberLayer
    if isinstance(layer, DrillLayer):
        return {"type": "excellon", "name": layer.name, "tools": [
            {"number": t.number, "diameter": t.diameter, "plated": t.plated,
             "points": _round(np.asarray(t.points).reshape(-1, 2), 5),
             "slots": _round(np.asarray(t.slots).reshape(-1, 4), 5)} for t in layer.tools.values()]}
    if isinstance(layer, GerberLayer):
        return {"type": "gerber", "name": layer.name, "wkb": shapely.to_wkb(layer.copper, hex=True),
                "traces": [_round(t, 5) for t in layer.traces]}
    if isinstance(layer, GeometryLayer):
        return {"type": "geometry", "name": layer.name, "wkb": shapely.to_wkb(layer.geometry, hex=True)}
    raise TypeError(f"camada não serializável: {type(layer).__name__}")


def layer_from_json(d: dict[str, Any]) -> Any:
    import shapely
    from .model import DrillLayer, DrillTool, GeometryLayer, GerberLayer
    kind = d.get("type")
    if kind == "excellon":
        tools = {}
        for t in d.get("tools", []):
            tools[int(t["number"])] = DrillTool(
                number=int(t["number"]), diameter=float(t["diameter"]),
                points=np.asarray(t.get("points", []), dtype=float).reshape(-1, 2),
                slots=np.asarray(t.get("slots", []), dtype=float).reshape(-1, 4), plated=t.get("plated"))
        return DrillLayer(name=d.get("name", "Furos"), source_path="", tools=tools)
    if kind == "gerber":
        return GerberLayer(name=d.get("name", "Gerber"), source_path="",
                           copper=shapely.from_wkb(d["wkb"]),
                           traces=[np.asarray(t, dtype=float).reshape(-1, 2) for t in d.get("traces", [])])
    if kind == "geometry":
        return GeometryLayer(name=d.get("name", "Geometria"), geometry=shapely.from_wkb(d["wkb"]))
    raise ValueError(f"tipo de camada desconhecido: {kind!r}")
