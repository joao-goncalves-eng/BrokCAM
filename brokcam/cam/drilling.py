"""Furação (Excellon → operações de furação e fresagem de furos/slots).

Gera uma operação por broca (ordenada por diâmetro), com os furos já ordenados para
minimizar o deslocamento. Opções:

* seleção das ferramentas do arquivo a usar;
* mapeamento de diâmetros: exato, agrupar diâmetros próximos (tolerância),
  associar à broca disponível mais adequada de uma lista ou ESCOLHER À MÃO a broca de
  cada diâmetro (``mapping='manual'`` + ``bit_map``, ex.: 0,79 / 0,81 / 0,85 → broca 0,8),
  tudo num só programa com uma pausa de troca por broca;
* fresar furos maiores que a broca (círculo com fresa de topo, multiprofundidade);
* slots (furos oblongos) fresados, furados em sequência ou ignorados;
* furação em bicadas (peck, ``CutParams.multi_depth`` + ``depth_per_pass``);
* compensação da ponta da broca (118°) somada à profundidade.
* um job por broca (``split_jobs='per_bit'``, :func:`drill_split`), cada um com
  ``bit_filter`` para poder ser regenerado de forma idêntica.
"""
from __future__ import annotations

import math
import re
from dataclasses import dataclass, field, replace
from typing import Any

import numpy as np
import shapely

from brokcam.cam.common import Progress, cut_params_from, cut_params_specs, start_point
from brokcam.cam.optimize import optimize_paths, optimize_points
from brokcam.cam.tools import drill_tip_length
from brokcam.core.model import DrillLayer, Job, PathOp, Tool
from brokcam.core.params import ParamGroup, ParamSpec, register

DRILLING = register(ParamGroup(
    key="drilling",
    title="Furação",
    description=(
        "Gera os furos do arquivo Excellon: uma operação por broca, do menor para o "
        "maior diâmetro (as brocas finas quebram menos quando a placa ainda está "
        "inteira), com pausa para troca de ferramenta entre elas. Furos maiores que as "
        "brocas disponíveis podem ser fresados com uma fresa de topo, e diâmetros muito "
        "parecidos podem ser agrupados numa só broca para reduzir trocas. Também é "
        "possível gerar um trabalho (e um G-code) separado para cada broca."),
    params=(
        ParamSpec(
            "split_jobs", "Programas gerados", "choice", "single",
            choices=(("single", "Um programa com pausas de troca"),
                     ("per_bit", "Um trabalho separado por broca")),
            help=(
                "Um programa: todas as brocas num só G-code, com pausa (M0) para trocar a "
                "broca e refazer o zero Z entre elas. Um trabalho por broca: cria um item "
                "na árvore para cada broca (depois da associação de diâmetros), cada um com "
                "seu próprio G-code sem pausas — mais um item para os furos fresados, se "
                "houver. Use quando quiser zerar o Z com calma a cada broca, furar só um "
                "diâmetro agora e os outros depois, ou refazer apenas os furos de um "
                "diâmetro que saíram errados. Ctrl+Z desfaz todos os trabalhos de uma vez.")),
        ParamSpec(
            "tool_select", "Ferramentas a usar", "str", "",
            help=(
                "Números das ferramentas do arquivo Excellon a furar, separados por "
                "vírgula (ex.: '1, 3' ou 'T1, T3'). Vazio = todas. Útil para furar "
                "primeiro só os furos de fixação, ou deixar os furos grandes para "
                "depois. A tabela de diâmetros da camada tem o botão 'Gerar só este "
                "diâmetro', que faz isso sem digitar.")),
        ParamSpec(
            "bit_filter", "Somente a broca de", "float", 0.0, unit="mm", minimum=-1.0,
            maximum=100.0, step=0.1, decimals=3, advanced=True,
            help=(
                "Gera apenas os furos que, DEPOIS da associação de diâmetros, caem na "
                "broca deste diâmetro (ex.: 0,8). 0 = todas as brocas; -1 = somente os "
                "furos fresados com a fresa de topo. É preenchido automaticamente em cada "
                "trabalho criado por 'Um trabalho separado por broca', para que o trabalho "
                "seja regenerado igual ao reabrir o projeto; normalmente você não precisa "
                "mexer.")),
        ParamSpec(
            "tool_order", "Ordem das brocas", "choice", "asc",
            choices=(("asc", "Menor → maior"), ("desc", "Maior → menor"), ("file", "Ordem do arquivo")),
            help=(
                "Sequência das brocas. Do menor para o maior é o recomendado: as brocas "
                "finas trabalham com a placa mais rígida e os furos grandes, feitos por "
                "último, não enfraquecem a região dos pequenos.")),
        ParamSpec(
            "mapping", "Associação de diâmetros", "choice", "exact",
            choices=(("exact", "Exata (uma broca por diâmetro)"),
                     ("group", "Agrupar diâmetros próximos"),
                     ("library", "Brocas disponíveis"),
                     ("manual", "Escolher a broca de cada diâmetro")),
            help=(
                "Exata: cada diâmetro do arquivo vira uma broca (podem ser muitas trocas, "
                "ex.: 0,79 / 0,8 / 0,81 mm). Agrupar: diâmetros que diferem menos que a "
                "tolerância usam a mesma broca (a maior do grupo, para o furo não sair "
                "apertado). Brocas disponíveis: cada furo é associado à broca mais "
                "adequada da sua lista de brocas reais — o modo mais prático para quem tem "
                "um jogo limitado de brocas. Escolher a broca de cada diâmetro: você decide, na "
                "tabela 'Diâmetros de furo' (selecione várias linhas e use 'Atribuir broca'), "
                "qual broca faz cada diâmetro — ex.: 0,79, 0,81 e 0,85 com a broca de 0,8 e "
                "1,0 e 1,1 com a de 1,0. Tudo sai num só G-code, com uma troca por broca.")),
        ParamSpec(
            "group_tol", "Tolerância de agrupamento", "float", 0.05, unit="mm", minimum=0.0,
            maximum=2.0, step=0.01, decimals=3,
            help=(
                "Diferença máxima de diâmetro para dois furos usarem a mesma broca no modo "
                "'Agrupar'. 0,05–0,1 mm é imperceptível para componentes PTH comuns.")),
        ParamSpec(
            "available_drills", "Brocas disponíveis (mm)", "str",
            "0.6, 0.7, 0.8, 0.9, 1.0, 1.2, 1.5, 2.0, 3.0",
            help=(
                "Lista das brocas que você possui, separadas por vírgula. Usada no modo "
                "'Brocas disponíveis'.")),
        ParamSpec(
            "bit_map", "Broca de cada diâmetro", "str", "",
            help=(
                "Associação escolhida à mão (modo 'Escolher a broca de cada diâmetro'), no "
                "formato 'furo=broca' separado por ponto e vírgula, em mm: ex. "
                "'0,79=0,8; 0,81=0,8; 1,02=1,0; 3,2=fresa'. 'fresa' = fresar esse furo com a "
                "fresa de topo. Normalmente é preenchido pelo botão 'Atribuir broca' da tabela "
                "de diâmetros — não precisa digitar. Diâmetros que não aparecem aqui usam a "
                "broca da lista 'Brocas disponíveis' escolhida pela regra abaixo.")),
        ParamSpec(
            "library_rule", "Regra de escolha da broca", "choice", "nearest",
            choices=(("nearest", "Mais próxima"), ("larger", "Igual ou maior"),
                     ("smaller", "Igual ou menor")),
            help=(
                "Como escolher a broca da lista para cada furo. Mais próxima: menor "
                "diferença absoluta. Igual ou maior: o furo nunca fica apertado (bom para "
                "terminais de componentes). Igual ou menor: o furo nunca fica folgado "
                "(bom quando os furos grandes serão fresados: a broca menor faz o centro "
                "e a fresa alarga).")),
        ParamSpec(
            "slot_strategy", "Slots (furos oblongos)", "choice", "mill",
            choices=(("mill", "Fresar com a broca"), ("drill", "Furos em sequência"),
                     ("skip", "Ignorar")),
            help=(
                "Fresar: a broca desce numa ponta do slot e se desloca até a outra (como "
                "uma fresa) — funciona, mas brocas não são feitas para corte lateral; use "
                "avanço baixo. Furos em sequência: faz uma fileira de furos sobrepostos "
                "(mais seguro para brocas finas, bordas onduladas). Ignorar: não gera os "
                "slots.")),
        ParamSpec(
            "slot_overlap", "Sobreposição dos furos do slot", "float", 50.0, unit="%",
            minimum=0.0, maximum=95.0, step=5.0, decimals=1, advanced=True,
            help=(
                "No modo 'Furos em sequência', quanto cada furo se sobrepõe ao anterior "
                "(em % do diâmetro). 50% deixa bordas razoavelmente lisas; maior = mais "
                "furos e bordas mais lisas.")),
        ParamSpec(
            "mill_large", "Fresar furos grandes", "bool", False,
            help=(
                "Furos grandes (acima do diâmetro mínimo abaixo) ou sem broca adequada na "
                "lista são fresados em círculo com uma fresa de topo, em vez de furados. "
                "Evita comprar brocas grandes e trocas extras; a fresa desce em várias "
                "passadas até atravessar a placa.")),
        ParamSpec(
            "mill_tool_dia", "Fresa para furos", "float", 1.0, unit="mm", minimum=0.1, maximum=10.0,
            step=0.1, decimals=2,
            help=(
                "Diâmetro da fresa de topo usada para fresar furos e slots grandes. Furos "
                "menores ou iguais a esse diâmetro não podem ser fresados (serão "
                "furados).")),
        ParamSpec(
            "mill_min_dia", "Fresar furos a partir de", "float", 3.0, unit="mm", minimum=0.0,
            maximum=100.0, step=0.1, decimals=2,
            help=(
                "Furos com diâmetro igual ou maior que este são sempre fresados (quando "
                "'Fresar furos grandes' está ativo). Típico: 2,5–3 mm, acima das brocas "
                "comuns de PCB.")),
        ParamSpec(
            "mill_threshold", "Diferença máx. broca/furo", "float", 0.2, unit="mm", minimum=0.0,
            maximum=10.0, step=0.05, decimals=3, advanced=True,
            help=(
                "No modo 'Brocas disponíveis', se a broca escolhida for menor que o furo "
                "por mais que este valor, o furo é fresado (quando 'Fresar furos grandes' "
                "está ativo); caso contrário apenas um aviso é emitido.")),
        ParamSpec(
            "mill_feed_xy", "Avanço XY da fresagem", "float", 150.0, unit="mm/min", minimum=1.0,
            maximum=10000.0, step=10.0, decimals=1,
            help="Avanço no plano XY ao fresar furos e slots com a fresa de topo (FR-4: 100–200 mm/min)."),
        ParamSpec(
            "mill_depth_per_pass", "Profundidade por passada (fresagem)", "float", 0.4, unit="mm",
            minimum=0.01, maximum=10.0, step=0.05, decimals=3,
            help=(
                "Quanto a fresa desce por volta ao fresar furos/slots. Metade do diâmetro "
                "da fresa é um limite seguro em FR-4.")),
        ParamSpec(
            "peck", "Furação em bicadas (peck)", "bool", False,
            help=(
                "A broca desce em etapas e sobe entre elas para expulsar o cavaco (como o "
                "ciclo G83). Recomendado para brocas finas (< 0,8 mm) e placas grossas ou "
                "de fibra que empastam; deixa a furação mais lenta. Em placas de 1,6 mm "
                "com brocas de metal duro normalmente é dispensável.")),
        ParamSpec(
            "peck_depth", "Profundidade da bicada", "float", 0.5, unit="mm", minimum=0.05,
            maximum=10.0, step=0.1, decimals=2,
            help="Quanto a broca avança em cada bicada antes de recuar (furação em bicadas)."),
        ParamSpec(
            "tip_compensation", "Compensar ponta da broca", "bool", False,
            help=(
                "Soma à profundidade o comprimento da ponta cônica da broca "
                "((d/2)/tan(ângulo/2) ≈ 0,3·d para 118°), garantindo que o diâmetro "
                "completo atravesse a placa. Útil para brocas grandes; com Z de corte "
                "já folgado (ex.: -2 mm para placa de 1,6 mm) não é necessário. Use "
                "placa de sacrifício por baixo.")),
        ParamSpec(
            "tip_angle", "Ângulo da ponta da broca", "float", 118.0, unit="°", minimum=60.0,
            maximum=180.0, step=1.0, decimals=1, advanced=True,
            help="Ângulo da ponta das brocas (118° é o padrão; algumas de metal duro têm 130°–140°)."),
        ParamSpec(
            "toolchange_z", "Z de troca de ferramenta", "float", 15.0, unit="mm", minimum=0.0,
            maximum=100.0, step=1.0, decimals=2,
            help=(
                "Altura para onde o eixo Z sobe na pausa de troca de broca (M0/M6). Deve "
                "dar espaço para tirar e colocar a broca com a mão: 15–30 mm em "
                "máquinas 3018. Depois da troca, refaça o zero Z (sonda ou papel).")),
        *cut_params_specs(
            defaults={"cut_z": -1.8, "travel_z": 2.0, "feed_z": 60.0, "feed_xy": 150.0},
            help_extra={"cut_z": "Na furação, atravesse a placa com folga (ex.: -1,8 a -2,0 mm para "
                                 "1,6 mm).",
                        "feed_xy": "Na furação só é usado ao fresar slots com a broca."},
            exclude=("multi_depth", "depth_per_pass", "laser_mode", "laser_power")),
        ParamSpec(
            "optimize", "Otimizar ordem dos furos", "bool", True,
            help=(
                "Ordena os furos de cada broca para minimizar o deslocamento (vizinho mais "
                "próximo + 2-opt). Em placas com centenas de furos economiza vários "
                "minutos.")),
        ParamSpec(
            "optimize_time", "Tempo máx. de otimização", "float", 0.5, unit="s", minimum=0.0,
            maximum=30.0, step=0.1, decimals=1, advanced=True,
            help="Tempo máximo gasto na melhoria 2-opt da ordem dos furos, por broca."),
        ParamSpec("start_x", "Início X", "float", 0.0, unit="mm", decimals=2, advanced=True,
                  help="Posição X da ferramenta no início do trabalho (normalmente a origem)."),
        ParamSpec("start_y", "Início Y", "float", 0.0, unit="mm", decimals=2, advanced=True,
                  help="Posição Y da ferramenta no início do trabalho (normalmente a origem)."),
    ),
))


@dataclass
class _Group:
    bit: float                                   # diâmetro da broca escolhida
    holes: list[tuple[float, np.ndarray]] = field(default_factory=list)   # (diâmetro, pontos)
    slots: list[tuple[float, np.ndarray]] = field(default_factory=list)   # (largura, slots)
    sources: set[int] = field(default_factory=set)


def _parse_numbers(text: str) -> list[float]:
    out: list[float] = []
    for tok in str(text).replace(";", ",").split(","):
        tok = tok.strip().upper().lstrip("T")
        try:
            out.append(float(tok))
        except ValueError:
            pass
    return out


def _choose_bit(dia: float, bits: np.ndarray, rule: str) -> float:
    if not len(bits):
        return dia
    if rule == "larger":
        ok = bits[bits >= dia - 1e-9]
        return float(ok.min()) if len(ok) else float(bits.max())
    if rule == "smaller":
        ok = bits[bits <= dia + 1e-9]
        return float(ok.max()) if len(ok) else float(bits.min())
    return float(bits[np.argmin(np.abs(bits - dia))])


MILL = "fresa"          # valor especial do ``bit_map``: fresar o furo com a fresa de topo
_MAP_TOL = 1e-3          # diâmetros do ``bit_map`` casam com os do arquivo dentro desta tolerância


def _num(tok: str) -> float:
    return float(tok.strip().replace(",", "."))


def parse_bit_map(text: str) -> dict[float, float | str]:
    """Lê o ``bit_map`` ('0,79=0,8; 1,02=1,0; 3,2=fresa') → ``{Ø furo: Ø broca | "fresa"}``.

    Aceita ``;`` ou quebra de linha entre pares, ``=`` ou ``:`` ou ``->`` entre furo e broca,
    vírgula ou ponto decimal. Pares inválidos são ignorados (o resto continua valendo).
    """
    out: dict[float, float | str] = {}
    for part in re.split(r"[;\n]+", str(text or "")):
        part = part.strip()
        if not part:
            continue
        m = re.match(r"^\s*([0-9.,]+)\s*(?:=|:|->|→)\s*(\S+)\s*$", part)
        if not m:
            continue
        try:
            hole = round(_num(m.group(1)), 4)
        except ValueError:
            continue
        val = m.group(2).strip().lower()
        if val in (MILL, "mill", "f"):
            out[hole] = MILL
            continue
        try:
            bit = _num(val)
        except ValueError:
            continue
        if bit > 0:
            out[hole] = round(bit, 4)
    return out


def format_bit_map(mapping: dict[float, float | str]) -> str:
    """Inverso de :func:`parse_bit_map` (ordem crescente de furo, vírgula decimal)."""
    parts = []
    for hole in sorted(mapping):
        v = mapping[hole]
        parts.append(f"{fmt_mm(hole)}={v if v == MILL else fmt_mm(float(v))}")
    return "; ".join(parts)


def _lookup(mapping: dict[float, float | str], dia: float) -> float | str | None:
    for hole, v in mapping.items():
        if abs(hole - dia) <= _MAP_TOL:
            return v
    return None


def _circle(cx: float, cy: float, radius: float, n: int, ccw: bool) -> np.ndarray:
    t = np.linspace(0.0, 2 * math.pi, n + 1)
    if not ccw:
        t = -t
    pts = np.column_stack([cx + radius * np.cos(t), cy + radius * np.sin(t)])
    pts[-1] = pts[0]
    return pts


def _slot_drill_points(slots: np.ndarray, dia: float, overlap: float) -> np.ndarray:
    pts: list[np.ndarray] = []
    pitch = max(dia * (1.0 - overlap / 100.0), dia * 0.05)
    for x0, y0, x1, y1 in slots:
        L = math.hypot(x1 - x0, y1 - y0)
        n = max(int(math.ceil(L / pitch)), 1)
        t = np.linspace(0.0, 1.0, n + 1)
        pts.append(np.column_stack([x0 + t * (x1 - x0), y0 + t * (y1 - y0)]))
    return np.vstack(pts) if pts else np.zeros((0, 2))


def fmt_mm(v: float) -> str:
    """Diâmetro com vírgula decimal (pt-BR), sem zeros à direita: 0.8 -> '0,8'."""
    return f"{float(v):g}".replace(".", ",")


def _tlist(numbers: set[int] | list[int]) -> str:
    return ", ".join(f"T{n}" for n in sorted(numbers))


@dataclass
class _Plan:
    tools: list[Any]
    order: list[_Group]
    milled_holes: list[tuple[float, np.ndarray]]
    milled_slots: list[tuple[float, np.ndarray]]
    milled_sources: set[int]
    warnings: list[str]


def _plan(layer: DrillLayer, s: dict[str, Any]) -> _Plan:
    """Seleção de ferramentas + associação de diâmetros → grupos por broca (sem caminhos)."""
    warnings: list[str] = []
    wanted = {int(v) for v in _parse_numbers(s["tool_select"])}
    all_tools = list(layer.tools.values())
    tools = [t for n, t in layer.tools.items() if not wanted or n in wanted]
    if wanted and not tools:
        warnings.append("Nenhuma das ferramentas selecionadas existe no arquivo.")
    if s["tool_order"] != "file":
        tools.sort(key=lambda t: (t.diameter, t.number))

    bits = np.array(sorted(_parse_numbers(s["available_drills"])), dtype=float)
    mill_large = bool(s["mill_large"])
    mill_dia = float(s["mill_tool_dia"])
    mill_min = float(s["mill_min_dia"])
    groups: dict[float, _Group] = {}
    milled_holes: list[tuple[float, np.ndarray]] = []
    milled_slots: list[tuple[float, np.ndarray]] = []
    milled_sources: set[int] = set()
    bit_of: dict[int, float] = {}
    auto_ids: set[int] = set()        # modo manual: diâmetros sem escolha (seguem a regra da lista)
    forced_mill: set[int] = set()     # modo manual: diâmetros marcados como "fresa"

    if s["mapping"] == "group":
        # agrupa considerando a camada INTEIRA (não só ``tool_select``): furar só o T1 dá a
        # mesma broca que ele teria no programa completo (sem surpresa em "Gerar só este diâmetro")
        tol = float(s["group_tol"])
        clusters: list[list[Any]] = []
        for t in sorted(all_tools, key=lambda t: t.diameter):
            if clusters and t.diameter - clusters[-1][0].diameter <= tol + 1e-12:
                clusters[-1].append(t)
            else:
                clusters.append([t])
        bit_of = {id(t): max(c.diameter for c in cl) for cl in clusters for t in cl}
    elif s["mapping"] == "library":
        if not len(bits):
            warnings.append("Lista de brocas disponíveis vazia; usando diâmetros exatos.")
        bit_of = {id(t): _choose_bit(t.diameter, bits, s["library_rule"]) for t in tools}
    elif s["mapping"] == "manual":
        # escolhido à mão por diâmetro; o que não foi escolhido segue a regra da lista
        manual = parse_bit_map(s["bit_map"])
        for t in tools:
            v = _lookup(manual, t.diameter)
            if v is None:
                bit_of[id(t)] = _choose_bit(t.diameter, bits, s["library_rule"])
                auto_ids.add(id(t))
            elif v == MILL:
                bit_of[id(t)] = round(t.diameter, 4)
                forced_mill.add(id(t))
            else:
                bit_of[id(t)] = float(v)
    else:
        bit_of = {id(t): round(t.diameter, 4) for t in tools}

    for t in tools:
        bit = bit_of[id(t)]
        pts = np.asarray(t.points, dtype=float).reshape(-1, 2)
        slots = np.asarray(t.slots, dtype=float).reshape(-1, 4)
        chosen = s["mapping"] == "manual" and id(t) not in auto_ids     # escolhido à mão: respeitar
        too_small_bit = ((s["mapping"] == "library" or (s["mapping"] == "manual" and not chosen))
                         and t.diameter - bit > float(s["mill_threshold"]))
        mill_it = id(t) in forced_mill or (mill_large and not chosen and (t.diameter >= mill_min or too_small_bit))
        if mill_it and t.diameter <= mill_dia + 1e-6:
            warnings.append(f"T{t.number} (Ø{t.diameter:g} mm) não pode ser fresado com fresa de "
                            f"{mill_dia:g} mm; será furado com broca de {bit:g} mm.")
            mill_it = False
        if mill_it:
            if len(pts):
                milled_holes.append((t.diameter, pts))
            if len(slots):
                milled_slots.append((t.diameter, slots))
            if len(pts) or len(slots):
                milled_sources.add(t.number)
            continue
        if too_small_bit:
            warnings.append(f"T{t.number}: furo de {t.diameter:g} mm será feito com broca de "
                            f"{bit:g} mm (diferença {t.diameter - bit:.2f} mm).")
        g = groups.setdefault(bit, _Group(bit))
        g.sources.add(t.number)
        if len(pts):
            g.holes.append((t.diameter, pts))
        if len(slots):
            g.slots.append((t.diameter, slots))

    order = list(groups.values())
    if s["tool_order"] == "asc":
        order.sort(key=lambda g: g.bit)
    elif s["tool_order"] == "desc":
        order.sort(key=lambda g: -g.bit)
    return _Plan(tools, order, milled_holes, milled_slots, milled_sources, warnings)


def _count(items: list[tuple[float, np.ndarray]], width: int) -> int:
    return int(sum(len(np.asarray(a).reshape(-1, width)) for _, a in items))


def _bit_matches(bit: float, bf: float) -> bool:
    return abs(float(bit) - float(bf)) < 1e-6


def drill(layer: DrillLayer, settings: dict[str, Any], progress=None, cancel=None) -> Job:
    """Gera o trabalho de furação de ``layer`` (grupo ``DRILLING``).

    Sempre devolve UM :class:`Job`. ``bit_filter`` (> 0) restringe aos furos associados à
    broca daquele diâmetro; ``-1`` restringe aos furos fresados. Para obter um job por
    broca use :func:`drill_split` (ou :func:`drill_auto`, usado pela interface).

    Convenções para o pós-processador:

    * ``PathOp(kind="drill")`` com ``params.multi_depth=True`` = furação em bicadas,
      ``params.depth_per_pass`` = profundidade de cada bicada;
    * fresagem de furos/slots usa ``PathOp(kind="mill")`` com multiprofundidade;
    * ``job.settings['toolchange_z']`` = altura de troca de ferramenta.

    A numeração das ferramentas (T1, T2…) segue a sequência das brocas do plano completo
    — também nos jobs filtrados — e o nome de cada ferramenta inclui os números originais
    do arquivo Excellon (aparecem na mensagem de troca do G-code).
    """
    prog = Progress(progress, cancel)
    s = DRILLING.validate(settings)
    base = cut_params_from(s)
    base = replace(base, multi_depth=bool(s["peck"]), depth_per_pass=float(s["peck_depth"]),
                   laser_mode=False)
    prog(0.0, "Preparando furação…")

    plan = _plan(layer, s)
    warnings = list(plan.warnings)
    tools = plan.tools
    mill_dia = float(s["mill_tool_dia"])
    bf = float(s["bit_filter"])
    filtered = abs(bf) > 1e-9
    want_milled = not filtered or bf < 0

    # ---------------------------------------------------------------- operações
    ops: list[PathOp] = []
    bits_info: list[dict[str, Any]] = []
    cur = start_point(s)
    tl = float(s["optimize_time"])
    number = 1
    sel = [g for g in plan.order if not filtered or (bf > 0 and _bit_matches(g.bit, bf))]
    # "rank" = posição da broca na lista CRESCENTE de brocas da camada inteira (sem
    # ``tool_select``): define a cor no canvas e na tabela, igual no job único, nos jobs
    # separados e — com associação exata — na própria camada (fresados = por último).
    full = plan if not str(s["tool_select"]).strip() else _plan(layer, {**s, "tool_select": ""})
    asc = sorted(float(g.bit) for g in full.order)

    def rank_of(bit: float) -> int:
        return next((i for i, b in enumerate(asc) if _bit_matches(b, bit)), 0)

    if filtered and bf > 0 and not sel:
        warnings.append(f"Nenhum furo usa a broca de {fmt_mm(bf)} mm com as opções atuais.")
    for gi, g in enumerate(plan.order):
        tool_number = number
        number += 1
        if g not in sel:
            continue
        prog(0.1 + 0.7 * gi / max(len(plan.order), 1), f"Ordenando furos da broca {g.bit:g} mm…")
        src = _tlist(g.sources)
        tool = Tool(diameter=g.bit, name=f"Broca {g.bit:g} mm (arquivo {src})", number=tool_number,
                    shape="drill")
        params = base
        if s["tip_compensation"]:
            params = replace(base, cut_z=base.cut_z - drill_tip_length(g.bit, float(s["tip_angle"])))
        pts_list = [p for _, p in g.holes]
        if s["slot_strategy"] == "drill":
            pts_list += [_slot_drill_points(sl, g.bit, float(s["slot_overlap"])) for _, sl in g.slots]
        pts = np.vstack(pts_list) if pts_list else np.zeros((0, 2))
        if len(pts):
            if s["optimize"]:
                pts = optimize_points(pts, cur, tl)
            # rótulo vai para o comentário "(Op Tn da maquina …: …)" do G-code: o T aqui é o do ARQUIVO
            ops.append(PathOp("drill", tool, params, points=pts,
                              label=f"Furos broca Ø{fmt_mm(g.bit)} mm - arquivo {src}"))
            cur = tuple(pts[-1])
        if s["slot_strategy"] == "mill" and g.slots:
            paths = [np.array([[x0, y0], [x1, y1]]) for _, sl in g.slots for x0, y0, x1, y1 in sl]
            if s["optimize"]:
                paths = optimize_paths(paths, cur, allow_reverse=True, time_limit=tl)
            sp = replace(params, multi_depth=True,
                         depth_per_pass=float(s["mill_depth_per_pass"]))
            ops.append(PathOp("mill", tool, sp, paths=paths, label=f"Slots broca Ø{fmt_mm(g.bit)} mm - arquivo {src}"))
            cur = tuple(paths[-1][-1])
        n_slots = 0 if s["slot_strategy"] == "skip" else _count(g.slots, 4)
        dias = sorted({round(float(d), 4) for d, _ in g.holes}
                      | ({round(float(d), 4) for d, _ in g.slots} if n_slots else set()))
        bits_info.append({"bit": float(g.bit), "sources": sorted(g.sources), "holes": _count(g.holes, 2),
                          "slots": n_slots, "milled": False, "rank": rank_of(g.bit), "tool": tool_number,
                          "dias": dias})
        prog.check()

    if want_milled and (plan.milled_holes or plan.milled_slots):
        prog(0.85, "Gerando fresagem dos furos grandes…")
        rm = mill_dia / 2.0
        src = _tlist(plan.milled_sources)
        mtool = Tool(diameter=mill_dia, name=f"Fresa de topo {mill_dia:g} mm (arquivo {src})", number=number,
                     shape="flat")
        mparams = replace(base, feed_xy=float(s["mill_feed_xy"]), multi_depth=True,
                          depth_per_pass=float(s["mill_depth_per_pass"]))
        paths: list[np.ndarray] = []
        for dia, pts in plan.milled_holes:
            R = dia / 2.0 - rm
            n = max(24, int(math.ceil(2 * math.pi * R / 0.1)))
            n = min(n, 360)
            # furo = anel interno: concordante (spindle horário) percorre no anti-horário
            paths.extend(_circle(x, y, R, n, ccw=True) for x, y in pts)
        for width, sl in plan.milled_slots:
            for x0, y0, x1, y1 in sl:
                ln = shapely.LineString([(x0, y0), (x1, y1)])
                poly = shapely.buffer(ln, width / 2.0 - rm, quad_segs=12)
                poly = shapely.orient_polygons(poly, exterior_cw=False)
                paths.append(np.asarray(poly.exterior.coords))
        if s["optimize"]:
            paths = optimize_paths(paths, cur, allow_reverse=False, time_limit=tl)
        ops.append(PathOp("mill", mtool, mparams, paths=paths,
                          label=f"Furos fresados com fresa Ø{fmt_mm(mill_dia)} mm - arquivo {src}"))
        bits_info.append({"bit": float(mill_dia), "sources": sorted(plan.milled_sources),
                          "holes": _count(plan.milled_holes, 2), "slots": _count(plan.milled_slots, 4),
                          "milled": True, "rank": len(asc), "tool": number,
                          "dias": sorted({round(float(d), 4) for d, _ in plan.milled_holes + plan.milled_slots})})
    elif filtered and bf < 0:
        warnings.append("Nenhum furo é fresado com as opções atuais.")

    total = sum(len(np.asarray(t.points).reshape(-1, 2)) for t in tools)
    n_holes = sum(b["holes"] for b in bits_info)
    if len(bits_info) == 1 and (filtered or str(s["tool_select"]).strip()):
        b = bits_info[0]
        name = _split_name(b)
    else:
        name = (f"{layer.name} — furação ({len(bits_info)} broca{'s' if len(bits_info) != 1 else ''} · "
                f"{n_holes} furo{'s' if n_holes != 1 else ''})")
    prog(1.0, "Furação concluída.")
    return Job(name=name, source_id=layer.id, ops=ops, operation="drilling",
               settings={**s, "warnings": warnings, "toolchange_z": float(s["toolchange_z"]),
                         "holes_in": total, "bits": bits_info})


def _split_name(b: dict[str, Any]) -> str:
    """Nome de um job de uma broca só. Se os furos não têm o diâmetro da broca (associação
    'Agrupar'/'Brocas disponíveis'), o diâmetro dos furos aparece explicitamente."""
    src = " ".join(f"T{n}" for n in b["sources"])
    extra = f" · {b['slots']} slot{'s' if b['slots'] != 1 else ''}" if b["slots"] else ""
    holes = f"{b['holes']} furo{'s' if b['holes'] != 1 else ''}"
    dias = [float(d) for d in b.get("dias") or []]
    if b.get("milled"):
        return f"Furos fresados com fresa Ø{fmt_mm(b['bit'])} mm ({src} · {holes}{extra})"
    if dias and any(not _bit_matches(d, b["bit"]) for d in dias):
        lst = " e ".join(fmt_mm(d) for d in dias)
        return f"Furação broca Ø{fmt_mm(b['bit'])} mm (furos Ø{lst} · {src} · {holes}{extra})"
    return f"Furação Ø{fmt_mm(b['bit'])} mm ({src} · {holes}{extra})"


def bit_assignment(layer: DrillLayer, settings: dict[str, Any]) -> dict[int, tuple[float, bool]]:
    """Broca que cada ferramenta do arquivo recebe com as opções atuais (camada inteira).

    Devolve ``{número T do arquivo: (Ø da broca, fresado?)}`` — fresado = feito com a fresa
    de topo (Ø = ``mill_tool_dia``). Usado pela interface para mostrar, ANTES de gerar, qual
    broca a "Associação de diâmetros" vai escolher para cada furo.
    """
    s = DRILLING.validate({**settings, "tool_select": ""})
    plan = _plan(layer, s)
    out: dict[int, tuple[float, bool]] = {}
    for g in plan.order:
        for n in g.sources:
            out[int(n)] = (float(g.bit), False)
    for n in plan.milled_sources:
        out[int(n)] = (float(s["mill_tool_dia"]), True)
    return out


def drill_split(layer: DrillLayer, settings: dict[str, Any], progress=None, cancel=None) -> list[Job]:
    """Um :class:`Job` por broca (depois da associação de diâmetros) + um para os furos fresados.

    Cada job guarda em ``settings['bit_filter']`` o diâmetro da sua broca (ou ``-1`` para os
    furos fresados), de modo que ``drill(layer, job.settings)`` o regenera igual ao reabrir
    o projeto. Cada job começa no ponto inicial (origem) e tem uma só ferramenta: o G-code
    dele não tem pausa de troca.
    """
    prog = Progress(progress, cancel)
    s = DRILLING.validate(settings)
    s["split_jobs"] = "per_bit"
    plan = _plan(layer, s)
    filters: list[float] = [float(g.bit) for g in plan.order
                            if g.holes or (g.slots and s["slot_strategy"] != "skip")]
    if plan.milled_holes or plan.milled_slots:
        filters.append(-1.0)
    jobs: list[Job] = []
    n = max(len(filters), 1)
    for i, bf in enumerate(filters):
        prog.check()

        def sub(frac: float, msg: str = "", i: int = i) -> None:
            prog((i + max(0.0, min(1.0, frac))) / n, msg)

        jobs.append(drill(layer, {**s, "bit_filter": bf}, progress=sub, cancel=cancel))
    prog(1.0, f"Furação: {len(jobs)} trabalho(s) separado(s).")
    return jobs


def drill_auto(layer: DrillLayer, settings: dict[str, Any], progress=None, cancel=None) -> Job | list[Job]:
    """Entrada usada pelo registro de operações.

    ``split_jobs == "per_bit"`` sem ``bit_filter`` → lista de jobs (:func:`drill_split`);
    caso contrário um único job (:func:`drill`) — inclusive ao regenerar, do ``.bcam``, um
    job separado (que já traz o seu ``bit_filter``).
    """
    s = DRILLING.validate(settings)
    if s["split_jobs"] == "per_bit" and abs(float(s["bit_filter"])) < 1e-9:
        return drill_split(layer, settings, progress=progress, cancel=cancel)
    return drill(layer, settings, progress=progress, cancel=cancel)
