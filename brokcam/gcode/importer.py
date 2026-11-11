"""Importação de G-code → caminhos editáveis (``Job``) e geometria (``GeometryLayer``).

Permite abrir um programa já pronto (do próprio BrokCAM, do FlatCAM, do Candle ou de outro
CAM) e trabalhar com ele como se tivesse sido gerado aqui: ver no canvas, mover/girar junto
com a placa, editar caminhos à mão (apagar, inverter, reordenar), simular e gerar o G-code
de novo pelo pós-processador GRBL — com outras velocidades, profundidades ou trocas.

Como o G-code é reconstruído (heurística, sem Qt):

* o programa é interpretado por :func:`brokcam.gcode.preview.parse_gcode` (G0/G1/G2/G3,
  arcos, G20/G21, G90/G91); ciclos fixos G81/G82/G83/G73 são expandidos antes;
* o programa é dividido em BLOCOS: cada troca de ferramenta (``T``/``M6``/``M0`` após ``T``) e
  cada comentário ``(Op …)`` do BrokCAM abre um bloco novo (uma ferramenta por bloco);
* em cada bloco, movimentos de avanço com Z abaixo do limite (``cut_z_threshold``, padrão
  0 = superfície) viram polilinhas de corte; uma descida em Z seguida de subida sem andar
  em XY vira um FURO;
* passadas repetidas sobre o mesmo caminho (recorte em várias profundidades) viram UMA
  polilinha com multiprofundidade; caminhos em profundidades diferentes viram operações
  separadas com a mesma ferramenta (sem troca);
* diâmetro e formato da ferramenta vêm dos comentários quando existem (``(Op T1 da maquina
  vbit D0.15mm: …)`` do BrokCAM, ``Ø0,8``, ``Dia: 0.8``, ``Tool Dia = 0.8`` etc.); senão
  usam os padrões do grupo :data:`GCODE_IMPORT`;
* avanços, rotação e altura de deslocamento são lidos do próprio programa.

O texto original fica em ``job.gcode``: enviar o caminho importado sem editá-lo manda
exatamente o arquivo aberto. Editar os caminhos descarta esse texto (o G-code passa a ser
gerado pelo pós-processador).
"""
from __future__ import annotations

import os
import re
from dataclasses import dataclass, field
from typing import Any

import numpy as np
import shapely

from brokcam.core.model import CutParams, GeometryLayer, Job, PathOp, Tool
from brokcam.core.params import ParamGroup, ParamSpec, register

from .preview import parse_gcode

__all__ = ["GCODE_IMPORT", "ImportedGcode", "import_gcode", "import_gcode_file", "expand_canned_cycles"]

GCODE_IMPORT = register(ParamGroup(
    key="gcode_import",
    title="Importar G-code",
    description=(
        "Como um G-code aberto vira caminhos editáveis e geometria no projeto. O programa é "
        "lido movimento a movimento: o que corta abaixo da superfície vira caminho de "
        "fresagem, as descidas sem andar em XY viram furos e cada troca de ferramenta vira "
        "uma operação. Diâmetros e formatos das ferramentas são lidos dos comentários quando "
        "existem (G-code do BrokCAM e do FlatCAM trazem); senão valem os padrões abaixo."),
    params=(
        ParamSpec(
            "cut_z_threshold", "Corta abaixo de Z", "float", 0.0, unit="mm", minimum=-10.0,
            maximum=10.0, step=0.01, decimals=3,
            help=(
                "Movimentos de avanço (G1/G2/G3) com Z igual ou abaixo deste valor são "
                "considerados corte; acima, só deslocamento. 0 = superfície da placa, o padrão "
                "de quase todo CAM de PCB. Se o programa foi feito com o zero no fundo da placa "
                "(raro), use a espessura dela (ex.: 1,6). Programas de laser (sem Z) são "
                "tratados como corte em todo G1.")),
        ParamSpec(
            "default_tool_dia", "Ø padrão da fresa", "float", 0.2, unit="mm", minimum=0.01,
            maximum=20.0, step=0.05, decimals=3,
            help=(
                "Diâmetro usado para desenhar a largura do corte quando o G-code não informa a "
                "ferramenta em comentário. Só afeta a visualização e a geometria recriada — o "
                "caminho (centro da ferramenta) é sempre o do arquivo. 0,1–0,2 mm para "
                "isolamento com fresa em V; 1–2 mm para recorte.")),
        ParamSpec(
            "default_drill_dia", "Ø padrão da broca", "float", 0.8, unit="mm", minimum=0.05,
            maximum=20.0, step=0.1, decimals=3,
            help=(
                "Diâmetro dos furos quando o G-code não diz qual broca usa. Você pode corrigir "
                "depois, na tabela 'Ferramentas e cortes' do caminho importado. Furos de componente comuns: "
                "0,8–1,0 mm.")),
        ParamSpec(
            "merge_passes", "Juntar passadas repetidas", "bool", True,
            help=(
                "Quando o mesmo contorno é percorrido várias vezes descendo o Z (recorte em "
                "várias passadas), guarda UMA polilinha com multiprofundidade em vez de várias "
                "cópias sobrepostas. Facilita editar (apagar um contorno apaga todas as "
                "passadas) e o G-code regerado continua descendo em etapas.")),
        ParamSpec(
            "make_geometry", "Mostrar a geometria recriada", "bool", True,
            help=(
                "Junto com o caminho é sempre criada uma camada de Geometria com as linhas "
                "cortadas e os círculos dos furos (é ela que se move, gira e agrupa com a placa, "
                "levando o caminho junto, e pode servir de base para outras operações, como um "
                "recorte ao redor). Desligado, essa camada começa oculta (o olho na árvore do "
                "projeto mostra de novo).")),
        ParamSpec(
            "arc_tolerance", "Tolerância de arcos", "float", 0.01, unit="mm", minimum=0.001,
            maximum=0.5, step=0.005, decimals=3, advanced=True,
            help=(
                "Erro máximo ao transformar arcos G2/G3 em segmentos retos. 0,01 mm é invisível "
                "na placa; valores maiores deixam o arquivo regerado menor.")),
    ),
))

_COMMENT_RE = re.compile(r"\(([^)]*)\)|;(.*)$")
_BROKCAM_OP = re.compile(r"Op\s+T(\d+)\s+da\s+maquina\s+(\w+)\s+D\s*([\d.]+)\s*mm\s*:?\s*(.*)", re.I)
_DIA_PATTERNS = (
    re.compile(r"[ØøΦ⌀]\s*([\d]+(?:[.,]\d+)?)"),
    re.compile(r"\bdi(?:a|â)m?(?:eter|etro)?\.?\s*[:=]?\s*([\d]+(?:[.,]\d+)?)", re.I),
    re.compile(r"\btool\s*dia(?:meter)?\s*[:=]?\s*([\d]+(?:[.,]\d+)?)", re.I),
    re.compile(r"\bD\s*([\d]+(?:\.\d+)?)\s*mm\b"),
)
_SHAPE_WORDS = (("vbit", ("vbit", "v-bit", "fresa em v", "gravador", "engraver", " v ")),
                ("drill", ("broca", "drill", "furo", "furos")),
                ("ball", ("ball", "esférica", "esferica")),
                ("laser", ("laser",)))
_S_RE = re.compile(r"S\s*([-+]?\d+\.?\d*)", re.I)
_T_RE = re.compile(r"(?<![A-Z])T\s*(\d+)", re.I)
_M_RE = re.compile(r"(?<![A-Z])M\s*0*(\d+)", re.I)
_EPS = 1e-6


@dataclass
class ImportedGcode:
    """Resultado de :func:`import_gcode`."""
    job: Job
    geometry: GeometryLayer
    warnings: list[str] = field(default_factory=list)
    stats: dict[str, Any] = field(default_factory=dict)
    notes: list[str] = field(default_factory=list)      # informações (não são problemas)


# --------------------------------------------------------------------------- ciclos fixos

def expand_canned_cycles(text: str) -> tuple[str, bool]:
    """Expande G81/G82/G83/G73 (não suportados pelo GRBL) em G0/G1 equivalentes.

    Devolve ``(texto, houve_expansão)``. Suporta X/Y/Z/R/Q/P/F, G98/G99 e modo modal
    (linhas seguintes só com X/Y repetem o ciclo) até G80 ou outro G de movimento.
    """
    if not re.search(r"G\s*0*(81|82|83|73)\b", text, re.I):
        return text, False
    out: list[str] = []
    cyc: dict[str, float] | None = None
    retract_initial = True          # G98 (padrão)
    cur = {"X": 0.0, "Y": 0.0, "Z": 0.0}
    word = re.compile(r"([A-Z])\s*([-+]?(?:\d+\.?\d*|\.\d+))")
    for raw in text.splitlines():
        line = _COMMENT_RE.sub("", raw).upper()
        words = word.findall(line)
        gs = [float(v) for k, v in words if k == "G"]
        vals = {k: float(v) for k, v in words if k != "G"}
        if any(g in (98.0,) for g in gs):
            retract_initial = True
        if any(g in (99.0,) for g in gs):
            retract_initial = False
        new_cycle = next((g for g in gs if g in (81.0, 82.0, 83.0, 73.0)), None)
        if new_cycle is not None:
            cyc = {"G": new_cycle, "Z": vals.get("Z", cur["Z"]), "R": vals.get("R", cur["Z"]),
                   "Q": vals.get("Q", 0.0), "P": vals.get("P", 0.0), "F": vals.get("F", 0.0),
                   "init": cur["Z"]}
        elif any(g in (0.0, 1.0, 2.0, 3.0, 80.0) for g in gs):
            cyc = None
        if cyc is None or not ({"X", "Y"} & set(vals) or new_cycle is not None):
            out.append(raw)
            for k in ("X", "Y", "Z"):
                if k in vals:
                    cur[k] = vals[k]
            continue
        for k in ("Z", "R", "Q", "P", "F"):
            if k in vals and new_cycle is None:
                cyc[k] = vals[k]
        x = vals.get("X", cur["X"])
        y = vals.get("Y", cur["Y"])
        r, zb = cyc["R"], cyc["Z"]
        top = max(cyc["init"], r) if retract_initial else r
        f = f" F{cyc['F']:g}" if cyc["F"] else ""
        out.append(f"(ciclo G{cyc['G']:g} expandido) G0 X{x:.4f} Y{y:.4f}")
        out.append(f"G0 Z{r:.4f}")
        q = abs(cyc["Q"])
        if cyc["G"] in (83.0, 73.0) and q > 1e-6:
            depth = r
            while depth - q > zb + 1e-9:
                depth -= q
                out.append(f"G1 Z{depth:.4f}{f}")
                out.append(f"G0 Z{(r if cyc['G'] == 83.0 else depth + 0.2):.4f}")
                if cyc["G"] == 83.0:
                    out.append(f"G0 Z{depth + 0.2:.4f}")
        out.append(f"G1 Z{zb:.4f}{f}")
        if cyc["G"] == 82.0 and cyc["P"]:
            out.append(f"G4 P{cyc['P']:g}")
        out.append(f"G0 Z{top:.4f}")
        cur.update({"X": x, "Y": y, "Z": top})
    return "\n".join(out), True


# --------------------------------------------------------------------------- comentários

def _num(s: str) -> float:
    return float(s.replace(",", "."))


def _tool_from_comments(comments: list[str]) -> dict[str, Any]:
    """Número, diâmetro, formato e rótulo de ferramenta encontrados nos comentários."""
    info: dict[str, Any] = {}
    for c in comments:
        m = _BROKCAM_OP.search(c)
        if m:
            info.update(number=int(m.group(1)), shape=m.group(2).lower(), dia=float(m.group(3)),
                        label=m.group(4).strip())
            return info
    text = " ".join(comments)
    low = f" {text.lower()} "
    for pat in _DIA_PATTERNS:
        m = pat.search(text)
        if m:
            try:
                d = _num(m.group(1))
            except ValueError:
                continue
            if 0.01 <= d <= 50:
                info["dia"] = d
                break
    for shape, words in _SHAPE_WORDS:
        if any(w in low for w in words):
            info["shape"] = shape
            break
    return info


# --------------------------------------------------------------------------- blocos

@dataclass
class _Block:
    start_line: int
    tool_number: int
    comments: list[str] = field(default_factory=list)
    spindle: float = 0.0
    laser: bool = False


def _scan_blocks(lines: list[str], tool_change_lines: set[int]) -> list[_Block]:
    """Divide o programa em blocos (uma ferramenta cada) e coleta comentários e S de cada um."""
    blocks: list[_Block] = [_Block(0, 1)]
    tool = 1
    last_s = 0.0
    seen_motion = False
    for idx, raw in enumerate(lines):
        comments = [a or b for a, b in _COMMENT_RE.findall(raw)]
        code = _COMMENT_RE.sub("", raw)
        op_comment = any(_BROKCAM_OP.search(c) for c in comments)
        tm = _T_RE.search(code)
        if tm:
            tool = int(tm.group(1))
        if (idx in tool_change_lines or op_comment) and seen_motion:
            blocks.append(_Block(idx, tool))
            seen_motion = False
        elif idx in tool_change_lines or op_comment:
            blocks[-1].tool_number = tool
        b = blocks[-1]
        b.comments.extend(c.strip() for c in comments if c and c.strip())
        sm = _S_RE.search(code)
        if sm:
            last_s = float(sm.group(1))
        for mm in _M_RE.findall(code):
            if mm in ("3", "4"):
                b.spindle = b.spindle or last_s
                if mm == "4":
                    b.laser = True
        if re.search(r"[XY]\s*[-+.\d]", code.upper()):
            seen_motion = True
    for b in blocks:
        if not b.spindle:
            b.spindle = last_s
    return blocks


def _median(vals: list[float], default: float) -> float:
    v = [x for x in vals if x > 0]
    return float(np.median(v)) if v else default


# --------------------------------------------------------------------------- importação

def import_gcode(text: str, name: str = "G-code", settings: dict[str, Any] | None = None,
                 source_path: str = "") -> ImportedGcode:
    """Reconstrói caminhos (``Job``) e geometria a partir do programa ``text``."""
    s = GCODE_IMPORT.validate(settings or {})
    warnings: list[str] = []
    text_x, expanded = expand_canned_cycles(text)
    pv = parse_gcode(text_x, arc_tolerance=float(s["arc_tolerance"]))
    for w in pv.warnings:
        if "Ciclos fixos" not in w:
            warnings.append(w)
    lines = text_x.splitlines()
    seg = pv.segments
    rapid = pv.is_rapid
    seg_lines = pv.line_of_segment
    feeds = pv.segment_feed if pv.segment_feed is not None else np.zeros(len(seg))
    if not len(seg):
        raise ValueError("Nenhum movimento encontrado no G-code.")

    blocks = _scan_blocks(lines, {ln for ln, _t in pv.tool_changes})
    starts = np.array([b.start_line for b in blocks])
    block_of_seg = np.searchsorted(starts, seg_lines, side="right") - 1

    thr = float(s["cut_z_threshold"])
    has_z = bool(np.ptp(seg[:, :, 2]) > _EPS) if len(seg) else False
    ops: list[PathOp] = []
    all_paths: list[np.ndarray] = []
    all_holes: list[tuple[float, float, float]] = []      # x, y, raio
    n_merged = 0
    used_numbers: dict[tuple[int, float, str], int] = {}

    for bi, blk in enumerate(blocks):
        idx = np.flatnonzero(block_of_seg == bi)
        if not len(idx):
            continue
        info = _tool_from_comments(blk.comments)
        laser = blk.laser and not has_z

        paths: list[tuple[np.ndarray, float]] = []        # (pontos XY, Z mais fundo)
        holes: dict[tuple[float, float], list[float]] = {}  # ponto -> profundidades das descidas
        cut_feeds: list[float] = []
        plunge_feeds: list[float] = []
        rapid_z: list[float] = []
        cur: list[tuple[float, float]] | None = None
        cur_z = 0.0
        plunge: tuple[float, float, float] | None = None
        cut_since_plunge = False

        def flush() -> None:
            nonlocal cur
            if cur is not None and len(cur) > 1:
                paths.append((np.asarray(cur, dtype=float), cur_z))
            cur = None

        for k in idx:
            (x0, y0, z0), (x1, y1, z1) = seg[k]
            moved_xy = abs(x1 - x0) > _EPS or abs(y1 - y0) > _EPS
            below = (z0 <= thr + _EPS and z1 <= thr + _EPS) if has_z else not rapid[k]
            if rapid[k]:
                rapid_z.append(max(z0, z1))
            if not rapid[k] and moved_xy and below:
                if cur is None or abs(cur[-1][0] - x0) > _EPS or abs(cur[-1][1] - y0) > _EPS:
                    flush()
                    cur = [(x0, y0)]
                    cur_z = min(z0, z1)
                cur.append((x1, y1))
                cur_z = min(cur_z, z0, z1)
                cut_feeds.append(float(feeds[k]))
                cut_since_plunge = True
                continue
            if not moved_xy and has_z:
                if z1 < z0 - _EPS and z1 <= thr + _EPS:          # descida até cortar
                    flush()
                    if not rapid[k]:
                        plunge_feeds.append(float(feeds[k]))
                    plunge = (x1, y1, z1)
                    cut_since_plunge = False
                elif z1 > z0 + _EPS:                              # subida
                    if plunge is not None and not cut_since_plunge:
                        key = (round(plunge[0], 4), round(plunge[1], 4))
                        holes.setdefault(key, []).append(plunge[2])
                    flush()
                    plunge = None
                continue
            flush()
            if moved_xy:
                plunge = None
        flush()
        if plunge is not None and not cut_since_plunge:
            holes.setdefault((round(plunge[0], 4), round(plunge[1], 4)), []).append(plunge[2])

        feed_xy = _median(cut_feeds, 120.0)
        feed_z = _median(plunge_feeds, min(feed_xy, 60.0))
        above = [z for z in rapid_z if z > thr + _EPS]
        travel_z = float(np.median(above)) if above else 1.0
        rpm = float(blk.spindle or 10000.0)
        number = int(info.get("number", blk.tool_number) or 1)

        def make_tool(kind: str) -> Tool:
            shape = info.get("shape") or ("drill" if kind == "drill" else ("laser" if laser else "flat"))
            if kind == "drill" and shape not in ("drill",):
                shape = "drill" if "shape" not in info else shape
            if shape not in ("flat", "vbit", "ball", "drill", "laser"):
                shape = "flat"
            dia = float(info.get("dia") or (s["default_drill_dia"] if kind == "drill" else s["default_tool_dia"]))
            label = "Broca" if shape == "drill" else ("Fresa em V" if shape == "vbit" else
                                                       "Laser" if shape == "laser" else "Fresa")
            key = (number, round(dia, 4), shape)
            num = used_numbers.setdefault(key, number)
            return Tool(diameter=dia, name=f"{label} {dia:g} mm (importada)", number=num, shape=shape)

        # ---- furos
        if holes:
            tool = make_tool("drill")
            pts = np.array(list(holes.keys()), dtype=float)
            depths = [min(v) for v in holes.values()]
            n_peck = max(len({round(z, 4) for z in v}) for v in holes.values())
            peck_lv = sorted({round(z, 4) for z in max(holes.values(), key=len)}, reverse=True)
            peck_step = float(np.max(np.abs(np.diff([0.0] + peck_lv)))) if len(peck_lv) > 1 else 0.1
            zc = float(min(depths))
            if np.ptp(depths) > 0.05:
                warnings.append(f"Bloco {bi + 1}: furos com profundidades diferentes; usando a mais funda "
                                f"(Z {zc:g} mm).")
            params = CutParams(cut_z=zc, travel_z=travel_z, feed_xy=feed_xy, feed_z=feed_z, spindle_rpm=rpm,
                               multi_depth=n_peck > 1, depth_per_pass=round(peck_step, 4))
            ops.append(PathOp("drill", tool, params, points=pts,
                              label=info.get("label") or f"Furos importados Ø{tool.diameter:g} mm"))
            all_holes.extend((x, y, tool.diameter / 2) for x, y in pts.tolist())

        # ---- fresagem: junta passadas repetidas, agrupa por profundidade
        if paths:
            tool = make_tool("mill")
            groups: dict[tuple[float, int, float], list[np.ndarray]] = {}
            order: list[tuple[float, int, float]] = []
            if s["merge_passes"]:
                seen: dict[bytes, int] = {}
                uniq: list[list[Any]] = []          # [pontos, profundidades das passadas]
                for pts, z in paths:
                    r = np.round(pts, 3)
                    # passadas em zigue-zague percorrem o caminho ao contrário: mesma chave
                    key = min(r.tobytes(), r[::-1].tobytes())
                    if key in seen:
                        uniq[seen[key]][1].append(z)
                        n_merged += 1
                        continue
                    seen[key] = len(uniq)
                    uniq.append([pts, [z]])
                items = []
                for pts, zs in uniq:
                    lv = sorted({round(z, 4) for z in zs}, reverse=True)       # -0,4, -0,8 … (mais raso 1º)
                    steps = np.diff([max(0.0, thr if has_z else 0.0)] + lv) if len(lv) > 1 else []
                    dpp = float(np.max(np.abs(steps))) if len(steps) else 0.1
                    items.append((pts, min(zs), len(lv), round(dpp, 4)))
            else:
                items = [(p, z, 1, 0.1) for p, z in paths]
            for pts, z, n, dpp in items:
                gk = (round(z, 3), n, dpp)
                if gk not in groups:
                    groups[gk] = []
                    order.append(gk)
                groups[gk].append(pts)
                all_paths.append(pts)
            for zc, n, dpp in order:
                if laser:
                    params = CutParams(cut_z=0.0, travel_z=0.0, feed_xy=feed_xy, feed_z=feed_z, spindle_rpm=0.0,
                                       laser_mode=True, laser_power=rpm)
                else:
                    params = CutParams(cut_z=float(zc), travel_z=travel_z, feed_xy=feed_xy, feed_z=feed_z,
                                       spindle_rpm=rpm, multi_depth=n > 1, depth_per_pass=float(dpp))
                lbl = info.get("label") or f"Caminhos importados ({tool.name})"
                if len(order) > 1:
                    lbl += f" · Z {zc:g}"
                ops.append(PathOp("mill", tool, params, paths=groups[(zc, n, dpp)], label=lbl))

    if not ops:
        raise ValueError("O G-code não tem cortes abaixo de Z "
                         f"{thr:g} mm (ajuste 'Corta abaixo de Z' em Preferências › Importar G-code).")
    notes: list[str] = []
    if n_merged:
        notes.append(f"{n_merged} passada(s) repetida(s) foram juntadas em caminhos com multiprofundidade.")
    if expanded:
        notes.append("Ciclos fixos (G81/G83…) foram convertidos em movimentos simples.")

    base = os.path.basename(name) or "G-code"
    tc_z = [float(m) for m in re.findall(r"\(Troca de ferramenta[^)]*\)\s*\n\s*G0\s*Z\s*([-\d.]+)", text_x)]
    job = Job(name=f"{base} (importado)", source_id=-1, ops=ops, operation="gcode_import",
              settings={"edited": True, "imported": True, "source_file": source_path,
                        "warnings": warnings, "notes": notes, "toolchange_z": tc_z[0] if tc_z else 15.0})
    job.gcode = text

    # a camada sempre existe (fonte do caminho); ``make_geometry`` só define se aparece
    parts = [shapely.linestrings(p) for p in all_paths if len(p) > 1]
    if all_holes:
        h = np.asarray(all_holes)
        circles = shapely.buffer(shapely.points(h[:, :2]), h[:, 2], quad_segs=8)
        parts.extend(shapely.get_exterior_ring(circles).tolist())
    geom = shapely.multilinestrings(parts) if parts else shapely.MultiLineString()
    geom_layer = GeometryLayer(name=f"{base} — geometria", geometry=geom, source_path="")
    job.source_id = geom_layer.id

    stats = {"show_geometry": bool(s["make_geometry"]), "ops": len(ops), "paths": len(all_paths), "holes": len(all_holes),
             "tools": len({(o.tool.number, o.tool.diameter, o.tool.shape) for o in ops}),
             "lines": len(lines), "merged": n_merged}
    return ImportedGcode(job=job, geometry=geom_layer, warnings=warnings, stats=stats, notes=notes)


def import_gcode_file(path: str, settings: dict[str, Any] | None = None) -> ImportedGcode:
    """Lê ``path`` (UTF-8, tolerante) e chama :func:`import_gcode`."""
    with open(path, encoding="utf-8", errors="replace") as fh:
        text = fh.read()
    return import_gcode(text, os.path.basename(path), settings, source_path=os.path.abspath(path))
