"""Limpeza de cobre (Non-Copper Clearing, NCC).

Remove TODO o cobre que não pertence a trilhas/pads dentro de uma área, em vez de
apenas isolar. Calcula-se a *região permitida para o centro da ferramenta*::

    centro = (área ⊖ r) − (cobre ⊕ (r + afastamento))

e ela é preenchida por:

* **linhas** — varredura paralela (raster/zigue-zague) calculada por um preenchimento
  por linhas de varredura (scanline) totalmente vetorizado em numpy;
* **offset** — contornos concêntricos para dentro (``centro ⊖ k·passo``);
* **combinado** — alguns contornos junto ao cobre (bom acabamento) e linhas no miolo.
"""
from __future__ import annotations

import math
from typing import Any

import numpy as np
import shapely
from shapely.geometry.base import BaseGeometry

from brokcam.cam.common import (
    Progress, area_geometry, chord_safe, cut_params_from, cut_params_specs, optimize_specs,
    orient_for_milling, polygons_of, resolution_specs, rings_to_paths, start_point, tool_from, enable_vbit_auto_z,
    tool_specs,
)
from brokcam.cam.optimize import optimize_paths
from brokcam.core.model import GerberLayer, GeometryLayer, Job, PathOp
from brokcam.core.params import ParamGroup, ParamSpec, register

NCC = register(ParamGroup(
    key="ncc",
    title="Limpeza de cobre (NCC)",
    description=(
        "Remove todo o cobre que não faz parte do circuito dentro da área escolhida "
        "(Non-Copper Clearing). Deixa a placa com aparência de placa industrial, "
        "reduz o risco de pontes de solda e capacitâncias parasitas, mas é MUITO mais "
        "demorada que o isolamento. Use uma fresa de topo (0,8–2 mm) para as áreas "
        "grandes e, se necessário, isole antes com uma V-bit fina para garantir a "
        "separação entre trilhas próximas."),
    params=(
        *tool_specs(defaults={"tool_shape": "flat", "tool_dia": 0.8}),
        ParamSpec(
            "method", "Método", "choice", "lines",
            choices=(("lines", "Linhas (zigue-zague)"), ("offset", "Contornos (offset)"),
                     ("combo", "Combinado")),
            help=(
                "Linhas: varre a área com passadas paralelas, ligadas em zigue-zague. É o "
                "mais rápido de calcular e de usinar em áreas grandes e abertas, mas deixa "
                "as bordas serrilhadas (ative 'Contorno final'). Contornos: corta anéis "
                "concêntricos a partir do cobre para dentro; acabamento excelente junto às "
                "trilhas, porém é mais lento de calcular em placas grandes e gera muitas "
                "subidas de ferramenta. Combinado: alguns contornos junto ao cobre "
                "(acabamento) e linhas no miolo (velocidade) — o melhor dos dois.")),
        ParamSpec(
            "overlap", "Sobreposição", "float", 40.0, unit="%", minimum=0.0, maximum=90.0,
            step=5.0, decimals=1,
            help=(
                "Quanto cada passada se sobrepõe à anterior, em % do diâmetro da "
                "ferramenta. Comece com 30–40%; se sobrarem fios de cobre entre as "
                "passadas (comum com V-bit, cuja largura varia), aumente. Valores altos "
                "multiplicam o tempo de usinagem e o tamanho do G-code.")),
        ParamSpec(
            "area", "Área a limpar", "choice", "bbox",
            choices=(("bbox", "Retângulo envolvente"), ("convex", "Contorno convexo"),
                     ("boundary", "Contorno da placa"), ("region", "Região selecionada")),
            help=(
                "Retângulo envolvente: o menor retângulo que contém todo o cobre, ampliado "
                "pela margem. Contorno convexo: a 'casca' que envolve o cobre (economiza "
                "tempo em placas não retangulares). Contorno da placa: a geometria do "
                "contorno (ex.: camada Edge.Cuts) informada pela interface. Região "
                "selecionada: a área desenhada na tela.")),
        ParamSpec(
            "margin", "Margem", "float", 1.0, unit="mm", minimum=-50.0, maximum=50.0, step=0.5,
            decimals=2,
            help=(
                "Quanto a área a limpar se estende além do cobre (ou encolhe, se "
                "negativa). Para retângulo/convexo, 1–2 mm garante limpar até a borda; "
                "para contorno da placa use 0 ou negativo para não fresar além da "
                "borda.")),
        ParamSpec(
            "clearance", "Afastamento do cobre", "float", 0.0, unit="mm", minimum=0.0, maximum=10.0,
            step=0.05, decimals=3,
            help=(
                "Distância mínima entre o corte e o cobre do circuito. 0 limpa encostado "
                "nas trilhas (a ferramenta tangencia o cobre). Valores como 0,2–0,5 mm "
                "deixam uma faixa de cobre ao redor das trilhas — útil quando o "
                "isolamento fino já foi feito com outra ferramenta e você quer só "
                "remover as grandes áreas, poupando a fresa e o tempo.")),
        ParamSpec(
            "milling", "Sentido de fresamento", "choice", "climb",
            choices=(("climb", "Concordante (climb)"), ("conventional", "Convencional")),
            help=(
                "Sentido de percurso dos contornos (métodos Contornos e Combinado, e do "
                "contorno final). Concordante dá melhor acabamento em máquinas com pouca "
                "folga; convencional tolera melhor folga mecânica. As linhas do "
                "zigue-zague alternam o sentido por natureza.")),
        ParamSpec(
            "line_angle", "Ângulo das linhas", "float", 0.0, unit="°", minimum=-180.0, maximum=180.0,
            step=15.0, decimals=1, advanced=True,
            help=(
                "Direção das passadas do método Linhas (0° = paralelas ao eixo X, 90° = "
                "paralelas ao eixo Y). Escolha a direção do eixo mais rígido/rápido da "
                "sua máquina ou a direção predominante das áreas a limpar.")),
        ParamSpec(
            "connect", "Conectar caminhos", "bool", True,
            help=(
                "Liga o fim de uma passada ao início da seguinte com um movimento de corte "
                "(sem levantar a ferramenta), sempre que a ligação ficar inteiramente "
                "dentro da área a limpar. Reduz drasticamente o número de subidas/"
                "descidas e o tempo total. Desative apenas para depuração.")),
        ParamSpec(
            "contour", "Contorno final", "bool", True,
            help=(
                "Acrescenta um passe contornando o cobre e a borda da área, removendo o "
                "serrilhado deixado pelas extremidades das linhas. Recomendado com o "
                "método Linhas.")),
        ParamSpec(
            "combo_rings", "Contornos no modo combinado", "int", 2, minimum=1, maximum=20, step=1,
            decimals=0, advanced=True,
            help=(
                "Quantos contornos concêntricos são cortados junto ao cobre antes de "
                "preencher o miolo com linhas, no método Combinado. 1–3 é o usual.")),
        ParamSpec(
            "max_rings", "Limite de contornos", "int", 500, minimum=1, maximum=10000, step=10,
            decimals=0, advanced=True,
            help=(
                "Número máximo de contornos concêntricos do método Contornos (proteção "
                "contra cálculos muito longos em áreas enormes). Se o limite for "
                "atingido sobra cobre no centro das áreas e um aviso é emitido.")),
        *resolution_specs(),
        *cut_params_specs(defaults={"cut_z": -0.08, "feed_xy": 150.0, "feed_z": 60.0}),
        *optimize_specs(),
    ),
))
enable_vbit_auto_z(NCC)


# =========================================================================== scanline

def _ring_edges(geom: BaseGeometry) -> np.ndarray:
    """Todas as arestas (x0, y0, x1, y1) dos anéis dos polígonos de ``geom``."""
    polys = polygons_of(geom)
    if not len(polys):
        return np.zeros((0, 4))
    rings = shapely.get_rings(polys)
    coords, idx = shapely.get_coordinates(rings, return_index=True)
    same = idx[1:] == idx[:-1]
    a = coords[:-1][same]
    b = coords[1:][same]
    return np.hstack([a, b])


def scanline_segments(geom: BaseGeometry, step: float, angle_deg: float = 0.0,
                      offset: float | None = None) -> list[np.ndarray]:
    """Preenche ``geom`` com segmentos paralelos espaçados de ``step`` (vetorizado).

    Retorna uma lista de "linhas de varredura", cada uma um array (K, 2, 2) de
    segmentos ordenados por x (no sistema girado), já de volta às coordenadas reais.
    Usa a regra par-ímpar sobre as arestas do polígono (furos incluídos).
    """
    edges = _ring_edges(geom)
    if not len(edges) or step <= 0:
        return []
    th = math.radians(angle_deg)
    c, s = math.cos(th), math.sin(th)
    # gira por -θ: as linhas passam a ser horizontais
    x0 = edges[:, 0] * c + edges[:, 1] * s
    y0 = -edges[:, 0] * s + edges[:, 1] * c
    x1 = edges[:, 2] * c + edges[:, 3] * s
    y1 = -edges[:, 2] * s + edges[:, 3] * c
    ymin_all = float(min(y0.min(), y1.min()))
    ymax_all = float(max(y0.max(), y1.max()))
    base = ymin_all + (step / 2.0 if offset is None else offset)
    nlines = int(math.floor((ymax_all - base) / step)) + 1
    if nlines <= 0:
        return []
    lo = np.minimum(y0, y1)
    hi = np.maximum(y0, y1)
    keep = hi > lo                                # ignora arestas horizontais
    x0, y0, x1, y1, lo, hi = x0[keep], y0[keep], x1[keep], y1[keep], lo[keep], hi[keep]
    # linhas k com lo <= y_k < hi (meio-aberto evita contar vértices duas vezes)
    k0 = np.ceil((lo - base) / step).astype(np.int64)
    k1 = np.ceil((hi - base) / step).astype(np.int64) - 1
    k0 = np.maximum(k0, 0)
    k1 = np.minimum(k1, nlines - 1)
    cnt = np.maximum(k1 - k0 + 1, 0)
    total = int(cnt.sum())
    if total == 0:
        return []
    e = np.repeat(np.arange(len(cnt)), cnt)
    first = np.repeat(np.cumsum(cnt) - cnt, cnt)
    k = k0[e] + (np.arange(total) - first)
    yk = base + k * step
    t = (yk - y0[e]) / (y1[e] - y0[e])
    xk = x0[e] + t * (x1[e] - x0[e])
    order = np.lexsort((xk, k))
    k = k[order]
    xk = xk[order]
    # pares consecutivos dentro da mesma linha: (entrada, saída)
    out: list[np.ndarray] = []
    splits = np.flatnonzero(np.diff(k)) + 1
    for kk, xs in zip(np.split(k, splits), np.split(xk, splits)):
        if len(xs) < 2:
            continue
        m = len(xs) // 2 * 2
        xa = xs[0:m:2]
        xb = xs[1:m:2]
        good = xb - xa > 1e-9
        if not good.any():
            continue
        xa, xb = xa[good], xb[good]
        y = base + kk[0] * step
        # volta para as coordenadas reais (gira por +θ)
        ax = xa * c - y * s
        ay = xa * s + y * c
        bx = xb * c - y * s
        by = xb * s + y * c
        seg = np.stack([np.column_stack([ax, ay]), np.column_stack([bx, by])], axis=1)
        out.append(seg)
    return out


def _zigzag(lines: list[np.ndarray], region: BaseGeometry, connect: bool, tol: float) -> list[np.ndarray]:
    """Encadeia segmentos de linhas consecutivas em zigue-zague quando a ligação cabe na região."""
    if not lines:
        return []
    if not connect:
        return [seg for ln in lines for seg in ln]
    shapely.prepare(region)
    used = [np.zeros(len(ln), dtype=bool) for ln in lines]
    chains: list[np.ndarray] = []
    nl = len(lines)
    for li in range(nl):
        for si in range(len(lines[li])):
            if used[li][si]:
                continue
            used[li][si] = True
            pts = [lines[li][si][0], lines[li][si][1]]
            cur_line = li
            forward = True             # percorreu a->b
            while cur_line + 1 < nl:
                nxt = lines[cur_line + 1]
                end = pts[-1]
                # próximo segmento será percorrido no sentido oposto: entra pela ponta
                # do mesmo lado em que estamos
                cand_pts = nxt[:, 1] if forward else nxt[:, 0]
                free = ~used[cur_line + 1]
                if not free.any():
                    break
                d = np.hypot(cand_pts[:, 0] - end[0], cand_pts[:, 1] - end[1])
                d[~free] = np.inf
                j = int(np.argmin(d))
                if not np.isfinite(d[j]) or d[j] > tol:
                    break
                link = shapely.LineString([end, cand_pts[j]])
                if not shapely.covers(region, link):
                    break
                used[cur_line + 1][j] = True
                seg = nxt[j]
                if forward:
                    pts.extend([seg[1], seg[0]])
                else:
                    pts.extend([seg[0], seg[1]])
                forward = not forward
                cur_line += 1
            chains.append(np.array(pts))
    return chains


# =========================================================================== operação

def _clear_area(layer: GerberLayer | GeometryLayer, copper: BaseGeometry, s: dict[str, Any],
                raw: dict[str, Any]) -> BaseGeometry:
    margin = float(s["margin"])
    mode = s["area"]
    if mode == "boundary":
        g = area_geometry(raw, "boundary_geometry")
        if g is None:
            raise ValueError("Área 'Contorno da placa' escolhida, mas nenhum contorno foi informado "
                             "(settings['boundary_geometry']).")
        from brokcam.cam.cutout import board_from_geometry
        g = board_from_geometry(g)
    elif mode == "region":
        g = area_geometry(raw, "region_geometry")
        if g is None:
            raise ValueError("Área 'Região selecionada' escolhida, mas nenhuma região foi informada "
                             "(settings['region_geometry']).")
    elif mode == "convex":
        g = shapely.convex_hull(copper)
    else:
        g = shapely.box(*copper.bounds) if not copper.is_empty else shapely.Polygon()
    if margin:
        g = shapely.buffer(g, margin, join_style="mitre" if mode == "bbox" else "round")
    return g


def clear_copper(layer: GerberLayer | GeometryLayer, settings: dict[str, Any],
                 progress=None, cancel=None) -> Job:
    """Gera o trabalho de limpeza de cobre (grupo ``NCC``).

    Chaves extras aceitas em ``settings``: ``boundary_geometry`` (contorno da placa),
    ``region_geometry`` (região selecionada) e ``exclude_geometry`` (área proibida).
    ``job.settings`` recebe ``warnings`` e ``uncleared_area_mm2``.
    """
    prog = Progress(progress, cancel)
    raw = dict(settings)
    s = NCC.validate(settings)
    warnings: list[str] = []
    params = cut_params_from(s)
    tool = tool_from(s)
    d = tool.diameter
    r = d / 2.0
    step = d * (1.0 - float(s["overlap"]) / 100.0)
    qs = int(s["arc_segments"])
    tol = float(s["simplify_tol"])
    climb = s["milling"] == "climb"
    prog(0.0, "Preparando limpeza de cobre…")

    geom = layer.copper if isinstance(layer, GerberLayer) else layer.geometry
    parts = polygons_of(geom)
    copper = shapely.multipolygons(parts) if len(parts) else shapely.MultiPolygon()
    area = _clear_area(layer, copper, s, raw)
    if area.is_empty:
        warnings.append("Área a limpar vazia.")
        return Job(name=f"{layer.name} — limpeza de cobre", source_id=layer.id, ops=[],
                   operation="ncc", settings={**s, "warnings": warnings, "uncleared_area_mm2": 0.0})
    prog(0.05, "Calculando região livre para a ferramenta…")
    avoid = shapely.buffer(copper, chord_safe(r + float(s["clearance"]), qs), quad_segs=qs) if len(parts) else None
    inner = shapely.buffer(area, -r, quad_segs=qs, join_style="mitre")
    region = shapely.difference(inner, avoid) if avoid is not None else inner
    exclude = area_geometry(raw, "exclude_geometry")
    if exclude is not None:
        region = shapely.difference(region, shapely.buffer(exclude, r, quad_segs=qs))
    prog.check()

    def rings_of(g: BaseGeometry) -> list[np.ndarray]:
        gp = polygons_of(g)
        if tol > 0 and len(gp):
            gp = shapely.simplify(gp, tol, preserve_topology=False)
            gp = gp[~shapely.is_empty(gp)]
        return rings_to_paths(orient_for_milling(gp, climb))

    paths: list[np.ndarray] = []
    method = s["method"]
    if method in ("offset", "combo"):
        nmax = int(s["combo_rings"]) if method == "combo" else int(s["max_rings"])
        cur = region
        k = 0
        while not cur.is_empty and k < nmax:
            prog(0.1 + 0.6 * min(k / max(nmax, 1), 1.0), f"Contorno {k + 1}…")
            paths.extend(rings_of(cur))
            k += 1
            cur = shapely.buffer(cur, -step, quad_segs=qs)
        if method == "offset" and not cur.is_empty:
            warnings.append(f"Limite de {nmax} contornos atingido; pode sobrar cobre no centro de "
                            "áreas grandes.")
        if method == "combo" and not cur.is_empty:
            fill = shapely.buffer(region, -((k - 1) * step + step * 0.5), quad_segs=qs) if k else region
            prog(0.7, "Preenchendo o miolo com linhas…")
            lines = scanline_segments(fill, step, float(s["line_angle"]))
            paths.extend(_zigzag(lines, shapely.buffer(fill, 1e-6), bool(s["connect"]), step * 2.5))
    else:
        prog(0.2, "Gerando linhas de varredura…")
        lines = scanline_segments(region, step, float(s["line_angle"]))
        prog(0.5, "Conectando passadas…")
        paths.extend(_zigzag(lines, shapely.buffer(region, 1e-6), bool(s["connect"]), step * 2.5))
        if s["contour"]:
            paths.extend(rings_of(region))

    # cobre não alcançado (cantos estreitos, vãos menores que a ferramenta)
    prog(0.85, "Estimando áreas não alcançadas…")
    target = shapely.difference(area, shapely.buffer(copper, float(s["clearance"]))) \
        if len(parts) else area
    reach = shapely.buffer(region, r, quad_segs=qs)
    unc = max(float(target.area - shapely.intersection(reach, target).area), 0.0)
    if unc > 0.01 * max(target.area, 1e-9) and unc > d * d:
        warnings.append(f"Cerca de {unc:.1f} mm² não podem ser alcançados pela ferramenta de "
                        f"{d:.3f} mm (vãos estreitos/cantos). Use uma ferramenta menor para o resto.")

    if s["optimize"] and paths:
        prog(0.9, "Otimizando a ordem dos cortes…")
        paths = optimize_paths(paths, start_point(s), allow_reverse=True,
                               time_limit=float(s["optimize_time"]))
    ops = [PathOp("mill", tool, params, paths=paths, label="Limpeza de cobre")] if paths else []
    if not paths:
        warnings.append("Nada a limpar: a ferramenta não cabe em nenhuma área livre.")
    prog(1.0, "Limpeza de cobre concluída.")
    return Job(name=f"{layer.name} — limpeza de cobre", source_id=layer.id, ops=ops,
               operation="ncc", settings={**s, "warnings": warnings, "uncleared_area_mm2": unc})
