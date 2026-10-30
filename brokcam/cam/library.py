"""Biblioteca de ferramentas do usuário.

Cada ferramenta da biblioteca guarda a geometria (formato, diâmetro, ângulo da V-bit)
**e** os dados de corte que funcionaram para ela (profundidade, avanços, rotação). Ao
escolher uma ferramenta numa operação, esses valores preenchem o formulário — assim o
usuário configura uma vez e reaproveita em todos os projetos.

A biblioteca é um JSON simples (lista de ferramentas), sem dependência de Qt. A
interface decide onde salvar (``%APPDATA%/BrokCAM/tools.json``).
"""
from __future__ import annotations

import json
import os
import uuid
from dataclasses import asdict, dataclass, field, fields
from pathlib import Path
from typing import Any, Iterable

from brokcam.core.model import CutParams, Tool
from brokcam.core.params import ParamGroup, ParamSpec, register, set_conditions, set_derive, set_locked

from .tools import (DEFAULT_TOOLS, VBIT_LOCK_NOTE, ToolPreset, vbit_auto_cut_z, vbit_auto_values,
                    vbit_effective_width, vbit_width_from_cut_z)

LIBRARY_FORMAT = "brokcam-tools"
LIBRARY_VERSION = 1

SHAPES = (
    ("vbit", "Fresa em V (gravador)"),
    ("flat", "Fresa de topo reto"),
    ("ball", "Fresa de ponta esférica"),
    ("drill", "Broca"),
    ("laser", "Laser"),
)

OPERATION_CHOICES = ("isolation", "ncc", "cutout", "drilling")
OPERATION_LABELS = {"isolation": "Isolamento", "ncc": "Limpeza de cobre", "cutout": "Recorte",
                    "drilling": "Furação"}

# Operações em que cada formato faz sentido por padrão.
_DEFAULT_OPS = {
    "vbit": ["isolation", "ncc"],
    "flat": ["isolation", "ncc", "cutout", "drilling"],
    "ball": ["isolation", "ncc"],
    "drill": ["drilling"],
    "laser": ["isolation", "ncc", "cutout"],
}


@dataclass
class LibraryTool:
    """Ferramenta da biblioteca: geometria + dados de corte + onde pode ser usada."""
    name: str
    shape: str = "flat"
    diameter: float = 1.0
    vbit_angle: float = 30.0
    vbit_tip: float = 0.1
    number: int = 1
    cut_z: float = -0.1
    travel_z: float = 1.0
    feed_xy: float = 120.0
    feed_z: float = 60.0
    spindle_rpm: float = 10000.0
    multi_depth: bool = False
    depth_per_pass: float = 0.1
    dwell_s: float = 0.0
    laser_power: float = 1000.0
    operations: list[str] = field(default_factory=list)
    notes: str = ""
    uid: str = field(default_factory=lambda: uuid.uuid4().hex[:12])
    # Fresa em V: largura do sulco desejada; o Z de corte é derivado dela (automático).
    vbit_width: float | None = None

    def __post_init__(self) -> None:
        if not self.operations:
            self.operations = list(_DEFAULT_OPS.get(self.shape, []))
        if self.shape == "vbit":
            if self.vbit_width is None:     # biblioteca antiga: preserva o Z que estava salvo
                self.vbit_width = vbit_width_from_cut_z(self.vbit_angle, self.vbit_tip, self.cut_z)
            self.cut_z = round(vbit_auto_cut_z(self.vbit_angle, self.vbit_tip, float(self.vbit_width)), 6)

    # ------------------------------------------------------------------ conversões
    @property
    def effective_diameter(self) -> float:
        """Largura real do corte: para V-bit depende do Z de corte."""
        if self.shape == "vbit":
            return vbit_effective_width(self.vbit_angle, self.vbit_tip, self.cut_z)
        return self.diameter

    def to_tool(self) -> Tool:
        shape = self.shape if self.shape in ("flat", "vbit", "ball", "drill", "laser") else "flat"
        return Tool(diameter=self.effective_diameter, name=self.name, number=self.number,
                    shape=shape, vbit_angle=self.vbit_angle, vbit_tip=self.vbit_tip)  # type: ignore[arg-type]

    def cut_params(self) -> CutParams:
        return CutParams(cut_z=-abs(self.cut_z), travel_z=abs(self.travel_z), feed_xy=self.feed_xy,
                         feed_z=self.feed_z, spindle_rpm=self.spindle_rpm, multi_depth=self.multi_depth,
                         depth_per_pass=self.depth_per_pass, dwell_s=self.dwell_s,
                         laser_mode=self.shape == "laser", laser_power=self.laser_power)

    def form_values(self) -> dict[str, Any]:
        """Valores no formato do ``TOOL_ENTRY`` (para o formulário de edição)."""
        d = asdict(self)
        for op in OPERATION_CHOICES:
            d[f"op_{op}"] = op in self.operations
        d.pop("operations")
        d.pop("uid")
        if d.get("vbit_width") is None:      # só fresas em V têm largura do sulco
            d.pop("vbit_width", None)
        return d

    @classmethod
    def from_form_values(cls, values: dict[str, Any], uid: str | None = None) -> "LibraryTool":
        known = {f.name for f in fields(cls)}
        kw = {k: v for k, v in values.items() if k in known and k not in ("operations", "uid")}
        kw["operations"] = [op for op in OPERATION_CHOICES if values.get(f"op_{op}")]
        if kw.get("shape") != "vbit":
            kw.pop("vbit_width", None)
        t = cls(**kw)
        if uid:
            t.uid = uid
        return t

    def to_json(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_json(cls, d: dict[str, Any]) -> "LibraryTool":
        known = {f.name for f in fields(cls)}
        return cls(**{k: v for k, v in d.items() if k in known})

    def summary(self) -> str:
        """Descrição curta para listas: formato, medida e dados de corte principais."""
        if self.shape == "vbit":
            geo = f"V {self.vbit_angle:g}° ponta {self.vbit_tip:g} mm → {self.effective_diameter:.3f} mm"
        elif self.shape == "laser":
            geo = f"feixe {self.diameter:g} mm · S{self.laser_power:g}"
        else:
            geo = f"Ø {self.diameter:g} mm"
        cut = f"Z {round(self.cut_z, 3):g} · F{self.feed_xy:g}" if self.shape != "laser" else f"F{self.feed_xy:g}"
        return f"{geo} · {cut}"

    # ------------------------------------------------------------------ aplicação
    def apply(self, op_key: str, settings: dict[str, Any], group: ParamGroup | None = None,
              library: "ToolLibrary | None" = None) -> dict[str, Any]:
        """Copia geometria e dados de corte desta ferramenta para ``settings`` da operação.

        Só chaves que existem no grupo da operação são escritas (se ``group`` for dado).
        Retorna um novo dicionário.
        """
        out = dict(settings)
        laser = self.shape == "laser"
        vals: dict[str, Any] = {
            "cut_z": -abs(self.cut_z), "travel_z": abs(self.travel_z), "feed_xy": self.feed_xy,
            "feed_z": self.feed_z, "spindle_rpm": self.spindle_rpm, "multi_depth": self.multi_depth,
            "depth_per_pass": self.depth_per_pass, "dwell_s": self.dwell_s,
            "laser_mode": laser, "laser_power": self.laser_power,
        }
        if op_key == "drilling":
            if self.shape == "drill":
                drills = sorted({round(t.diameter, 3) for t in (library.tools if library else [self])
                                 if t.shape == "drill"})
                vals["available_drills"] = ", ".join(f"{d:g}" for d in drills)
            else:
                vals["mill_tool_dia"] = self.diameter
                vals["mill_large"] = True
            vals.pop("multi_depth")
            vals.pop("depth_per_pass")
        else:
            vals["tool_shape"] = "vbit" if self.shape == "vbit" else "flat"
            vals["tool_dia"] = self.effective_diameter if self.shape != "vbit" else self.diameter
            vals["vbit_angle"] = self.vbit_angle
            vals["vbit_tip"] = self.vbit_tip
            if self.shape == "vbit" and self.vbit_width is not None:
                vals["vbit_width"] = self.vbit_width
            vals["tool_number"] = self.number
        if group is not None:
            keys = {p.key for p in group.params}
            vals = {k: v for k, v in vals.items() if k in keys}
            for k, v in list(vals.items()):
                try:
                    vals[k] = group.spec(k).validate(v)
                except (ValueError, TypeError):
                    vals.pop(k)
        out.update(vals)
        out["library_tool"] = self.uid
        return out


# --------------------------------------------------------------------------- formulário

def _op_spec(op: str) -> ParamSpec:
    return ParamSpec(
        f"op_{op}", f"Usar em: {OPERATION_LABELS[op]}", "bool", False,
        help=(f"Mostra esta ferramenta no seletor da operação {OPERATION_LABELS[op]}. Desmarque "
              "para esconder ferramentas que não fazem sentido ali (ex.: brocas no isolamento). "
              "Não impede o uso — apenas organiza a lista."))


TOOL_ENTRY = register(ParamGroup(
    key="tool_library",
    title="Ferramenta da biblioteca",
    description=(
        "Cadastro de uma ferramenta física que você possui, com os dados de corte que funcionam "
        "na sua máquina. Ao escolher a ferramenta numa operação, estes valores preenchem o "
        "formulário automaticamente — ajuste uma vez, reutilize em todos os projetos."),
    params=(
        ParamSpec("name", "Nome", "str", "Nova ferramenta",
                  help=("Nome que aparece nas listas e nas mensagens de troca de ferramenta do "
                        "G-code. Inclua o que diferencia a ferramenta, ex.: “V 30° 0,1 mm (chinesa)” "
                        "ou “Broca 0,8 mm metal duro”.")),
        ParamSpec("shape", "Formato", "choice", "flat", choices=SHAPES,
                  help=("Tipo de ferramenta. Fresa em V: a largura do sulco cresce com a "
                        "profundidade (calculada automaticamente). Topo reto: largura = diâmetro. "
                        "Ponta esférica: tratada como topo reto no diâmetro informado. Broca: usada "
                        "na furação. Laser: G-code sem eixo Z, potência pelo valor S.")),
        ParamSpec("diameter", "Diâmetro", "float", 1.0, unit="mm", minimum=0.01, maximum=50.0,
                  step=0.05, decimals=3,
                  help=("Diâmetro de corte da ferramenta. Para fresa em V é ignorado (vale a ponta "
                        "e o ângulo); para laser, é a largura do ponto focado (≈0,08–0,2 mm). Meça "
                        "com paquímetro: o valor gravado na haste costuma ser nominal.")),
        ParamSpec("vbit_angle", "Ângulo da V", "float", 30.0, unit="°", minimum=1.0, maximum=170.0,
                  step=5.0, decimals=1,
                  help=("Ângulo total da ponta da fresa em V (10°, 20°, 30°, 45°, 60°, 90°). Só vale "
                        "para formato V. Ângulos menores variam menos de largura se a placa não "
                        "estiver perfeitamente nivelada, mas quebram com mais facilidade.")),
        ParamSpec("vbit_tip", "Ponta da V", "float", 0.1, unit="mm", minimum=0.0, maximum=2.0,
                  step=0.01, decimals=3,
                  help=("Largura da ponta plana da fresa em V (0,1 mm típico; 0,2 mm nas mais "
                        "robustas). Junto com o ângulo e o Z de corte define a largura do sulco: "
                        "largura = ponta + 2·profundidade·tan(ângulo/2).")),
        ParamSpec("vbit_width", "Largura do sulco (V)", "float", 0.15, unit="mm", minimum=0.01,
                  maximum=5.0, step=0.01, decimals=3,
                  help=("Largura de corte desejada para a fresa em V. O Z de corte é calculado "
                        "automaticamente a partir dela e fica travado: profundidade = (largura − "
                        "ponta) / (2·tan(ângulo/2)). Típico para V 30°/0,1 mm: 0,13–0,20 mm.")),
        ParamSpec("number", "Número (T)", "int", 1, minimum=1, maximum=99,
                  help=("Número da ferramenta usado no comando T do G-code e na mensagem de troca. "
                        "Útil se você organiza as fresas num suporte numerado. Ferramentas diferentes "
                        "podem ter o mesmo número; a troca é pedida quando muda a ferramenta física.")),
        ParamSpec("cut_z", "Z de corte", "float", -0.1, unit="mm", minimum=-10.0, maximum=0.0,
                  step=0.01, decimals=3,
                  help=("Profundidade de corte (negativa, abaixo da superfície do cobre). Isolamento: "
                        "-0,05 a -0,1 mm para cobre de 35 µm. Recorte: espessura da placa + 0,1 mm "
                        "(ex.: -1,7 mm). Furação: espessura + ponta da broca (ex.: -1,8 mm).")),
        ParamSpec("travel_z", "Z de deslocamento", "float", 1.0, unit="mm", minimum=0.1, maximum=50.0,
                  step=0.5, decimals=2,
                  help=("Altura segura para movimentos rápidos (G0) entre cortes. 1–2 mm basta em "
                        "placa bem presa; aumente se houver grampos ou parafusos no caminho.")),
        ParamSpec("feed_xy", "Avanço XY", "float", 120.0, unit="mm/min", minimum=1.0, maximum=10000.0,
                  step=10.0, decimals=0,
                  help=("Velocidade de corte no plano. Fresa V fina: 60–200 mm/min. Topo 1 mm em "
                        "FR4: 100–300 mm/min. Laser: 300–1500 mm/min. Muito rápido quebra a ferramenta "
                        "ou deixa rebarba; muito lento queima/derrete o substrato.")),
        ParamSpec("feed_z", "Avanço Z (mergulho)", "float", 60.0, unit="mm/min", minimum=1.0,
                  maximum=5000.0, step=10.0, decimals=0,
                  help=("Velocidade de descida da ferramenta no material. Use cerca de metade do "
                        "avanço XY. Brocas finas (< 0,8 mm) pedem 30–60 mm/min.")),
        ParamSpec("spindle_rpm", "Rotação", "float", 10000.0, unit="RPM", minimum=0.0, maximum=60000.0,
                  step=500.0, decimals=0,
                  help=("Rotação do spindle (valor S com M3). Deve respeitar a faixa $30/$31 do "
                        "GRBL. Spindles de PCB típicos: 8.000–12.000 RPM; motores de 775/ER11 "
                        "chegam a 12.000–24.000.")),
        ParamSpec("multi_depth", "Multiprofundidade", "bool", False,
                  help=("Corta em várias passadas até o Z de corte. Necessário no recorte da placa "
                        "(1,6 mm de uma vez quebra fresas finas) e em brocas para furação em bicadas.")),
        ParamSpec("depth_per_pass", "Profundidade por passada", "float", 0.1, unit="mm", minimum=0.01,
                  maximum=5.0, step=0.05, decimals=3,
                  help=("Quanto a ferramenta desce a cada passada na multiprofundidade. Regra "
                        "prática: até metade do diâmetro para fresas de topo (1 mm → 0,4–0,5 mm).")),
        ParamSpec("dwell_s", "Pausa após ligar spindle", "float", 0.0, unit="s", minimum=0.0,
                  maximum=60.0, step=0.5, decimals=1, advanced=True,
                  help=("Tempo de espera (G4) para o spindle atingir a rotação antes do primeiro "
                        "mergulho. O pós-processador usa o maior entre este e o aquecimento global.")),
        ParamSpec("laser_power", "Potência do laser", "float", 1000.0, unit="S", minimum=0.0,
                  maximum=100000.0, step=50.0, decimals=0,
                  help=("Valor S enviado com M4 no modo laser ($32=1). A escala vai de 0 a $30 "
                        "(normalmente 1000 = 100%). Só vale para formato Laser.")),
        *(_op_spec(op) for op in OPERATION_CHOICES),
        ParamSpec("notes", "Observações", "str", "",
                  help=("Anotações livres: fornecedor, material testado, data da última afiação, "
                        "resultado obtido. Não afeta o G-code.")),
    ),
))


_MILL = ("vbit", "flat", "ball", "drill")
set_conditions(TOOL_ENTRY, {
    "diameter": (("shape", ("flat", "ball", "drill", "laser")),),
    "vbit_angle": (("shape", ("vbit",)),),
    "vbit_tip": (("shape", ("vbit",)),),
    "vbit_width": (("shape", ("vbit",)),),
    "cut_z": (("shape", _MILL),),
    "travel_z": (("shape", _MILL),),
    "feed_z": (("shape", _MILL),),
    "spindle_rpm": (("shape", _MILL),),
    "multi_depth": (("shape", _MILL),),
    "depth_per_pass": (("multi_depth", (True,)), ("shape", _MILL)),
    "dwell_s": (("shape", _MILL),),
    "laser_power": (("shape", ("laser",)),),
})
set_locked(TOOL_ENTRY, {"cut_z": (("shape", ("vbit",)),)}, VBIT_LOCK_NOTE)
set_derive(TOOL_ENTRY, lambda v, raw: vbit_auto_values(v, raw, "shape"))


# --------------------------------------------------------------------------- biblioteca

def _from_preset(p: ToolPreset) -> LibraryTool:
    t = p.tool
    if p.category == "vbit":
        return LibraryTool(name=t.name, shape="vbit", diameter=t.vbit_tip, vbit_angle=t.vbit_angle,
                           vbit_tip=t.vbit_tip, number=1, cut_z=-0.08, travel_z=1.0, feed_xy=120.0,
                           feed_z=50.0, spindle_rpm=10000.0, operations=list(_DEFAULT_OPS["vbit"]),
                           notes=p.description)
    if p.category == "endmill":
        big = t.diameter >= 1.5
        return LibraryTool(name=t.name, shape="flat", diameter=t.diameter, number=2,
                           cut_z=-1.7 if big else -0.1, travel_z=2.0 if big else 1.0,
                           feed_xy=150.0, feed_z=40.0, spindle_rpm=12000.0, multi_depth=big,
                           depth_per_pass=round(t.diameter * 0.4, 2),
                           operations=list(_DEFAULT_OPS["flat"]), notes=p.description)
    return LibraryTool(name=t.name, shape="drill", diameter=t.diameter, number=3, cut_z=-1.8,
                       travel_z=2.0, feed_xy=150.0, feed_z=40.0 if t.diameter < 0.8 else 60.0,
                       spindle_rpm=12000.0, operations=["drilling"], notes=p.description)


def default_tools() -> list[LibraryTool]:
    """Biblioteca inicial: V-bits, fresas de topo, brocas e um laser de exemplo."""
    tools = [_from_preset(p) for p in DEFAULT_TOOLS]
    tools.append(LibraryTool(name="Laser diodo 5 W", shape="laser", diameter=0.1, cut_z=0.0,
                             feed_xy=600.0, feed_z=600.0, spindle_rpm=0.0, laser_power=1000.0,
                             operations=list(_DEFAULT_OPS["laser"]),
                             notes="Exemplo para gravar máscara/tinta em placa pintada. Ajuste a "
                                   "potência e o avanço com um teste em sobra de placa."))
    return tools


class ToolLibrary:
    """Coleção ordenada de :class:`LibraryTool` com persistência em JSON."""

    def __init__(self, tools: Iterable[LibraryTool] | None = None, path: str | os.PathLike[str] | None = None):
        self.tools: list[LibraryTool] = list(tools) if tools is not None else default_tools()
        self.path = Path(path) if path else None

    # ------------------------------------------------------------------ consulta
    def __len__(self) -> int:
        return len(self.tools)

    def __iter__(self):  # type: ignore[no-untyped-def]
        return iter(self.tools)

    def get(self, uid: str | None) -> LibraryTool | None:
        if not uid:
            return None
        return next((t for t in self.tools if t.uid == uid), None)

    def for_operation(self, op_key: str) -> list[LibraryTool]:
        return [t for t in self.tools if op_key in t.operations]

    # ------------------------------------------------------------------ edição
    def add(self, tool: LibraryTool, index: int | None = None) -> LibraryTool:
        if self.get(tool.uid) is not None:
            tool.uid = uuid.uuid4().hex[:12]
        self.tools.insert(len(self.tools) if index is None else index, tool)
        return tool

    def update(self, tool: LibraryTool) -> None:
        for i, t in enumerate(self.tools):
            if t.uid == tool.uid:
                self.tools[i] = tool
                return
        self.tools.append(tool)

    def remove(self, uid: str) -> LibraryTool | None:
        for i, t in enumerate(self.tools):
            if t.uid == uid:
                return self.tools.pop(i)
        return None

    def duplicate(self, uid: str) -> LibraryTool | None:
        src = self.get(uid)
        if src is None:
            return None
        d = LibraryTool.from_json({**src.to_json(), "uid": uuid.uuid4().hex[:12],
                                   "name": f"{src.name} (cópia)"})
        return self.add(d, self.tools.index(src) + 1)

    def move(self, uid: str, delta: int) -> None:
        t = self.get(uid)
        if t is None:
            return
        i = self.tools.index(t)
        j = max(0, min(len(self.tools) - 1, i + delta))
        self.tools.insert(j, self.tools.pop(i))

    def reset_defaults(self) -> None:
        self.tools = default_tools()

    # ------------------------------------------------------------------ arquivo
    def to_json(self) -> dict[str, Any]:
        return {"format": LIBRARY_FORMAT, "version": LIBRARY_VERSION,
                "tools": [t.to_json() for t in self.tools]}

    @staticmethod
    def parse(data: Any) -> list[LibraryTool]:
        if isinstance(data, dict) and data.get("format") in (LIBRARY_FORMAT, "tracecam-tools"):
            items = data.get("tools", [])
        elif isinstance(data, list):
            items = data
        else:
            raise ValueError("Arquivo não é uma biblioteca de ferramentas do BrokCAM.")
        return [LibraryTool.from_json(d) for d in items if isinstance(d, dict) and d.get("name")]

    @classmethod
    def load(cls, path: str | os.PathLike[str]) -> "ToolLibrary":
        """Lê a biblioteca de ``path``; se não existir, devolve a biblioteca padrão."""
        p = Path(path)
        if not p.exists():
            return cls(path=p)
        with open(p, encoding="utf-8") as fh:
            return cls(cls.parse(json.load(fh)), path=p)

    def save(self, path: str | os.PathLike[str] | None = None) -> Path:
        p = Path(path) if path else self.path
        if p is None:
            raise ValueError("Caminho da biblioteca não definido.")
        p.parent.mkdir(parents=True, exist_ok=True)
        tmp = p.with_suffix(p.suffix + ".tmp")
        with open(tmp, "w", encoding="utf-8") as fh:
            json.dump(self.to_json(), fh, indent=2, ensure_ascii=False)
        os.replace(tmp, p)
        self.path = p
        return p

    def import_file(self, path: str | os.PathLike[str], replace: bool = False) -> int:
        """Importa ferramentas de outro arquivo. Retorna quantas foram adicionadas."""
        with open(path, encoding="utf-8") as fh:
            new = self.parse(json.load(fh))
        if replace:
            self.tools = []
        existing = {t.uid for t in self.tools}
        for t in new:
            if t.uid in existing:
                self.update(t)
            else:
                self.add(t)
        return len(new)

    def export_file(self, path: str | os.PathLike[str], uids: Iterable[str] | None = None) -> None:
        sel = set(uids) if uids is not None else None
        data = {"format": LIBRARY_FORMAT, "version": LIBRARY_VERSION,
                "tools": [t.to_json() for t in self.tools if sel is None or t.uid in sel]}
        with open(path, "w", encoding="utf-8") as fh:
            json.dump(data, fh, indent=2, ensure_ascii=False)
