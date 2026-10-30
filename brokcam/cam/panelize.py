"""Painelização: replica uma camada (ou um trabalho de CAM) em linhas × colunas.

Diferente das demais operações, :func:`panelize` devolve uma NOVA CAMADA do mesmo
tipo (Gerber, Excellon ou geometria), que pode então ser isolada, furada e recortada
normalmente. :func:`panelize_job` replica diretamente um :class:`Job` já calculado
(mais rápido: não recalcula o CAM para cada cópia).
"""
from __future__ import annotations

from dataclasses import replace
from typing import Any

import numpy as np
import shapely

from brokcam.cam.common import Progress, polygons_of
from brokcam.core.model import DrillLayer, DrillTool, GerberLayer, GeometryLayer, Job, PathOp
from brokcam.core.params import ParamGroup, ParamSpec, register

PANELIZE = register(ParamGroup(
    key="panelize",
    title="Painelização",
    description=(
        "Cria um painel com várias cópias da placa, em linhas e colunas, para "
        "fabricar diversas unidades de uma vez numa mesma chapa. Painelize TODAS as "
        "camadas da placa (cobre, furos e contorno) com os mesmos valores, para que "
        "as cópias fiquem alinhadas."),
    params=(
        ParamSpec("columns", "Colunas", "int", 2, minimum=1, maximum=100, step=1, decimals=0,
                  help="Número de cópias na direção X."),
        ParamSpec("rows", "Linhas", "int", 2, minimum=1, maximum=100, step=1, decimals=0,
                  help="Número de cópias na direção Y."),
        ParamSpec(
            "spacing_x", "Espaçamento entre colunas", "float", 3.0, unit="mm", minimum=0.0,
            maximum=500.0, step=0.5, decimals=2,
            help=(
                "Distância livre entre a borda de uma cópia e a da cópia seguinte em X. "
                "Precisa ser maior que o diâmetro da fresa de recorte (ex.: 3 mm para "
                "fresa de 2 mm) para que as placas possam ser separadas.")),
        ParamSpec(
            "spacing_y", "Espaçamento entre linhas", "float", 3.0, unit="mm", minimum=0.0,
            maximum=500.0, step=0.5, decimals=2,
            help="Distância livre entre cópias na direção Y (mesmas considerações do espaçamento X)."),
        ParamSpec(
            "use_reference", "Usar dimensões de referência", "bool", False, advanced=True,
            help=(
                "Por padrão o passo entre cópias é a largura/altura da própria camada mais o "
                "espaçamento. Ative para usar a largura/altura de referência abaixo "
                "(ex.: as do contorno da placa) — necessário para que cobre, furos e "
                "contorno, que têm tamanhos diferentes, fiquem alinhados.")),
        ParamSpec("ref_width", "Largura de referência", "float", 50.0, unit="mm", minimum=0.0,
                  decimals=3, advanced=True,
                  help="Largura da placa usada no cálculo do passo quando 'Usar dimensões de referência' está ativo."),
        ParamSpec("ref_height", "Altura de referência", "float", 50.0, unit="mm", minimum=0.0,
                  decimals=3, advanced=True,
                  help="Altura da placa usada no cálculo do passo quando 'Usar dimensões de referência' está ativo."),
        ParamSpec(
            "constrain", "Limitar ao tamanho da chapa", "bool", False,
            help=(
                "Reduz linhas/colunas para que o painel caiba inteiro na chapa de "
                "largura × altura abaixo.")),
        ParamSpec("max_width", "Largura da chapa", "float", 200.0, unit="mm", minimum=1.0, decimals=2,
                  help="Largura máxima disponível para o painel quando 'Limitar ao tamanho da chapa' está ativo."),
        ParamSpec("max_height", "Altura da chapa", "float", 150.0, unit="mm", minimum=1.0, decimals=2,
                  help="Altura máxima disponível para o painel quando 'Limitar ao tamanho da chapa' está ativo."),
    ),
))


def panel_offsets(bounds: tuple[float, float, float, float], settings: dict[str, Any]) -> np.ndarray:
    """Deslocamentos (K,2) de cada cópia (a primeira é (0, 0))."""
    s = PANELIZE.validate(settings)
    xmin, ymin, xmax, ymax = bounds
    w, h = xmax - xmin, ymax - ymin
    if s["use_reference"]:
        w, h = float(s["ref_width"]), float(s["ref_height"])
    px, py = w + float(s["spacing_x"]), h + float(s["spacing_y"])
    cols, rows = int(s["columns"]), int(s["rows"])
    if s["constrain"]:
        if px > 0:
            cols = max(1, min(cols, int((float(s["max_width"]) - w) // px) + 1))
        if py > 0:
            rows = max(1, min(rows, int((float(s["max_height"]) - h) // py) + 1))
    gx, gy = np.meshgrid(np.arange(cols) * px, np.arange(rows) * py)
    return np.column_stack([gx.ravel(), gy.ravel()])


def _tile_geom(g, offs: np.ndarray):
    parts = shapely.get_parts(g)
    if not len(parts):
        return g
    copies = [shapely.transform(parts, lambda c, dx=dx, dy=dy: c + (dx, dy)) for dx, dy in offs]
    allp = np.concatenate(copies)
    if _overlaps(offs, g.bounds):
        return shapely.union_all(allp)
    if len(polygons_of(allp)) == len(allp):
        return shapely.multipolygons(allp)
    return shapely.GeometryCollection(list(allp))


def _overlaps(offs: np.ndarray, b) -> bool:
    if len(offs) < 2:
        return False
    w, h = b[2] - b[0], b[3] - b[1]
    dx = np.diff(np.unique(offs[:, 0]))
    dy = np.diff(np.unique(offs[:, 1]))
    return bool((len(dx) and dx.min() < w) or (len(dy) and dy.min() < h))


def panelize(layer: GerberLayer | DrillLayer | GeometryLayer, settings: dict[str, Any],
             progress=None, cancel=None) -> GerberLayer | DrillLayer | GeometryLayer:
    """Retorna uma nova camada com ``linhas × colunas`` cópias de ``layer``."""
    prog = Progress(progress, cancel)
    prog(0.0, "Painelizando…")
    offs = panel_offsets(layer.bounds, settings)
    name = f"{layer.name} (painel)"
    if isinstance(layer, GerberLayer):
        cu = _tile_geom(layer.copper, offs)
        traces = [t + o for o in offs for t in layer.traces]
        out: Any = GerberLayer(name=name, source_path=layer.source_path, copper=cu,
                               apertures=dict(layer.apertures), traces=traces,
                               warnings=list(layer.warnings))
    elif isinstance(layer, DrillLayer):
        tools = {}
        for n, t in layer.tools.items():
            pts = np.asarray(t.points, dtype=float).reshape(-1, 2)
            sl = np.asarray(t.slots, dtype=float).reshape(-1, 4)
            tools[n] = DrillTool(number=t.number, diameter=t.diameter,
                                 points=(pts[None, :, :] + offs[:, None, :]).reshape(-1, 2),
                                 slots=(sl[None, :, :] + np.tile(offs, 2)[:, None, :]).reshape(-1, 4),
                                 plated=t.plated)
        out = DrillLayer(name=name, source_path=layer.source_path, tools=tools,
                         warnings=list(layer.warnings))
    elif isinstance(layer, GeometryLayer):
        out = GeometryLayer(name=name, geometry=_tile_geom(layer.geometry, offs),
                            source_path=layer.source_path)
    else:
        raise TypeError(f"Camada não suportada: {type(layer).__name__}")
    prog(1.0, "Painel criado.")
    return out


def panelize_job(job: Job, bounds: tuple[float, float, float, float], settings: dict[str, Any]) -> Job:
    """Replica os caminhos/furos de ``job`` no painel (mesmos deslocamentos de :func:`panelize`)."""
    offs = panel_offsets(bounds, settings)
    ops: list[PathOp] = []
    for op in job.ops:
        paths = [p + o for o in offs for p in op.paths]
        pts = (np.asarray(op.points)[None, :, :] + offs[:, None, :]).reshape(-1, 2) \
            if len(op.points) else op.points
        ops.append(replace(op, paths=paths, points=pts))
    return Job(name=f"{job.name} (painel)", source_id=job.source_id, ops=ops,
               operation=job.operation, settings=dict(job.settings))
