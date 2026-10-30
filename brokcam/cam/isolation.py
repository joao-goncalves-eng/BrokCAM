"""Isolamento de trilhas (fresagem por isolamento).

A ferramenta percorre contornos deslocados ("offset") ao redor de cada ilha de
cobre, removendo uma faixa estreita de cobre que separa eletricamente as trilhas.
O primeiro passe fica a meio diâmetro da borda do cobre; passes adicionais se
afastam ``diâmetro × (1 − sobreposição)`` cada.

Detecção de curtos: a ferramenta de diâmetro ``d`` não entra em vãos menores que
``d``. Fazemos o *fechamento* morfológico ``copper ⊕ r ⊖ r`` (r = d/2): ilhas de cobre
que se fundem nesse fechamento continuarão ligadas por cobre não cortado — são
reportadas como "pontes" com a localização e a largura do vão. Com usinagem de
resto (ferramentas menores), esses vãos são cortados pelas ferramentas seguintes.
"""
from __future__ import annotations

from typing import Any

import numpy as np
import shapely
from shapely.geometry.base import BaseGeometry

from brokcam.cam.common import (
    Progress, area_geometry, chord_safe, clip_paths, cut_params_from, cut_params_specs,
    optimize_specs, orient_for_milling, polygons_of, resolution_specs, rings_to_paths,
    start_point, tool_specs, tool_from, enable_vbit_auto_z,
)
from brokcam.cam.optimize import optimize_paths
from brokcam.core.model import GerberLayer, GeometryLayer, Job, PathOp, Tool
from brokcam.core.params import ParamGroup, ParamSpec, register

_JOIN = {"round": "round", "mitre": "mitre", "bevel": "bevel"}

ISOLATION = register(ParamGroup(
    key="isolation",
    title="Isolamento de trilhas",
    description=(
        "Gera os caminhos que a fresa percorre ao redor de cada trilha e pad, removendo "
        "uma faixa fina de cobre para separá-los eletricamente. É a operação principal "
        "da fabricação caseira de PCB por fresagem. O resultado depende muito do "
        "diâmetro efetivo da ferramenta: confira a largura real do sulco num corte de "
        "teste. O BrokCAM avisa onde a ferramenta é larga demais para passar entre "
        "duas ilhas de cobre (risco de curto) e pode usar ferramentas menores para "
        "terminar esses pontos (usinagem de resto)."),
    params=(
        *tool_specs(defaults={"tool_shape": "vbit", "tool_dia": 0.2}),
        ParamSpec(
            "passes", "Número de passes", "int", 1, minimum=1, maximum=20, step=1, decimals=0,
            help=(
                "Quantos contornos concêntricos são cortados ao redor de cada ilha de cobre. "
                "Um passe já isola eletricamente, mas deixa um vão estreito (igual ao "
                "diâmetro da ferramenta) que dificulta a soldagem e favorece pontes de "
                "solda. 2–3 passes alargam o vão e facilitam a soldagem manual; mais "
                "passes aumentam o tempo de usinagem proporcionalmente. Largura total "
                "isolada = d + (passes − 1)·d·(1 − sobreposição).")),
        ParamSpec(
            "overlap", "Sobreposição entre passes", "float", 10.0, unit="%", minimum=0.0,
            maximum=90.0, step=5.0, decimals=1,
            help=(
                "Quanto cada passe se sobrepõe ao anterior, em porcentagem do diâmetro da "
                "ferramenta. 0% faz passes tangentes (pode sobrar um fio de cobre entre "
                "eles por imprecisão da máquina); 10–20% é o recomendado; valores altos "
                "desperdiçam tempo, pois o vão cresce pouco por passe. Só tem efeito com "
                "2 ou mais passes.")),
        ParamSpec(
            "milling", "Sentido de fresamento", "choice", "climb",
            choices=(("climb", "Concordante (climb)"), ("conventional", "Convencional")),
            help=(
                "Define o sentido em que cada contorno é percorrido. Concordante (climb): o "
                "dente entra no material pela parte mais grossa do cavaco; dá melhor "
                "acabamento e menos rebarba no cobre, e desgasta menos a ferramenta — "
                "recomendado para máquinas com pouca folga (fusos ou correias bem "
                "tensionadas). Convencional: mais tolerante a folga mecânica (backlash), "
                "útil em máquinas com fuso trapezoidal frouxo. Considera spindle girando "
                "no sentido horário (M3); internamente, concordante = contornos externos "
                "no sentido horário e furos no anti-horário.")),
        ParamSpec(
            "combine", "Combinar passes", "bool", True,
            help=(
                "Quando ativado, todos os passes formam uma única operação, ordenada em "
                "conjunto para minimizar deslocamentos. Desativado, cada passe vira uma "
                "operação separada (todos os primeiros passes, depois os segundos...), o "
                "que permite conferir o primeiro passe antes de continuar ou exportar "
                "passes separadamente.")),
        ParamSpec(
            "iso_type", "Tipo de isolamento", "choice", "full",
            choices=(("full", "Completo"), ("ext", "Somente exterior"), ("int", "Somente interior")),
            help=(
                "Completo: isola o contorno externo e os furos internos de cada ilha de "
                "cobre (o normal). Somente exterior: ignora aberturas internas das ilhas "
                "(ex.: o interior de um anel de cobre), útil quando o interior será "
                "furado ou não importa. Somente interior: corta apenas dentro das "
                "aberturas internas das ilhas (polígonos em forma de 'rosca'); se a ilha "
                "não tem abertura não há nada a cortar.")),
        ParamSpec(
            "follow", "Seguir trilhas (follow)", "bool", False, advanced=True,
            help=(
                "Em vez de contornar o cobre, a ferramenta passa PELO MEIO das trilhas, "
                "seguindo as linhas centrais do Gerber. Não isola nada: serve para "
                "gravação com laser, caneta/plotter ou para cortar trilhas finas como "
                "fusíveis. Requer que o Gerber traga trilhas desenhadas (D01); pads e "
                "regiões preenchidas são ignorados.")),
        ParamSpec(
            "scope", "Área a isolar", "choice", "all",
            choices=(("all", "Placa inteira"), ("bbox", "Retângulo (coordenadas)"),
                     ("region", "Região selecionada"), ("points", "Ilhas clicadas")),
            help=(
                "Define quais ilhas de cobre são isoladas. Placa inteira: todas. "
                "Retângulo: apenas as ilhas que tocam o retângulo definido pelas "
                "coordenadas mínimas/máximas abaixo. Região selecionada: as ilhas que "
                "tocam a área desenhada na tela. Ilhas clicadas: apenas os polígonos "
                "clicados na tela. Útil para refazer só um trecho que ficou ruim sem "
                "repetir a placa inteira.")),
        ParamSpec("scope_xmin", "Retângulo X mín.", "float", 0.0, unit="mm", decimals=3, advanced=True,
                  help="Canto inferior esquerdo (X) do retângulo usado quando 'Área a isolar' é Retângulo."),
        ParamSpec("scope_ymin", "Retângulo Y mín.", "float", 0.0, unit="mm", decimals=3, advanced=True,
                  help="Canto inferior esquerdo (Y) do retângulo usado quando 'Área a isolar' é Retângulo."),
        ParamSpec("scope_xmax", "Retângulo X máx.", "float", 100.0, unit="mm", decimals=3, advanced=True,
                  help="Canto superior direito (X) do retângulo usado quando 'Área a isolar' é Retângulo."),
        ParamSpec("scope_ymax", "Retângulo Y máx.", "float", 100.0, unit="mm", decimals=3, advanced=True,
                  help="Canto superior direito (Y) do retângulo usado quando 'Área a isolar' é Retângulo."),
        ParamSpec(
            "clip_to_scope", "Cortar só dentro da área", "bool", False, advanced=True,
            help=(
                "Com 'Retângulo' ou 'Região selecionada', recorta os caminhos exatamente "
                "na área escolhida, em vez de isolar por completo as ilhas que a tocam. "
                "Útil para retrabalhar apenas uma parte de uma ilha grande (ex.: plano "
                "de terra).")),
        ParamSpec(
            "extra_offset", "Afastamento extra", "float", 0.0, unit="mm", minimum=-1.0, maximum=5.0,
            step=0.01, decimals=3, advanced=True,
            help=(
                "Distância adicional entre a borda do cobre e o primeiro passe. Valores "
                "positivos deixam as trilhas mais largas que o desenho (compensam uma "
                "ferramenta que corta mais largo que o informado ou corrosão posterior); "
                "negativos deixam as trilhas mais finas. Normalmente 0.")),
        ParamSpec(
            "check_bridges", "Detectar curtos (pontes)", "bool", True,
            help=(
                "Verifica onde a ferramenta é larga demais para passar entre duas ilhas de "
                "cobre vizinhas (vão menor que o diâmetro efetivo). Nesses pontos o cobre "
                "não é cortado e as ilhas ficam em curto. Cada ocorrência é reportada com "
                "posição X/Y e largura do vão, para que você reduza a profundidade, use "
                "ferramenta mais fina ou ative a usinagem de resto. Custa algum tempo de "
                "processamento em placas grandes.")),
        ParamSpec(
            "rest_machining", "Usinagem de resto", "bool", False,
            help=(
                "Usa ferramentas menores (lista abaixo) apenas nos pontos onde a ferramenta "
                "principal não conseguiu passar, evitando curtos sem precisar isolar a "
                "placa inteira com a ferramenta mais fina (que é lenta e frágil). Cada "
                "ferramenta extra gera uma operação separada, com troca de ferramenta.")),
        ParamSpec(
            "rest_tool_dias", "Ferramentas de resto (mm)", "str", "0.1",
            help=(
                "Diâmetros das ferramentas menores usadas na usinagem de resto, separados "
                "por vírgula, ex.: '0.15, 0.1'. São usadas da maior para a menor; "
                "diâmetros iguais ou maiores que o da ferramenta principal são ignorados. "
                "Para V-bits informe a largura efetiva na profundidade de corte.")),
        ParamSpec(
            "join_style", "Cantos do contorno", "choice", "round", advanced=True,
            choices=(("round", "Arredondados"), ("mitre", "Vivos (mitre)"), ("bevel", "Chanfrados")),
            help=(
                "Forma dos cantos externos dos contornos deslocados. Arredondados é o "
                "geometricamente correto para uma fresa redonda e mantém distância "
                "constante do cobre. Vivos produzem cantos retos (menos vértices, porém "
                "o passe se afasta mais do cobre nos cantos). Chanfrados ficam no meio "
                "termo.")),
        *resolution_specs(),
        *cut_params_specs(
            defaults={"cut_z": -0.05, "feed_xy": 120.0, "feed_z": 60.0, "depth_per_pass": 0.05},
            help_extra={"cut_z": "Com fresa em V este campo é calculado automaticamente a partir "
                                 "da largura do sulco e fica travado."}),
        *optimize_specs(),
    ),
))
enable_vbit_auto_z(ISOLATION)


def _parse_dias(text: str) -> list[float]:
    out: list[float] = []
    for tok in str(text).replace(";", ",").split(","):
        tok = tok.strip().replace(" ", "")
        if not tok:
            continue
        try:
            v = float(tok)
        except ValueError:
            continue
        if v > 0:
            out.append(v)
    return out


def _select_copper(layer: GerberLayer | GeometryLayer, s: dict[str, Any], raw: dict[str, Any],
                   warnings: list[str]) -> tuple[np.ndarray, BaseGeometry | None]:
    """Retorna (polígonos de cobre selecionados, área de recorte opcional)."""
    geom = layer.copper if isinstance(layer, GerberLayer) else layer.geometry
    parts = polygons_of(geom)
    scope = s["scope"]
    area: BaseGeometry | None = None
    if scope == "all" or not len(parts):
        return parts, None
    tree = shapely.STRtree(parts)
    if scope == "points":
        pts = raw.get("scope_points") or []
        if not len(pts):
            warnings.append("Nenhuma ilha foi clicada; isolando a placa inteira.")
            return parts, None
        p = shapely.points(np.asarray(pts, dtype=float).reshape(-1, 2))
        idx = tree.query(p, predicate="within")[1]
        return parts[np.unique(idx)], None
    if scope == "bbox":
        area = shapely.box(min(s["scope_xmin"], s["scope_xmax"]), min(s["scope_ymin"], s["scope_ymax"]),
                           max(s["scope_xmin"], s["scope_xmax"]), max(s["scope_ymin"], s["scope_ymax"]))
    else:
        area = area_geometry(raw, "scope_geometry")
        if area is None:
            warnings.append("Nenhuma região foi selecionada; isolando a placa inteira.")
            return parts, None
    idx = tree.query(area, predicate="intersects")
    return parts[np.unique(idx)], (area if s["clip_to_scope"] else None)


def _as_multi(parts: np.ndarray) -> BaseGeometry:
    """Junta polígonos DISJUNTOS sem custo de união (as partes de uma união já são disjuntas)."""
    if not len(parts):
        return shapely.MultiPolygon()
    return shapely.multipolygons(parts)


def _offset_rings(base: BaseGeometry, offsets: list[float], s: dict[str, Any], climb: bool,
                  prog: Progress, inward: bool = False) -> tuple[list[list[np.ndarray]], list[BaseGeometry]]:
    """Para cada deslocamento, retorna os anéis (polilinhas) e o polígono deslocado."""
    rings_per: list[list[np.ndarray]] = []
    polys: list[BaseGeometry] = []
    js = _JOIN[s["join_style"]]
    qs = int(s["arc_segments"])
    tol = float(s["simplify_tol"])
    for i, off in enumerate(offsets):
        prog(i / max(len(offsets), 1), f"Calculando passe {i + 1} de {len(offsets)}…")
        g = shapely.buffer(base, chord_safe(-off if inward else off, qs), quad_segs=qs, join_style=js)
        polys.append(g)
        gp = polygons_of(g)
        if tol > 0 and len(gp):
            # simplificação por parte (vetorizada); a versão sobre o MultiPolygon inteiro
            # verifica topologia entre partes e é ordens de grandeza mais lenta
            gp = shapely.simplify(gp, tol, preserve_topology=False)
            gp = gp[~shapely.is_empty(gp)]
        gp = orient_for_milling(gp, climb)
        rings_per.append(rings_to_paths(gp))
    return rings_per, polys


def _bridges(copper_parts: np.ndarray, closing: BaseGeometry, prog: Progress
             ) -> tuple[list[dict[str, float]], BaseGeometry]:
    """Encontra vãos onde ilhas distintas se fundem no fechamento morfológico.

    Retorna (lista de pontes {x, y, gap}, união das áreas não cortadas entre ilhas).
    """
    if not len(copper_parts) or closing.is_empty:
        return [], shapely.Polygon()
    cparts = polygons_of(closing)
    tree = shapely.STRtree(cparts)
    reps = shapely.point_on_surface(copper_parts)
    pi, ci = tree.query(reps, predicate="within")
    if not len(pi):
        return [], shapely.Polygon()
    counts = np.bincount(ci, minlength=len(cparts))
    merged = np.flatnonzero(counts >= 2)
    if not len(merged):
        return [], shapely.Polygon()
    prog.check()
    mask = np.isin(ci, merged)
    grp_parts = copper_parts[pi[mask]]
    grp_ids = ci[mask]
    order = np.argsort(grp_ids, kind="stable")
    grp_parts, grp_ids = grp_parts[order], grp_ids[order]
    uniq, inv = np.unique(grp_ids, return_inverse=True)
    groups = shapely.multipolygons(grp_parts, indices=inv)
    slivers = shapely.difference(cparts[uniq], groups)
    sl = polygons_of(slivers)
    if not len(sl):
        return [], shapely.Polygon()
    eps = 1e-4
    ctree = shapely.STRtree(copper_parts)
    si, cj = ctree.query(shapely.buffer(sl, eps, quad_segs=2), predicate="intersects")
    out: list[dict[str, float]] = []
    keep: list[int] = []
    order = np.argsort(si, kind="stable")
    si, cj = si[order], cj[order]
    splits = np.flatnonzero(np.diff(si)) + 1
    for s_idx, c_idx in zip(np.split(si, splits), np.split(cj, splits)):
        if len(s_idx) == 0:
            continue
        cu = np.unique(c_idx)
        if len(cu) < 2:
            continue
        k = int(s_idx[0])
        keep.append(k)
        # largura do vão: menor distância entre pares de ilhas tocadas (limitado a poucas)
        cu = cu[:6]
        a, b = np.triu_indices(len(cu), 1)
        gap = float(shapely.distance(copper_parts[cu[a]], copper_parts[cu[b]]).min())
        pt = shapely.point_on_surface(sl[k])
        out.append({"x": float(pt.x), "y": float(pt.y), "gap": gap})
    rest = shapely.multipolygons(sl[keep]) if keep else shapely.Polygon()
    return out, rest


def isolate(layer: GerberLayer | GeometryLayer, settings: dict[str, Any],
            progress=None, cancel=None) -> Job:
    """Gera o trabalho de isolamento de ``layer`` conforme ``settings`` (grupo ``ISOLATION``).

    Chaves extras (não exibidas como formulário) aceitas em ``settings``:
    ``scope_geometry`` (geometria shapely da região selecionada), ``scope_points``
    (lista de (x, y) clicados) e ``exclude_geometry`` (área onde não se deve cortar).

    ``job.settings`` recebe também ``warnings`` (lista de textos), ``bridges`` (lista
    de ``{"x","y","gap"}``), ``tool_diameter`` e ``isolation_width``.
    """
    prog = Progress(progress, cancel)
    raw = dict(settings)
    s = ISOLATION.validate(settings)
    warnings: list[str] = []
    params = cut_params_from(s)
    tool = tool_from(s)
    d = tool.diameter
    r = d / 2.0
    climb = s["milling"] == "climb"
    prog(0.0, "Preparando isolamento…")

    exclude = area_geometry(raw, "exclude_geometry")
    parts, clip_area = _select_copper(layer, s, raw, warnings)
    ops: list[PathOp] = []
    bridges: list[dict[str, float]] = []
    passes = int(s["passes"])
    step = d * (1.0 - float(s["overlap"]) / 100.0)
    width = d + (passes - 1) * step

    def finish(paths: list[np.ndarray]) -> list[np.ndarray]:
        if clip_area is not None or exclude is not None:
            paths = clip_paths(paths, keep=clip_area, remove=exclude, min_len=r * 0.5)
        return paths

    if s["follow"]:
        traces = getattr(layer, "traces", None) or []
        if not traces:
            warnings.append("A camada não possui linhas centrais de trilhas; nada para seguir.")
        paths = finish([np.asarray(t, dtype=float)[:, :2] for t in traces if len(t) >= 2])
        ops.append(PathOp("mill", tool, params, paths=paths, label="Seguir trilhas"))
    elif not len(parts):
        warnings.append("Nenhum cobre a isolar na seleção.")
    else:
        extra = float(s["extra_offset"])
        offsets = [r + extra + i * step for i in range(passes)]
        if s["iso_type"] == "int":
            holes = []
            nmax = int(shapely.get_num_interior_rings(parts).max()) if len(parts) else 0
            for i in range(nmax):
                ir = shapely.get_interior_ring(parts, i)
                ir = ir[~shapely.is_missing(ir)]
                holes.append(shapely.polygons(ir))
            hole_arr = np.concatenate(holes) if holes else np.empty(0, dtype=object)
            if not len(hole_arr):
                warnings.append("Nenhuma ilha possui abertura interna; isolamento interior vazio.")
            base = _as_multi(hole_arr)
            ring_sets, _ = _offset_rings(base, offsets, s, climb, prog.sub(0.05, 0.8), inward=True)
            pass0 = None
        else:
            if s["iso_type"] == "ext":
                filled = shapely.polygons(shapely.get_exterior_ring(parts))
                base = shapely.union_all(filled)
            else:
                base = _as_multi(parts)
            ring_sets, polys = _offset_rings(base, offsets, s, climb, prog.sub(0.05, 0.7))
            pass0 = polys[0]

        ring_sets = [finish(rs) for rs in ring_sets]
        if s["combine"]:
            allp = [p for rs in ring_sets for p in rs]
            ops.append(PathOp("mill", tool, params, paths=allp,
                              label=f"Isolamento ({passes} passe{'s' if passes > 1 else ''})"))
        else:
            for i, rs in enumerate(ring_sets):
                ops.append(PathOp("mill", tool, params, paths=rs, label=f"Isolamento — passe {i + 1}"))

        # ----------------------------------------------------------- pontes / resto
        want_rest = bool(s["rest_machining"]) and pass0 is not None
        if (s["check_bridges"] or want_rest) and pass0 is not None:
            prog(0.72, "Verificando vãos estreitos (curtos)…")
            base_parts = polygons_of(base)
            closing = shapely.buffer(pass0, -(r + extra), quad_segs=int(s["arc_segments"]))
            bridges, rest_area = _bridges(base_parts, closing, prog)
            if want_rest and bridges:
                rest_dias = sorted({x for x in _parse_dias(s["rest_tool_dias"]) if x < d - 1e-9},
                                   reverse=True)
                if not rest_dias:
                    warnings.append("Usinagem de resto ativa, mas nenhuma ferramenta menor que a "
                                    "principal foi informada.")
                for k, dia in enumerate(rest_dias):
                    prog(0.8 + 0.1 * k / len(rest_dias), f"Usinagem de resto com Ø{dia:g} mm…")
                    rr = dia / 2.0
                    g = shapely.buffer(base, chord_safe(rr + extra, int(s["arc_segments"])), quad_segs=int(s["arc_segments"]),
                                       join_style=_JOIN[s["join_style"]])
                    g2 = orient_for_milling(g, climb)
                    rings = rings_to_paths(g2)
                    zone = shapely.buffer(rest_area, rr * 1.5, quad_segs=4)
                    rp = clip_paths(rings, keep=zone, remove=exclude, min_len=rr)
                    rt = Tool(diameter=dia, name=f"Ferramenta de resto Ø{dia:g} mm",
                              number=tool.number + k + 1, shape="flat")
                    if rp:
                        ops.append(PathOp("mill", rt, params, paths=rp, label=f"Resto Ø{dia:g} mm"))
                    closing = shapely.buffer(g, -(rr + extra), quad_segs=int(s["arc_segments"]))
                    bridges, rest_area = _bridges(base_parts, closing, prog)
                    if not bridges:
                        break
            for b in bridges[:50]:
                warnings.append(
                    f"Possível curto em X={b['x']:.3f} Y={b['y']:.3f} mm: vão de {b['gap']:.3f} mm "
                    f"é menor que a ferramenta ({d:.3f} mm).")
            if len(bridges) > 50:
                warnings.append(f"… e mais {len(bridges) - 50} possíveis curtos.")

    if s["optimize"]:
        prog(0.9, "Otimizando a ordem dos cortes…")
        cur = start_point(s)
        for op in ops:
            if op.paths:
                op.paths = optimize_paths(op.paths, cur, allow_reverse=True,
                                          time_limit=float(s["optimize_time"]))
                cur = tuple(op.paths[-1][-1])
    ops = [op for op in ops if op.paths]
    prog(1.0, "Isolamento concluído.")
    out_settings = dict(s)
    out_settings.update(warnings=warnings, bridges=bridges, tool_diameter=d, isolation_width=width)
    return Job(name=f"{layer.name} — isolamento", source_id=layer.id, ops=ops,
               operation="isolation", settings=out_settings)
