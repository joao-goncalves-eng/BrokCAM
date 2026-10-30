"""Biblioteca de ferramentas padrão e cálculos de geometria de ferramenta.

A fresa em V (V-bit, "gravador") é a ferramenta mais usada para isolar PCBs caseiras:
a ponta é muito fina (0,1 mm típico) e a largura do sulco cresce com a profundidade.
Por isso a largura efetiva do corte depende do ângulo, da ponta e do Z de corte::

    largura = ponta + 2 · profundidade · tan(ângulo / 2)

Exemplo: V 30°, ponta 0,1 mm, Z = -0,05 mm → 0,1 + 2·0,05·tan(15°) ≈ 0,127 mm.
"""
from __future__ import annotations

import math
from dataclasses import dataclass

from brokcam.core.model import Tool


def vbit_effective_width(angle_deg: float, tip: float, depth: float) -> float:
    """Largura efetiva (mm) do sulco de uma fresa em V na profundidade ``depth``.

    ``depth`` pode ser informado positivo ou negativo (Z de corte); usa-se o módulo.
    ``angle_deg`` é o ângulo total da ponta (ex.: 30°), ``tip`` o diâmetro da ponta plana.
    """
    d = abs(float(depth))
    a = math.radians(float(angle_deg)) / 2.0
    return float(tip) + 2.0 * d * math.tan(a)


def vbit_depth_for_width(angle_deg: float, tip: float, width: float) -> float:
    """Profundidade (positiva, mm) necessária para a V-bit atingir a largura ``width``.

    Retorna 0 se ``width`` ≤ ponta (a ponta já é larga o suficiente).
    """
    a = math.radians(float(angle_deg)) / 2.0
    if width <= tip or a <= 0:
        return 0.0
    return (float(width) - float(tip)) / (2.0 * math.tan(a))


VBIT_MIN_DEPTH = 0.04
"""Profundidade mínima (mm) aplicada à fresa em V: um pouco além do cobre de 35 µm (1 oz)."""

VBIT_MAX_DEPTH = 10.0
"""Limite absoluto (mm) do Z calculado — o mesmo limite do campo Z de corte."""

VBIT_DEEP_WARNING = 0.3
"""Acima desta profundidade (mm) o formulário avisa: incomum para isolar cobre."""

VBIT_LOCK_NOTE = (
    "Com fresa em V o Z de corte é calculado automaticamente a partir da largura do sulco, "
    "do ângulo e da ponta: Z = −(largura − ponta) / (2·tan(ângulo/2)), nunca mais raso que "
    "0,04 mm (para atravessar o cobre de 35 µm). Para cortar mais fundo ou mais raso, mude a "
    "largura do sulco — assim o caminho gerado e a profundidade real sempre combinam.")


def vbit_auto_cut_z(angle_deg: float, tip: float, width: float) -> float:
    """Z de corte (negativo, mm) para a V-bit abrir um sulco de largura ``width``.

    Nunca é mais raso que :data:`VBIT_MIN_DEPTH`.
    """
    return -min(max(vbit_depth_for_width(angle_deg, tip, width), VBIT_MIN_DEPTH), VBIT_MAX_DEPTH)


def vbit_width_from_cut_z(angle_deg: float, tip: float, cut_z: float) -> float:
    """Largura do sulco (arredondada a 0,001 mm) equivalente a um Z de corte antigo."""
    depth = max(abs(float(cut_z)), VBIT_MIN_DEPTH)
    return round(vbit_effective_width(angle_deg, tip, depth), 3)


def vbit_auto_values(values: dict, raw: dict, shape_key: str = "tool_shape") -> dict:
    """Regra de derivação: com fresa em V, ``cut_z`` = Z automático da ``vbit_width``.

    Compatibilidade: configurações antigas (sem ``vbit_width``) guardavam só o Z de corte;
    nesse caso a largura equivalente é calculada a partir dele, preservando o corte.
    """
    if values.get(shape_key) != "vbit" or "cut_z" not in values or values.get("laser_mode"):
        return values
    out = dict(values)
    angle = float(out.get("vbit_angle", 30.0))
    tip = float(out.get("vbit_tip", 0.1))
    if raw.get("vbit_width") is None and raw.get("cut_z") is not None:
        out["vbit_width"] = vbit_width_from_cut_z(angle, tip, float(raw["cut_z"]))
    width = float(out.get("vbit_width") or 0.15)
    out["cut_z"] = round(vbit_auto_cut_z(angle, tip, width), 6)
    return out


def drill_tip_length(diameter: float, point_angle_deg: float = 118.0) -> float:
    """Comprimento (mm) da ponta cônica de uma broca: (d/2) / tan(ângulo/2).

    Para a broca padrão de 118° isso é ≈ 0,3·d. Somar esse valor à profundidade
    garante que o diâmetro total da broca atravesse a placa.
    """
    a = math.radians(point_angle_deg) / 2.0
    return (float(diameter) / 2.0) / math.tan(a)


@dataclass(frozen=True)
class ToolPreset:
    """Ferramenta da biblioteca padrão, com descrição para a interface."""
    key: str
    tool: Tool
    description: str
    category: str          # "vbit" | "endmill" | "drill"


def _vbit(angle: int, tip: float = 0.1) -> ToolPreset:
    return ToolPreset(
        key=f"vbit_{angle}_{tip:g}",
        tool=Tool(diameter=tip, name=f"Fresa V {angle}° ponta {tip:g} mm", shape="vbit",
                  vbit_angle=float(angle), vbit_tip=tip),
        description=(f"Fresa em V de {angle}° com ponta de {tip:g} mm. Largura efetiva a "
                     f"0,05 mm de profundidade ≈ {vbit_effective_width(angle, tip, 0.05):.3f} mm; "
                     f"a 0,1 mm ≈ {vbit_effective_width(angle, tip, 0.1):.3f} mm. "
                     + ("Ângulos pequenos (10°–20°) variam pouco de largura com erros de "
                        "nivelamento, mas são frágeis." if angle <= 20 else
                        "Boa relação entre robustez e precisão para isolamento." if angle <= 30 else
                        "Ângulos grandes são robustos, mas a largura cresce rápido com a "
                        "profundidade: exige placa muito bem nivelada (autolevel).")),
        category="vbit",
    )


def _endmill(d: float) -> ToolPreset:
    return ToolPreset(
        key=f"endmill_{d:g}",
        tool=Tool(diameter=d, name=f"Fresa de topo {d:g} mm", shape="flat"),
        description=(f"Fresa de topo reto (end mill / milho) de {d:g} mm. "
                     + ("Útil para isolamento largo, limpeza de cobre e furos grandes."
                        if d < 1.5 else
                        "Indicada para recorte da placa e limpeza rápida de áreas grandes.")),
        category="endmill",
    )


def _drill(d: float) -> ToolPreset:
    return ToolPreset(
        key=f"drill_{d:g}",
        tool=Tool(diameter=d, name=f"Broca {d:g} mm", shape="drill"),
        description=f"Broca helicoidal de {d:g} mm (ponta 118°).",
        category="drill",
    )


DEFAULT_TOOLS: list[ToolPreset] = (
    [_vbit(a) for a in (10, 20, 30, 45, 60)]
    + [_endmill(d) for d in (0.8, 1.0, 2.0)]
    + [_drill(d) for d in (0.6, 0.7, 0.8, 0.9, 1.0, 1.1, 1.2, 1.3, 1.5, 1.6, 2.0, 2.5, 3.0, 3.2)]
)
"""Biblioteca padrão: V-bits 10°/20°/30°/45°/60°, fresas de topo 0,8/1,0/2,0 e brocas 0,6–3,2."""


def tools_by_category(category: str) -> list[ToolPreset]:
    """Ferramentas da biblioteca padrão de uma categoria ("vbit", "endmill", "drill")."""
    return [t for t in DEFAULT_TOOLS if t.category == category]


def standard_drill_sizes() -> list[float]:
    """Diâmetros (mm) das brocas da biblioteca padrão, em ordem crescente."""
    return sorted(t.tool.diameter for t in tools_by_category("drill"))


def effective_diameter(tool: Tool, cut_z: float) -> float:
    """Diâmetro efetivo de corte de ``tool`` no Z de corte informado."""
    if tool.shape == "vbit":
        return vbit_effective_width(tool.vbit_angle, tool.vbit_tip, cut_z)
    return tool.diameter
