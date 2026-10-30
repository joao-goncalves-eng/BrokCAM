"""Modelo de dados compartilhado entre parsers, CAM, pós-processador e interface.

Convenções (válidas em TODO o projeto):
* Unidade interna: milímetros. Parsers convertem polegadas -> mm na leitura.
* Coordenadas: plano XY cartesiano, Y para cima (a interface inverte na tela).
* Geometria 2D: shapely >= 2.0. Pontos e polilinhas: ``numpy.ndarray`` float64.
* Nenhum módulo de ``core``, ``parsers``, ``cam`` ou ``gcode`` importa Qt.
"""
from __future__ import annotations

import itertools
from dataclasses import dataclass, field
from typing import Any, Literal

import numpy as np
from shapely.geometry.base import BaseGeometry

_ids = itertools.count(1)


def new_id() -> int:
    return next(_ids)


# --------------------------------------------------------------------------- fontes

@dataclass
class Aperture:
    code: str                     # "D10"
    shape: str                    # "C", "R", "O", "P", macro name...
    params: tuple[float, ...]     # em mm


@dataclass
class GerberLayer:
    """Camada Gerber já interpretada.

    ``copper`` é a união de todas as áreas escuras (pads, trilhas, regiões),
    já com polaridade clear (LPC) aplicada. Pronta para isolamento.
    """
    name: str
    source_path: str
    copper: BaseGeometry                       # Polygon | MultiPolygon
    apertures: dict[str, Aperture] = field(default_factory=dict)
    # Linhas centrais das trilhas (útil para "seguir trilha"/laser). Opcional.
    traces: list[np.ndarray] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    id: int = field(default_factory=new_id)
    kind: Literal["gerber"] = "gerber"

    @property
    def bounds(self) -> tuple[float, float, float, float]:
        return self.copper.bounds if not self.copper.is_empty else (0, 0, 0, 0)


@dataclass
class DrillTool:
    number: int                   # T1, T2...
    diameter: float               # mm
    points: np.ndarray            # (N, 2) centros dos furos
    slots: np.ndarray             # (M, 4) x0, y0, x1, y1 de fresas oblongas (slots)
    plated: bool | None = None


@dataclass
class DrillLayer:
    name: str
    source_path: str
    tools: dict[int, DrillTool] = field(default_factory=dict)
    warnings: list[str] = field(default_factory=list)
    id: int = field(default_factory=new_id)
    kind: Literal["excellon"] = "excellon"

    @property
    def bounds(self) -> tuple[float, float, float, float]:
        pts = [t.points for t in self.tools.values() if len(t.points)]
        pts += [t.slots.reshape(-1, 2) for t in self.tools.values() if len(t.slots)]
        if not pts:
            return (0, 0, 0, 0)
        a = np.vstack(pts)
        return (float(a[:, 0].min()), float(a[:, 1].min()),
                float(a[:, 0].max()), float(a[:, 1].max()))


@dataclass
class GeometryLayer:
    """Geometria genérica (contorno da placa, DXF/SVG importado, resultado de CAM)."""
    name: str
    geometry: BaseGeometry
    source_path: str = ""
    id: int = field(default_factory=new_id)
    kind: Literal["geometry"] = "geometry"

    @property
    def bounds(self) -> tuple[float, float, float, float]:
        return self.geometry.bounds if not self.geometry.is_empty else (0, 0, 0, 0)


# --------------------------------------------------------------------------- CAM

@dataclass
class Tool:
    diameter: float               # mm
    name: str = ""                # ex.: "V-bit 30° 0.1mm", "Broca 0.8"
    number: int = 1               # número usado no M6 Tn
    shape: Literal["flat", "vbit", "ball", "drill", "laser"] = "flat"
    vbit_angle: float = 30.0      # graus, só para vbit
    vbit_tip: float = 0.1         # mm, só para vbit


@dataclass
class CutParams:
    """Parâmetros físicos de um corte. Todos em mm, mm/min, RPM, s."""
    cut_z: float = -0.05
    travel_z: float = 1.0
    feed_xy: float = 120.0
    feed_z: float = 60.0
    spindle_rpm: float = 10000.0
    multi_depth: bool = False
    depth_per_pass: float = 0.1
    dwell_s: float = 0.0
    laser_mode: bool = False      # GRBL $32=1: usa M4 e S como potência, sem Z
    laser_power: float = 1000.0   # valor S (0..$30)


@dataclass
class PathOp:
    """Uma operação de usinagem com uma única ferramenta.

    kind == "mill":  ``paths`` = lista de polilinhas (N,2) JÁ ORDENADAS para
                     minimizar deslocamento; uma polilinha fechada repete o 1º ponto.
    kind == "drill": ``points`` = (K,2) centros JÁ ORDENADOS.
    """
    kind: Literal["mill", "drill"]
    tool: Tool
    params: CutParams
    paths: list[np.ndarray] = field(default_factory=list)
    points: np.ndarray = field(default_factory=lambda: np.zeros((0, 2)))
    label: str = ""


@dataclass
class Job:
    """Resultado de uma operação de CAM, pronto para pós-processamento."""
    name: str
    source_id: int
    ops: list[PathOp]
    operation: str = ""           # "isolation", "drilling", "cutout", "ncc"...
    settings: dict[str, Any] = field(default_factory=dict)
    gcode: str = ""               # preenchido pelo pós-processador
    id: int = field(default_factory=new_id)
    kind: Literal["job"] = "job"

    def stats(self) -> dict[str, float]:
        """Comprimento de corte e número de furos (para estimativa de tempo)."""
        cut = 0.0
        holes = 0
        for op in self.ops:
            for p in op.paths:
                if len(p) > 1:
                    cut += float(np.hypot(*np.diff(p, axis=0).T).sum())
            holes += len(op.points)
        return {"cut_length_mm": cut, "holes": holes}


SourceLayer = GerberLayer | DrillLayer | GeometryLayer
