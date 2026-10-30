"""Utilitários compartilhados pelas operações de CAM.

* :func:`cut_params_specs` / :func:`cut_params_from` — parâmetros físicos de corte
  (Z, avanços, RPM, multiprofundidade, dwell) declarados uma única vez, com ajuda.
* :func:`tool_specs` / :func:`tool_from` — escolha de ferramenta (V-bit ou topo reto).
* :func:`optimize_specs` — opções de ordenação de caminhos.
* :class:`Progress` — encapsula ``progress``/``cancel`` e levanta :class:`Cancelled`.
* Conversão vetorizada de geometrias shapely em polilinhas numpy.
"""
from __future__ import annotations

import math
from dataclasses import replace
from typing import Any, Callable, Iterable

import numpy as np
import shapely
from shapely.geometry.base import BaseGeometry

from brokcam.cam import Cancelled
from brokcam.cam.tools import VBIT_LOCK_NOTE, vbit_auto_values, vbit_effective_width
from brokcam.core.model import CutParams, Tool
from brokcam.core.params import ParamGroup, ParamSpec, set_derive, set_locked

ProgressFn = Callable[[float, str], None]
CancelFn = Callable[[], bool]


# =========================================================================== progresso

class Progress:
    """Ajudante de progresso/cancelamento.

    ``p(frac, msg)`` informa o progresso (se houver callback) e verifica o
    cancelamento. ``p.check()`` apenas verifica. ``p.sub(a, b)`` cria um
    sub-progresso que mapeia 0..1 para a..b.
    """

    def __init__(self, progress: ProgressFn | None = None, cancel: CancelFn | None = None,
                 lo: float = 0.0, hi: float = 1.0) -> None:
        self._progress = progress
        self._cancel = cancel
        self._lo = lo
        self._hi = hi

    def check(self) -> None:
        if self._cancel is not None and self._cancel():
            raise Cancelled("Operação cancelada pelo usuário.")

    def __call__(self, frac: float, msg: str = "") -> None:
        self.check()
        if self._progress is not None:
            f = self._lo + (self._hi - self._lo) * min(max(frac, 0.0), 1.0)
            self._progress(f, msg)

    def sub(self, a: float, b: float) -> "Progress":
        span = self._hi - self._lo
        return Progress(self._progress, self._cancel, self._lo + span * a, self._lo + span * b)


# =========================================================================== parâmetros

_CUT_SPECS: tuple[ParamSpec, ...] = (
    ParamSpec(
        "cut_z", "Z de corte", "float", -0.05, unit="mm", minimum=-10.0, maximum=0.0,
        step=0.01, decimals=3,
        help=(
            "Profundidade final do corte, medida a partir da superfície do cobre (Z=0). "
            "Deve ser negativa. O cobre de uma placa FR-4 comum tem 35 µm (1 oz), então "
            "para isolamento com fresa em V 30°/0,1 mm valores entre -0,05 e -0,10 mm são "
            "típicos: rasos demais deixam cobre sem cortar onde a placa não está "
            "perfeitamente plana; fundos demais alargam o sulco da V-bit (a largura "
            "efetiva cresce 2·tan(ângulo/2) por mm de profundidade) e podem apagar "
            "trilhas finas. Para recorte da placa use a espessura total mais uma folga "
            "(ex.: -1,7 mm para placa de 1,6 mm). Para furação, a profundidade deve "
            "atravessar a placa (ex.: -1,8 mm). Se a sua placa empena, use autolevel "
            "(mapa de altura) em vez de aprofundar o corte.")),
    ParamSpec(
        "travel_z", "Z de deslocamento", "float", 1.0, unit="mm", minimum=0.0, maximum=50.0,
        step=0.1, decimals=2,
        help=(
            "Altura segura em que a ferramenta se desloca entre um corte e outro (G0). "
            "Precisa ficar acima de grampos, fita e de qualquer irregularidade da placa. "
            "Valores típicos: 1 a 2 mm para PCB fixada com fita dupla-face; 3 a 5 mm se "
            "houver grampos. Valores maiores aumentam o tempo total, pois cada subida e "
            "descida é repetida centenas de vezes.")),
    ParamSpec(
        "feed_xy", "Avanço XY", "float", 120.0, unit="mm/min", minimum=1.0, maximum=20000.0,
        step=10.0, decimals=1,
        help=(
            "Velocidade de avanço no plano XY durante o corte (G1 F...). Para fresa em V "
            "0,1 mm em cobre, 100–200 mm/min em máquinas tipo 3018 é um bom ponto de "
            "partida; fresas de topo de 0,8–1,0 mm aceitam 150–300 mm/min; recorte de "
            "FR-4 com fresa de 2 mm, 100–200 mm/min. Avanço alto demais quebra pontas "
            "finas e deixa rebarbas; baixo demais esquenta a ferramenta e derrete a "
            "resina. Aumente aos poucos observando o acabamento.")),
    ParamSpec(
        "feed_z", "Avanço Z (mergulho)", "float", 60.0, unit="mm/min", minimum=1.0,
        maximum=5000.0, step=10.0, decimals=1,
        help=(
            "Velocidade de descida da ferramenta para dentro do material (mergulho). "
            "Deve ser menor que o avanço XY: pontas em V e brocas finas quebram com "
            "mergulhos rápidos. Típico: 30–60 mm/min para V-bit, 50–100 mm/min para "
            "brocas de 0,6–1,0 mm, 30–60 mm/min para recorte. Na furação este é o avanço "
            "que de fato perfura a placa.")),
    ParamSpec(
        "spindle_rpm", "Rotação do spindle", "float", 10000.0, unit="RPM", minimum=0.0,
        maximum=60000.0, step=500.0, decimals=0,
        help=(
            "Rotação do motor (valor S do G-code, com M3). Fresas e brocas finas trabalham "
            "melhor em rotação alta: 10.000–12.000 RPM nos motores 775/ER11 comuns. "
            "Rotação baixa com avanço alto quebra a ferramenta; rotação alta com avanço "
            "muito baixo esquenta e derrete a resina. No GRBL o valor é escalado por $30 "
            "(rotação máxima configurada); se o seu spindle não tem controle de "
            "velocidade, o valor só liga/desliga o motor.")),
    ParamSpec(
        "multi_depth", "Multiprofundidade", "bool", False,
        help=(
            "Quando ativado, o corte até o Z final é feito em várias passadas, descendo no "
            "máximo 'Profundidade por passada' em cada uma. Essencial para recorte da "
            "placa (1,6 mm de FR-4 não se corta de uma vez com fresa fina) e para "
            "fresar furos grandes. No isolamento normalmente fica desligado, pois o "
            "corte é raso. Na furação, ativar esta opção gera furação em bicadas (peck): "
            "a broca sobe para retirar cavaco a cada passada.")),
    ParamSpec(
        "depth_per_pass", "Profundidade por passada", "float", 0.1, unit="mm", minimum=0.01,
        maximum=10.0, step=0.05, decimals=3,
        help=(
            "Quanto a ferramenta desce a cada passada quando a multiprofundidade está "
            "ativa (valor positivo). Regra prática: até metade do diâmetro da fresa em "
            "FR-4 — 0,3–0,5 mm para fresa de 1 mm, 0,5–0,8 mm para fresa de 2 mm. "
            "Passadas menores são mais lentas porém mais seguras para a ferramenta e "
            "geram menos calor.")),
    ParamSpec(
        "dwell_s", "Pausa (dwell)", "float", 0.0, unit="s", minimum=0.0, maximum=60.0,
        step=0.5, decimals=1, advanced=True,
        help=(
            "Tempo de espera (G4 P...) após ligar o spindle, para que ele atinja a "
            "rotação antes de começar a cortar. Use 1–3 s em motores sem controle de "
            "velocidade ou que demoram a acelerar. 0 desativa a pausa.")),
    ParamSpec(
        "laser_mode", "Modo laser", "bool", False, advanced=True,
        help=(
            "Gera o programa para um laser em vez de um spindle (GRBL com $32=1): usa M4 "
            "(potência dinâmica), o valor S passa a ser a potência e não há movimentos em "
            "Z. Útil para queimar tinta/máscara em vez de fresar o cobre. Deixe desligado "
            "para fresadoras.")),
    ParamSpec(
        "laser_power", "Potência do laser (S)", "float", 1000.0, minimum=0.0, maximum=100000.0,
        step=10.0, decimals=0, advanced=True,
        help=(
            "Valor S enviado no modo laser, na escala configurada em $30 do GRBL "
            "(normalmente 0–1000). Só é usado quando o modo laser está ativo.")),
)

CUT_KEYS: tuple[str, ...] = tuple(s.key for s in _CUT_SPECS)


# Campos que só fazem sentido em certas combinações (a interface esconde os demais).
_CUT_CONDITIONS: dict[str, tuple[tuple[str, tuple[Any, ...]], ...]] = {
    "depth_per_pass": (("multi_depth", (True,)), ("laser_mode", (False,))),
    "multi_depth": (("laser_mode", (False,)),),
    "laser_power": (("laser_mode", (True,)),),
    "spindle_rpm": (("laser_mode", (False,)),),
    "cut_z": (("laser_mode", (False,)),),
    "feed_z": (("laser_mode", (False,)),),
    "travel_z": (("laser_mode", (False,)),),
}


def cut_params_specs(defaults: dict[str, Any] | None = None,
                     help_extra: dict[str, str] | None = None,
                     include_laser: bool = True,
                     exclude: tuple[str, ...] = ()) -> tuple[ParamSpec, ...]:
    """Especificações dos parâmetros de corte (chaves em :data:`CUT_KEYS`).

    ``defaults`` substitui valores padrão por operação (ex.: ``{"cut_z": -1.7}``),
    ``help_extra`` acrescenta um parágrafo específico da operação ao ``help`` e
    ``exclude`` omite chaves que a operação trata de outra forma.
    """
    defaults = defaults or {}
    help_extra = help_extra or {}
    out: list[ParamSpec] = []
    keys = {s.key for s in _CUT_SPECS if not ((not include_laser and s.key.startswith("laser"))
                                               or s.key in exclude)}
    for s in _CUT_SPECS:
        if (not include_laser and s.key.startswith("laser")) or s.key in exclude:
            continue
        changes: dict[str, Any] = {}
        if s.key in defaults:
            changes["default"] = defaults[s.key]
        if s.key in help_extra:
            changes["help"] = s.help + " " + help_extra[s.key]
        cond = _CUT_CONDITIONS.get(s.key, ())
        cond = tuple(c for c in cond if c[0] in keys)
        if cond:
            changes["visible_if"] = s.visible_if + cond
        out.append(replace(s, **changes) if changes else s)
    return tuple(out)


def cut_params_from(settings: dict[str, Any]) -> CutParams:
    """Monta um :class:`CutParams` a partir de ``settings`` (chaves ausentes → padrão)."""
    base = CutParams()
    kw: dict[str, Any] = {}
    for key in CUT_KEYS:
        if key in settings and settings[key] is not None:
            kw[key] = settings[key]
    p = replace(base, **kw)
    p.cut_z = -abs(float(p.cut_z))
    p.travel_z = abs(float(p.travel_z))
    p.depth_per_pass = max(abs(float(p.depth_per_pass)), 1e-3)
    return p


def tool_specs(prefix: str = "tool", defaults: dict[str, Any] | None = None,
               allow_vbit: bool = True, label: str = "Ferramenta") -> tuple[ParamSpec, ...]:
    """Especificações de escolha de ferramenta: forma, diâmetro, ângulo/ponta da V-bit, número.

    Chaves: ``{prefix}_shape``, ``{prefix}_dia``, ``vbit_angle``, ``vbit_tip``,
    ``{prefix}_number`` (as chaves de V-bit só existem quando ``allow_vbit``).
    """
    d = defaults or {}
    specs: list[ParamSpec] = []
    if allow_vbit:
        specs.append(ParamSpec(
            f"{prefix}_shape", f"Tipo de {label.lower()}", "choice", d.get(f"{prefix}_shape", "vbit"),
            choices=(("vbit", "Fresa em V (gravador)"), ("flat", "Fresa de topo reto")),
            help=(
                "Formato da ponta da ferramenta. Fresa em V (V-bit): ponta cônica muito fina "
                "e barata; a largura do corte depende da profundidade, então o diâmetro "
                "efetivo é CALCULADO a partir do ângulo, da ponta e do Z de corte "
                "(largura = ponta + 2·|Z|·tan(ângulo/2)). Fresa de topo reto (end mill): a "
                "largura do corte é o próprio diâmetro, independente da profundidade — "
                "mais previsível em placas empenadas, porém mais cara e frágil nos "
                "diâmetros pequenos.")))
    specs.append(ParamSpec(
        f"{prefix}_dia", "Diâmetro da ferramenta", "float", d.get(f"{prefix}_dia", 0.2), unit="mm",
        minimum=0.01, maximum=20.0, step=0.05, decimals=3,
        visible_if=((f"{prefix}_shape", ("flat",)),) if allow_vbit else (),
        help=(
            "Diâmetro de corte da fresa de topo reto, em mm. Para fresas em V este valor é "
            "ignorado e substituído pela largura efetiva calculada. Medir a largura real "
            "do sulco com uma lupa após um corte de teste e ajustar aqui é a melhor forma "
            "de calibrar: se o sulco sai mais largo que o informado, as trilhas ficarão "
            "mais finas que o desenho.")))
    if allow_vbit:
        specs.append(ParamSpec(
            "vbit_angle", "Ângulo da V-bit", "float", d.get("vbit_angle", 30.0), unit="°",
            minimum=1.0, maximum=170.0, step=5.0, decimals=1,
            visible_if=((f"{prefix}_shape", ("vbit",)),),
            help=(
                "Ângulo total da ponta cônica da fresa em V (gravado no corpo ou na "
                "embalagem: 10°, 20°, 30°, 45°, 60°). Ângulos menores mudam menos de "
                "largura com variações de altura da placa, mas quebram com mais "
                "facilidade. 30° é o mais comum para PCB caseira.")))
        specs.append(ParamSpec(
            "vbit_tip", "Ponta da V-bit", "float", d.get("vbit_tip", 0.1), unit="mm",
            minimum=0.0, maximum=5.0, step=0.01, decimals=3,
            visible_if=((f"{prefix}_shape", ("vbit",)),),
            help=(
                "Diâmetro da ponta plana da fresa em V (largura mínima do sulco). Valores "
                "comuns: 0,1 mm ou 0,2 mm. É a largura do corte quando a fresa apenas "
                "encosta no cobre; com o Z de corte o sulco fica mais largo.")))
        specs.append(ParamSpec(
            "vbit_width", "Largura do sulco (V)", "float", d.get("vbit_width", 0.15), unit="mm",
            minimum=0.01, maximum=5.0, step=0.01, decimals=3,
            visible_if=((f"{prefix}_shape", ("vbit",)),),
            help=(
                "Largura de corte desejada para a fresa em V. É o valor que você controla: o "
                "Z de corte é calculado automaticamente a partir dela (e fica travado), pela "
                "fórmula profundidade = (largura − ponta) / (2·tan(ângulo/2)). Típico para "
                "isolamento com V 30°/0,1 mm: 0,13–0,20 mm (Z ≈ -0,05 a -0,19 mm). Larguras "
                "maiores isolam melhor, mas cortam mais fundo e podem apagar trilhas finas.")))
    specs.append(ParamSpec(
        f"{prefix}_number", "Número da ferramenta (T)", "int", d.get(f"{prefix}_number", 1),
        minimum=1, maximum=99, step=1, decimals=0, advanced=True,
        help=(
            "Número usado no comando de troca de ferramenta (M6 Tn). O GRBL não troca "
            "ferramentas sozinho: o programa pausa para a troca manual. Só importa se "
            "você combinar vários trabalhos em um único arquivo.")))
    return tuple(specs)


def enable_vbit_auto_z(group: ParamGroup, prefix: str = "tool") -> ParamGroup:
    """Com fresa em V, ``cut_z`` passa a ser calculado (e travado) a partir de ``vbit_width``."""
    keys = {p.key for p in group.params}
    if "cut_z" not in keys or f"{prefix}_shape" not in keys:
        return group
    cond: tuple = ((f"{prefix}_shape", ("vbit",)),)
    if "laser_mode" in keys:
        cond += (("laser_mode", (False,)),)
    set_locked(group, {"cut_z": cond}, VBIT_LOCK_NOTE)
    return set_derive(group, lambda v, raw: vbit_auto_values(v, raw, f"{prefix}_shape"))


def tool_from(settings: dict[str, Any], prefix: str = "tool", name: str = "") -> Tool:
    """Cria a :class:`Tool` descrita por ``settings``; V-bit tem diâmetro efetivo calculado."""
    shape = settings.get(f"{prefix}_shape", "flat")
    if settings.get("laser_mode"):
        shape = "flat"          # laser: largura do traço = diâmetro informado, sem geometria de V
    number = int(settings.get(f"{prefix}_number", 1))
    if shape == "vbit":
        angle = float(settings.get("vbit_angle", 30.0))
        tip = float(settings.get("vbit_tip", 0.1))
        dia = vbit_effective_width(angle, tip, float(settings.get("cut_z", -0.05)))
        return Tool(diameter=dia, name=name or f"Fresa V {angle:g}° ponta {tip:g} mm",
                    number=number, shape="vbit", vbit_angle=angle, vbit_tip=tip)
    dia = float(settings.get(f"{prefix}_dia", 1.0))
    return Tool(diameter=dia, name=name or f"Fresa de topo {dia:g} mm", number=number, shape="flat")


def optimize_specs(default_time: float = 0.5) -> tuple[ParamSpec, ...]:
    """Opções de otimização de percurso (chaves ``optimize``, ``optimize_time``, ``start_x/y``)."""
    return (
        ParamSpec(
            "optimize", "Otimizar ordem dos cortes", "bool", True,
            help=(
                "Reordena os caminhos (e escolhe o ponto de entrada de cada contorno "
                "fechado) para minimizar o deslocamento em vazio entre cortes, usando "
                "vizinho-mais-próximo seguido de melhoria 2-opt. Normalmente reduz o "
                "tempo de deslocamento em 50–90% em relação à ordem do arquivo. Desligue "
                "apenas para depurar ou se precisar manter a ordem original.")),
        ParamSpec(
            "optimize_time", "Tempo máx. de otimização", "float", default_time, unit="s",
            minimum=0.0, maximum=30.0, step=0.1, decimals=1, advanced=True,
            help=(
                "Tempo máximo gasto na etapa de melhoria (2-opt) da ordenação. Valores "
                "maiores podem encurtar um pouco mais o percurso em placas grandes; 0 "
                "usa apenas o vizinho-mais-próximo.")),
        ParamSpec(
            "start_x", "Início X", "float", 0.0, unit="mm", step=1.0, decimals=2, advanced=True,
            help="Posição X onde a ferramenta está ao começar o trabalho (normalmente a origem, 0). "
                 "A ordenação começa pelo corte mais próximo deste ponto."),
        ParamSpec(
            "start_y", "Início Y", "float", 0.0, unit="mm", step=1.0, decimals=2, advanced=True,
            help="Posição Y onde a ferramenta está ao começar o trabalho (normalmente a origem, 0)."),
    )


def resolution_specs() -> tuple[ParamSpec, ...]:
    """Opções de resolução geométrica (``arc_segments`` e ``simplify_tol``)."""
    return (
        ParamSpec(
            "arc_segments", "Segmentos por quarto de círculo", "int", 8, minimum=2, maximum=64,
            step=1, decimals=0, advanced=True,
            help=(
                "Quantos segmentos de reta aproximam cada quarto de circunferência nos "
                "contornos deslocados (offset). 8 é suficiente para PCB; valores maiores "
                "deixam curvas mais lisas mas geram G-code maior e mais lento de enviar "
                "pela serial.")),
        ParamSpec(
            "simplify_tol", "Tolerância de simplificação", "float", 0.003, unit="mm",
            minimum=0.0, maximum=0.5, step=0.001, decimals=4, advanced=True,
            help=(
                "Remove vértices redundantes dos caminhos desde que o desvio não passe "
                "deste valor. 0,003 mm (3 µm) é bem abaixo da precisão de máquinas "
                "hobby e reduz bastante o tamanho do G-code, evitando que o buffer do "
                "GRBL esvazie em curvas (o que causa paradas e marcas). 0 desativa.")),
    )


def chord_safe(offset: float, quad_segs: int) -> float:
    """Aumenta um deslocamento para que as CORDAS dos arcos poligonais fiquem à distância pedida.

    Um arco aproximado por segmentos tem os vértices sobre o círculo e as cordas
    para dentro (flecha r·(1 − cos(π/4n))). Dividindo por cos(π/4n) os vértices
    ficam um pouco para fora e nenhuma corda invade o cobre. O sinal é preservado.
    """
    return offset / math.cos(math.pi / (4 * max(int(quad_segs), 1)))


def start_point(settings: dict[str, Any]) -> tuple[float, float]:
    return (float(settings.get("start_x", 0.0)), float(settings.get("start_y", 0.0)))


# =========================================================================== geometria

_POLY_TYPES = (shapely.GeometryType.POLYGON,)
_LINE_TYPES = (shapely.GeometryType.LINESTRING, shapely.GeometryType.LINEARRING)


def parts_of(geom: BaseGeometry | Iterable[BaseGeometry] | None) -> np.ndarray:
    """Array de geometrias simples (sem Multi*/coleções), sem vazias."""
    if geom is None:
        return np.empty(0, dtype=object)
    arr = np.asarray(geom, dtype=object).ravel() if not isinstance(geom, BaseGeometry) else np.array([geom], dtype=object)
    parts = shapely.get_parts(arr)
    # coleções aninhadas (GeometryCollection dentro de coleção) são raras; repete até estabilizar
    while len(parts) and np.any(shapely.get_num_geometries(parts) > 1):
        parts = shapely.get_parts(parts)
    if len(parts):
        parts = parts[~shapely.is_empty(parts)]
    return parts


def polygons_of(geom: Any) -> np.ndarray:
    parts = parts_of(geom)
    return parts[shapely.get_type_id(parts) == shapely.GeometryType.POLYGON] if len(parts) else parts


def lines_of(geom: Any) -> np.ndarray:
    parts = parts_of(geom)
    if not len(parts):
        return parts
    t = shapely.get_type_id(parts)
    return parts[(t == shapely.GeometryType.LINESTRING) | (t == shapely.GeometryType.LINEARRING)]


def _split_coords(geoms: np.ndarray) -> list[np.ndarray]:
    if not len(geoms):
        return []
    coords, idx = shapely.get_coordinates(geoms, return_index=True)
    if not len(coords):
        return []
    cuts = np.flatnonzero(np.diff(idx)) + 1
    return [c for c in np.split(coords, cuts) if len(c) >= 2]


def rings_to_paths(geom: Any) -> list[np.ndarray]:
    """Todos os anéis (exteriores e interiores) dos polígonos como polilinhas fechadas."""
    polys = polygons_of(geom)
    if not len(polys):
        return []
    rings = shapely.get_rings(polys)
    return _split_coords(rings)


def lines_to_paths(geom: Any) -> list[np.ndarray]:
    """Linhas (LineString/LinearRing) como polilinhas abertas ou fechadas."""
    return _split_coords(lines_of(geom))


def geom_to_paths(geom: Any) -> list[np.ndarray]:
    """Polígonos → anéis fechados; linhas → polilinhas. Pontos são ignorados."""
    return rings_to_paths(geom) + lines_to_paths(geom)


def orient_for_milling(geom: BaseGeometry, climb: bool) -> BaseGeometry:
    """Orienta os anéis dos polígonos conforme o sentido de fresamento.

    Com spindle girando no sentido horário (M3), fresamento concordante (climb)
    corresponde a manter o material que fica (o cobre) à DIREITA da ferramenta:
    exteriores em sentido horário e furos (interiores) em anti-horário.
    O convencional é o oposto.
    """
    return shapely.orient_polygons(geom, exterior_cw=climb)


def is_closed(path: np.ndarray, tol: float = 1e-9) -> bool:
    return len(path) > 2 and float(np.abs(path[0] - path[-1]).max()) <= tol


def area_geometry(settings: dict[str, Any], key: str) -> BaseGeometry | None:
    """Lê uma geometria passada pela interface em ``settings[key]`` (shapely ou None)."""
    g = settings.get(key)
    if g is None:
        return None
    if isinstance(g, BaseGeometry):
        return None if g.is_empty else g
    if isinstance(g, (tuple, list)) and len(g) == 4 and all(isinstance(v, (int, float)) for v in g):
        return shapely.box(*g)
    raise TypeError(f"{key}: esperado geometria shapely ou (xmin, ymin, xmax, ymax)")


def clip_paths(paths: list[np.ndarray], keep: BaseGeometry | None = None,
               remove: BaseGeometry | None = None, min_len: float = 0.0) -> list[np.ndarray]:
    """Recorta polilinhas mantendo só o que está em ``keep`` e fora de ``remove``.

    Operação vetorizada (uma chamada GEOS por lote). Trechos menores que ``min_len``
    são descartados.
    """
    if not paths or (keep is None and remove is None):
        return paths
    lines = paths_to_lines(paths)
    if keep is not None:
        shapely.prepare(keep)
        inside = shapely.covers(keep, lines)
        lines = np.where(inside, lines, shapely.intersection(lines, keep))
    if remove is not None:
        shapely.prepare(remove)
        hit = shapely.intersects(remove, lines)
        if hit.any():
            lines = lines.copy()
            lines[hit] = shapely.difference(lines[hit], remove)
    merged = shapely.line_merge(lines)
    out = lines_to_paths(merged)
    if min_len > 0:
        out = [p for p in out if float(np.hypot(*np.diff(p, axis=0).T).sum()) >= min_len]
    return out


def paths_to_lines(paths: list[np.ndarray]) -> np.ndarray:
    """Converte polilinhas (N,2) em um array de LineString numa única chamada vetorizada."""
    paths = [np.asarray(p, dtype=float)[:, :2] for p in paths if len(p) >= 2]
    if not paths:
        return np.empty(0, dtype=object)
    lens = np.fromiter((len(p) for p in paths), dtype=np.int64, count=len(paths))
    idx = np.repeat(np.arange(len(paths)), lens)
    return shapely.linestrings(np.vstack(paths), indices=idx)


def path_length(path: np.ndarray) -> float:
    return float(np.hypot(*np.diff(path, axis=0).T).sum()) if len(path) > 1 else 0.0
