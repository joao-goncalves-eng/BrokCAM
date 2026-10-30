"""Recorte da placa (cutout) com pontes de fixação.

A fresa percorre o contorno da placa (do lado de fora, por padrão) atravessando toda
a espessura em várias passadas. Pequenos trechos ("pontes", *tabs*) ficam sem cortar
para a placa não se soltar durante a usinagem; depois são quebrados/lixados.

Fontes de contorno aceitas:

* ``GeometryLayer`` com polígonos (placa sólida) ou linhas (DXF/Edge.Cuts) — linhas são
  poligonizadas e combinadas pela regra par-ímpar (recortes internos viram furos);
* ``GerberLayer`` de contorno (Edge.Cuts desenhado com traço) — a linha central do traço
  é reconstruída;
* qualquer camada → retângulo envolvente ou forma convexa, mais margem.
"""
from __future__ import annotations

from dataclasses import replace
from typing import Any

import numpy as np
import shapely
from shapely.geometry.base import BaseGeometry
from shapely.ops import substring

from brokcam.cam.common import (
    Progress, cut_params_from, cut_params_specs, geom_to_paths, lines_of, optimize_specs,
    orient_for_milling, polygons_of, start_point, tool_from, tool_specs,
)
from brokcam.cam.optimize import optimize_paths, optimize_points
from brokcam.core.model import DrillLayer, GerberLayer, GeometryLayer, Job, PathOp, Tool
from brokcam.core.params import ParamGroup, ParamSpec, register

CUTOUT = register(ParamGroup(
    key="cutout",
    title="Recorte da placa",
    description=(
        "Gera o percurso que separa a placa do material ao redor, atravessando toda a "
        "espessura (multiprofundidade). Pontes (tabs) mantêm a placa presa até o fim "
        "para que ela não se solte e seja danificada pela fresa. Use uma fresa de topo "
        "(milho) de 1–2 mm; V-bits não servem para recorte. Faça o recorte por último, "
        "depois do isolamento e da furação."),
    params=(
        *tool_specs(defaults={"tool_dia": 1.0}, allow_vbit=False),
        ParamSpec(
            "source", "Forma do recorte", "choice", "auto",
            choices=(("auto", "Automática"), ("outline", "Contorno da camada"),
                     ("rect", "Retângulo envolvente"), ("convex", "Forma convexa")),
            help=(
                "Automática: usa o contorno quando a camada é um contorno (Edge.Cuts, DXF, "
                "geometria) e o retângulo envolvente nos demais casos (ex.: camada de "
                "cobre). Contorno da camada: segue exatamente o desenho da borda, "
                "inclusive recortes internos e cantos arredondados. Retângulo "
                "envolvente: o menor retângulo que contém a camada, mais a margem. "
                "Forma convexa: a 'casca' que envolve a camada (bom para placas "
                "triangulares/irregulares sem contorno desenhado).")),
        ParamSpec(
            "margin", "Margem", "float", 0.0, unit="mm", minimum=-20.0, maximum=50.0, step=0.5,
            decimals=2,
            help=(
                "Distância extra entre a borda da placa (ou do cobre, no retângulo "
                "envolvente) e o corte. Com 'Retângulo envolvente' de uma camada de "
                "cobre use 1–3 mm para não cortar rente às trilhas. Com o contorno "
                "desenhado normalmente fica 0.")),
        ParamSpec(
            "side", "Lado do corte", "choice", "outside",
            choices=(("outside", "Por fora da linha"), ("inside", "Por dentro da linha"),
                     ("on", "Sobre a linha")),
            help=(
                "Compensação do raio da ferramenta. Por fora: o centro da fresa passa a "
                "meio diâmetro do lado de fora da borda e a placa sai com a medida exata "
                "do desenho (o normal). Por dentro: a fresa corta do lado de dentro — "
                "útil para abrir janelas/rasgos onde o que importa é o furo. Sobre a "
                "linha: o centro da fresa segue a linha desenhada (a placa perde meio "
                "diâmetro de cada lado).")),
        ParamSpec(
            "milling", "Sentido de fresamento", "choice", "climb",
            choices=(("climb", "Concordante (climb)"), ("conventional", "Convencional")),
            help=(
                "Concordante deixa a borda da placa mais lisa em máquinas rígidas; "
                "convencional é mais tolerante a folgas e reduz o puxão da fresa em "
                "máquinas leves. Em FR-4 ambas funcionam; se a fresa 'puxar' ou vibrar, "
                "experimente o convencional.")),
        ParamSpec(
            "gaps", "Pontes", "choice", "4",
            choices=(("none", "Nenhuma"), ("lr", "2 — esquerda/direita"), ("tb", "2 — topo/base"),
                     ("4", "4 — uma por lado"), ("2lr", "4 — duas à esquerda e à direita"),
                     ("2tb", "4 — duas no topo e na base"), ("8", "8 — duas por lado"),
                     ("auto", "Automáticas pelo perímetro")),
            help=(
                "Quantidade e posição das pontes que seguram a placa (como no FlatCAM). "
                "As posições são calculadas pelo retângulo envolvente de cada contorno e "
                "projetadas no percurso. 'Automáticas' distribui pontes igualmente ao "
                "longo do perímetro, uma a cada 'Espaçamento das pontes' (mínimo 2). "
                "Placas pequenas: 2 pontes; médias: 4; grandes ou finas: 8. Sem pontes "
                "a placa pode se soltar no último passe e ser arrancada pela fresa.")),
        ParamSpec(
            "gap_size", "Largura da ponte", "float", 2.0, unit="mm", minimum=0.0, maximum=50.0,
            step=0.5, decimals=2,
            help=(
                "Largura do trecho de material que fica sem cortar em cada ponte, medida "
                "na borda da placa (o diâmetro da fresa já é descontado automaticamente). "
                "1,5–3 mm segura bem placas de FR-4 de 1,6 mm e ainda quebra com um "
                "alicate. Pontes largas demais exigem serra ou lima depois.")),
        ParamSpec(
            "gap_spacing", "Espaçamento das pontes", "float", 60.0, unit="mm", minimum=5.0,
            maximum=1000.0, step=5.0, decimals=1, advanced=True,
            help="Distância aproximada entre pontes no modo 'Automáticas pelo perímetro'."),
        ParamSpec(
            "gap_type", "Tipo de ponte", "choice", "bridge",
            choices=(("bridge", "Ponte inteira"), ("thin", "Ponte fina"),
                     ("mousebites", "Mordida de rato (furos)")),
            help=(
                "Ponte inteira: o trecho não é cortado em nenhuma profundidade (mais "
                "firme). Ponte fina: o trecho é cortado até a 'Profundidade da ponte fina', "
                "deixando só uma lâmina que se quebra com os dedos. Mordida de rato "
                "(mouse bites): a ponte fica inteira, mas é perfurada por uma fileira de "
                "furos pequenos ao longo da borda, como nas placas industriais em painel; "
                "gera uma operação de furação extra.")),
        ParamSpec(
            "gap_depth", "Profundidade da ponte fina", "float", -1.0, unit="mm", minimum=-10.0,
            maximum=0.0, step=0.1, decimals=2,
            help=(
                "Z de corte usado sobre as pontes finas (negativo). Para placa de 1,6 mm, "
                "-1,0 a -1,2 mm deixa 0,4–0,6 mm de material: firme o bastante para "
                "usinar e fácil de quebrar.")),
        ParamSpec(
            "mb_dia", "Diâmetro dos furos (mordida)", "float", 0.5, unit="mm", minimum=0.1,
            maximum=3.0, step=0.1, decimals=2, advanced=True,
            help="Diâmetro da broca usada nas mordidas de rato. 0,5–0,8 mm é o padrão industrial."),
        ParamSpec(
            "mb_spacing", "Espaçamento dos furos (mordida)", "float", 0.8, unit="mm", minimum=0.1,
            maximum=10.0, step=0.1, decimals=2, advanced=True,
            help=(
                "Distância entre centros dos furos das mordidas de rato. Deve ser maior que "
                "o diâmetro dos furos (sobra uma parede entre eles); 1,5× o diâmetro é "
                "um bom valor.")),
        *cut_params_specs(
            defaults={"cut_z": -1.7, "feed_xy": 120.0, "feed_z": 40.0, "multi_depth": True,
                      "depth_per_pass": 0.4, "travel_z": 2.0},
            help_extra={"cut_z": "Para recorte, use a espessura da placa + 0,1 mm (ex.: -1,7 mm "
                                 "para 1,6 mm); proteja a mesa com uma placa de sacrifício."}),
        *optimize_specs(),
    ),
))


# =========================================================================== contorno

def _even_odd(polys: np.ndarray) -> BaseGeometry:
    """Combina polígonos pela regra par-ímpar (contorno externo − recortes internos...)."""
    polys = polys[shapely.area(polys) > 1e-9] if len(polys) else polys
    if not len(polys):
        return shapely.Polygon()
    filled = shapely.polygons(shapely.get_exterior_ring(polys))
    tree = shapely.STRtree(filled)
    # ponto representativo da FACE (não do polígono preenchido): fica fora dos furos
    reps = shapely.point_on_surface(polys)
    a, b = tree.query(reps, predicate="within")   # a: ponto de i dentro de filled[b]
    depth = np.bincount(a[a != b], minlength=len(filled))
    even = filled[depth % 2 == 0]
    odd = filled[depth % 2 == 1]
    g = shapely.union_all(even)
    if len(odd):
        g = shapely.difference(g, shapely.union_all(odd))
    return g


def board_from_geometry(geom: BaseGeometry) -> BaseGeometry:
    """Reconstrói a área da placa a partir de um contorno (linhas ou traços com largura).

    * Linhas → poligonizadas (com nós nas interseções) e combinadas por par-ímpar.
    * Polígonos com furos e "finos" (traços de Edge.Cuts) → a linha central do traço é
      estimada: cada anel interno é expandido de meia largura do traço.
    * Polígonos sólidos → usados como estão.
    """
    lines = lines_of(geom)
    faces: list[np.ndarray] = []
    if len(lines):
        noded = shapely.union_all(lines)
        pg = shapely.polygonize(shapely.get_parts(noded))
        faces.append(polygons_of(pg))
    polys = polygons_of(geom)
    if len(polys):
        nint = shapely.get_num_interior_rings(polys)
        solid = polys[nint == 0]
        stroke = polys[nint > 0]
        faces.append(solid)
        if len(stroke):
            perim_total = shapely.length(shapely.boundary(stroke))
            w = 2.0 * shapely.area(stroke) / np.maximum(perim_total, 1e-9)
            filled_area = shapely.area(shapely.polygons(shapely.get_exterior_ring(stroke)))
            thin = shapely.area(stroke) < 0.5 * filled_area
            # anéis internos dos traços finos -> linha central
            mids: list[BaseGeometry] = []
            for p, wi, t in zip(stroke, w, thin):
                if not t:
                    faces.append(np.array([p], dtype=object))
                    continue
                for ir in p.interiors:
                    mids.append(shapely.buffer(shapely.Polygon(ir), wi / 2.0, join_style="mitre"))
            if mids:
                faces.append(np.array(mids, dtype=object))
    allf = np.concatenate([f for f in faces if len(f)]) if any(len(f) for f in faces) else \
        np.empty(0, dtype=object)
    return _even_odd(polygons_of(allf)) if len(allf) else shapely.Polygon()


def _looks_like_outline(layer: GerberLayer | GeometryLayer) -> bool:
    if isinstance(layer, GeometryLayer):
        return True
    polys = polygons_of(layer.copper)
    if not len(polys) or len(polys) > 50:
        return False
    ext_area = shapely.area(shapely.polygons(shapely.get_exterior_ring(polys)))
    # traço fino envolvendo uma área grande
    return bool(np.all(shapely.get_num_interior_rings(polys) > 0)
                and shapely.area(polys).sum() < 0.3 * ext_area.sum())


def _source_geometry(layer: Any) -> BaseGeometry:
    if isinstance(layer, GerberLayer):
        return layer.copper
    if isinstance(layer, GeometryLayer):
        return layer.geometry
    if isinstance(layer, DrillLayer):
        return shapely.box(*layer.bounds)
    raise TypeError(f"Camada não suportada no recorte: {type(layer).__name__}")


def board_shape(layer: Any, settings: dict[str, Any]) -> BaseGeometry:
    """Área da placa (antes da compensação da ferramenta) conforme ``source`` e ``margin``."""
    src = settings.get("source", "auto")
    margin = float(settings.get("margin", 0.0))
    geom = _source_geometry(layer)
    if src == "auto":
        src = "outline" if not isinstance(layer, DrillLayer) and _looks_like_outline(layer) else "rect"
    if src == "outline":
        g = board_from_geometry(geom)
        if g.is_empty:
            g = shapely.box(*geom.bounds)
        return shapely.buffer(g, margin, join_style="round") if margin else g
    if src == "convex":
        g = shapely.convex_hull(geom)
        return shapely.buffer(g, margin) if margin else g
    g = shapely.box(*geom.bounds)
    return shapely.buffer(g, margin, join_style="mitre") if margin else g


# =========================================================================== pontes

def gap_targets(ring_bounds: tuple[float, float, float, float], mode: str) -> list[tuple[float, float]]:
    """Pontos-alvo das pontes para um contorno com a caixa envolvente ``ring_bounds``."""
    xmin, ymin, xmax, ymax = ring_bounds
    cx, cy = (xmin + xmax) / 2.0, (ymin + ymax) / 2.0
    h, w = ymax - ymin, xmax - xmin
    lr = [(xmin, cy), (xmax, cy)]
    tb = [(cx, ymin), (cx, ymax)]
    lr2 = [(x, ymin + f * h) for x in (xmin, xmax) for f in (0.25, 0.75)]
    tb2 = [(xmin + f * w, y) for y in (ymin, ymax) for f in (0.25, 0.75)]
    return {"none": [], "lr": lr, "tb": tb, "4": lr + tb, "2lr": lr2, "2tb": tb2,
            "8": lr2 + tb2}.get(mode, [])


def _gap_positions(ring: shapely.LineString, mode: str, spacing: float) -> np.ndarray:
    """Distâncias (ao longo do anel) dos centros das pontes."""
    L = ring.length
    if mode == "none" or L <= 0:
        return np.zeros(0)
    if mode == "auto":
        n = max(2, int(round(L / max(spacing, 1e-6))))
        return (np.arange(n) + 0.5) * (L / n)
    tg = gap_targets(ring.bounds, mode)
    if not tg:
        return np.zeros(0)
    return np.unique(np.round(shapely.line_locate_point(ring, shapely.points(tg)), 9))


def _split_ring(coords: np.ndarray, centers: np.ndarray, length: float
                ) -> tuple[list[np.ndarray], list[np.ndarray]]:
    """Divide um anel fechado em trechos cortados e trechos de ponte.

    ``centers`` = distâncias dos centros das pontes; ``length`` = comprimento da ponte
    medido ao longo do percurso. Retorna (cortes, pontes).
    """
    ring = shapely.LineString(coords)
    L = ring.length
    if not len(centers) or length <= 0:
        return [coords], []
    if length * len(centers) >= L:
        return [], [coords]
    c = np.sort(centers % L)
    half = length / 2.0
    # gira o anel para começar no meio de uma ponte: os trechos ficam sem "volta"
    start = (c[0]) % L
    # parametrização desenrolada: [start, start+L)
    rel = (c - start) % L          # rel[0] == 0
    cuts: list[np.ndarray] = []
    bridges: list[np.ndarray] = []
    def piece(a: float, b: float) -> np.ndarray | None:
        a_abs, b_abs = start + a, start + b
        if b - a <= 1e-9:
            return None
        if b_abs <= L:
            g = substring(ring, a_abs, b_abs)
            return np.asarray(g.coords)
        if a_abs >= L:
            g = substring(ring, a_abs - L, b_abs - L)
            return np.asarray(g.coords)
        g1 = np.asarray(substring(ring, a_abs, L).coords)
        g2 = np.asarray(substring(ring, 0.0, b_abs - L).coords)
        return np.vstack([g1, g2[1:]])

    n = len(rel)
    for i in range(n):
        a = rel[i] + half
        b = (rel[i + 1] - half) if i + 1 < n else (L - half)
        p = piece(a, b)
        if p is not None and len(p) >= 2:
            cuts.append(p)
        lo = rel[i] - half
        hi = rel[i] + half
        if lo < 0:
            p1 = piece(L + lo, L)
            p2 = piece(0.0, hi)
            if p1 is not None and p2 is not None:
                bridges.append(np.vstack([p1, p2[1:]]))
        else:
            p = piece(lo, hi)
            if p is not None:
                bridges.append(p)
    return cuts, bridges


def _points_along(path: np.ndarray, spacing: float) -> np.ndarray:
    line = shapely.LineString(path)
    L = line.length
    n = max(1, int(np.floor(L / spacing)) + 1)
    ds = np.linspace(0.0, L, n) if n > 1 else np.array([L / 2.0])
    if n > 1:
        ds = ds - ds.mean() + L / 2.0
    return shapely.get_coordinates(shapely.line_interpolate_point(line, ds))


# =========================================================================== operação

def cutout(layer: GerberLayer | GeometryLayer | DrillLayer, settings: dict[str, Any],
           progress=None, cancel=None) -> Job:
    """Gera o trabalho de recorte da placa (grupo ``CUTOUT``).

    Retorna até três operações: corte principal (com as pontes abertas), passes sobre
    as pontes finas (``gap_type='thin'``, Z = ``gap_depth``) e furação das mordidas de
    rato (``gap_type='mousebites'``). ``job.settings['gap_points']`` lista os centros
    das pontes.
    """
    prog = Progress(progress, cancel)
    s = CUTOUT.validate(settings)
    s_tool = {**s, "tool_shape": "flat"}
    params = cut_params_from(s)
    tool = tool_from(s_tool)
    d = tool.diameter
    r = d / 2.0
    climb = s["milling"] == "climb"
    warnings: list[str] = []
    prog(0.0, "Calculando o contorno da placa…")

    board = board_shape(layer, s)
    if board.is_empty:
        raise ValueError("Não foi possível determinar o contorno da placa.")
    if s["side"] == "outside":
        cut_poly = shapely.buffer(board, r, quad_segs=16)
        cut_poly = orient_for_milling(cut_poly, climb)
    elif s["side"] == "inside":
        cut_poly = shapely.buffer(board, -r, quad_segs=16)
        if cut_poly.is_empty:
            raise ValueError("A ferramenta é maior que a área a recortar por dentro.")
        cut_poly = orient_for_milling(cut_poly, not climb)
    else:
        cut_poly = orient_for_milling(board, climb)
    prog.check()

    gap_size = float(s["gap_size"])
    # no percurso, a ponte precisa ser mais longa que a largura desejada: a fresa
    # come meio diâmetro de cada lado da interrupção
    gap_len = gap_size + d
    use_gaps = s["gaps"] != "none" and gap_size > 0
    cuts: list[np.ndarray] = []
    bridges: list[np.ndarray] = []
    gap_points: list[tuple[float, float]] = []
    mb_points: list[np.ndarray] = []
    board_edges = shapely.get_parts(shapely.boundary(board))
    for poly in polygons_of(cut_poly):
        rings = geom_to_paths(poly)
        if not rings:
            continue
        ext, interiors = rings[0], rings[1:]
        cuts.extend(interiors)                  # recortes internos: sem pontes
        if not use_gaps:
            cuts.append(ext)
            continue
        ring = shapely.LineString(ext)
        centers = _gap_positions(ring, s["gaps"], float(s["gap_spacing"]))
        gp = shapely.get_coordinates(shapely.line_interpolate_point(ring, centers)) if len(centers)             else np.zeros((0, 2))
        gap_points.extend(map(tuple, gp.tolist()))
        if s["gap_type"] == "mousebites":
            # a ponte fica inteira e recebe uma fileira de furos sobre a borda real da placa
            c_cut, _ = _split_ring(ext, centers, gap_len)
            cuts.extend(c_cut)
            for pt in gp:
                P = shapely.Point(pt)
                k = int(np.argmin(shapely.distance(board_edges, P)))
                bl = board_edges[k]
                dist = bl.project(P)
                seg = substring(bl, max(dist - gap_size / 2.0, 0.0), min(dist + gap_size / 2.0, bl.length))
                if seg.length > 0:
                    mb_points.append(_points_along(np.asarray(seg.coords), float(s["mb_spacing"])))
            continue
        c_cut, c_br = _split_ring(ext, centers, gap_len)
        cuts.extend(c_cut)
        bridges.extend(c_br)

    if use_gaps and not gap_points:
        warnings.append("Nenhuma ponte pôde ser posicionada.")

    ops: list[PathOp] = []
    cur = start_point(s)
    tl = float(s["optimize_time"])
    if s["optimize"] and cuts:
        cuts = optimize_paths(cuts, cur, allow_reverse=False, time_limit=tl)
    if cuts:
        ops.append(PathOp("mill", tool, params, paths=cuts, label="Recorte"))
        cur = tuple(cuts[-1][-1])
    if s["gap_type"] == "thin" and bridges:
        gz = -abs(float(s["gap_depth"]))
        if gz <= params.cut_z:
            warnings.append("Profundidade da ponte fina é igual ou maior que o Z de corte; as "
                            "pontes seriam cortadas por completo.")
        thin_params = replace(params, cut_z=max(gz, params.cut_z))
        if s["optimize"]:
            bridges = optimize_paths(bridges, cur, allow_reverse=True, time_limit=tl)
        ops.append(PathOp("mill", tool, thin_params, paths=bridges, label="Pontes finas"))
    if s["gap_type"] == "mousebites" and mb_points:
        pts = np.vstack(mb_points)
        if s["optimize"]:
            pts = optimize_points(pts, cur, tl)
        mb_tool = Tool(diameter=float(s["mb_dia"]), name=f"Broca {float(s['mb_dia']):g} mm",
                       number=tool.number + 1, shape="drill")
        mb_params = replace(params, multi_depth=False)
        ops.append(PathOp("drill", mb_tool, mb_params, points=pts, label="Mordidas de rato"))
    prog(1.0, "Recorte concluído.")
    return Job(name=f"{layer.name} — recorte", source_id=layer.id, ops=ops, operation="cutout",
               settings={**s, "warnings": warnings, "gap_points": gap_points,
                         "board_bounds": tuple(board.bounds)})
