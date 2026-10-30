"""Leitor de arquivos de furação Excellon (NC drill), saída em milímetros.

Suporta cabeçalho M48 (METRIC/INCH, LZ/TZ, formato ``0000.00``, ``FMAT``,
``;FILE_FORMAT=4:4``, ``;FORMAT={2:4/...}``), definição de ferramentas ``T1C0.8``
(com F/S/B/H/Z ignorados), corpo com seleção de ferramenta, coordenadas modais
com ou sem ponto decimal, modo incremental (ICI/G91), slots G85, modo de
roteamento (G00/G01 com M15/M16/M17) convertido em slots e atributos
KiCad/Altium para furos metalizados ou não.
"""
from __future__ import annotations

import os
import re

import numpy as np

from brokcam.core.model import DrillLayer, DrillTool

from . import ParseError
from .gerber import MM_PER_INCH, _read_text

__all__ = ["parse_excellon", "parse_excellon_text"]

_NUM = r"[+-]?(?:\d+\.?\d*|\.\d+)"
_TOOL_DEF_RE = re.compile(rf"^T0*(\d+)((?:[CFSBHZ]{_NUM})*)$")
_TOOL_PARAM_RE = re.compile(rf"([CFSBHZ])({_NUM})")
_TOOL_SEL_RE = re.compile(r"^T0*(\d+)$")
_COORD_RE = re.compile(rf"X({_NUM})|Y({_NUM})")
_UNITS_RE = re.compile(r"^(METRIC|INCH)\s*(?:,\s*(LZ|TZ))?\s*(?:,\s*([0#]+)\.([0#]+))?", re.I)
_FILE_FORMAT_RE = re.compile(r"FILE_FORMAT\s*=\s*(\d+)\s*:\s*(\d+)", re.I)
_FORMAT_RE = re.compile(r"FORMAT\s*=?\s*\{\s*(\d+)\s*:\s*(\d+)", re.I)


class _State:
    def __init__(self, name: str) -> None:
        self.name = name
        self.warnings: list[str] = []
        self._warned: set[str] = set()
        self.units: str | None = None
        self.zeros: str | None = None     # "LZ" (zeros à esquerda presentes) | "TZ"
        self.int_digits: int | None = None
        self.dec_digits: int | None = None
        self.incremental = False
        self.diam: dict[int, float] = {}            # mm
        self.plated: dict[int, bool | None] = {}
        self.pending_plated: bool | None = None     # de ";#@! TA.AperFunction" antes do T
        self.file_plated: bool | None = None        # de TF.FileFunction / ;TYPE=
        self.points: dict[int, list[tuple[float, float]]] = {}
        self.slots: dict[int, list[tuple[float, float, float, float]]] = {}
        self.tool: int | None = None
        self.x = 0.0
        self.y = 0.0
        self.mode = "drill"    # "drill" | "rout"
        self.rout_g = 0        # 0 = G00 (movimento), 1 = G01 (corte)
        self.recognized = 0

    def warn(self, msg: str, key: str | None = None) -> None:
        key = key or msg
        if key not in self._warned:
            self._warned.add(key)
            self.warnings.append(msg)

    @property
    def k(self) -> float:
        return MM_PER_INCH if self.units != "MM" else 1.0

    def ensure_units(self) -> None:
        if self.units is None:
            self.units = "IN"
            self.warn("Unidade não declarada (METRIC/INCH ausente): assumindo polegadas.", "no-units")

    def number(self, s: str) -> float:
        """Converte um número de coordenada nas unidades do arquivo (sem escala para mm)."""
        if "." in s:
            return float(s)
        if self.int_digits is None or self.dec_digits is None:
            self.ensure_units()
            self.int_digits, self.dec_digits = (3, 3) if self.units == "MM" else (2, 4)
            self.warn(
                f"Formato de coordenadas sem ponto decimal e não declarado: assumindo "
                f"{self.int_digits}:{self.dec_digits}. Confira as dimensões.", "no-format")
        neg = s.startswith("-")
        digits = s.lstrip("+-")
        if not digits:
            return 0.0
        if self.zeros == "LZ":
            # zeros à esquerda presentes: a posição da vírgula é contada da esquerda
            v = int(digits) / 10 ** (len(digits) - self.int_digits)
        else:
            if self.zeros is None:
                self.warn("Supressão de zeros não declarada (LZ/TZ): assumindo zeros à "
                          "esquerda omitidos.", "no-zeros")
            v = int(digits) / 10 ** self.dec_digits
        return -v if neg else v

    def define_tool(self, num: int, params: str) -> None:
        d = None
        for key, val in _TOOL_PARAM_RE.findall(params):
            if key == "C":
                d = float(val)
        if d is None:
            if num not in self.diam:
                self.warn(f"Ferramenta T{num} sem diâmetro (C) na definição.", f"nodia-{num}")
            return
        self.ensure_units()
        self.diam[num] = d * self.k
        plated = self.pending_plated if self.pending_plated is not None else self.file_plated
        self.plated[num] = plated
        self.pending_plated = None

    def select_tool(self, num: int) -> None:
        if num == 0:
            self.tool = None
            return
        self.tool = num
        if num not in self.diam:
            self.warn(f"Ferramenta T{num} usada sem definição de diâmetro; diâmetro 0 assumido.",
                      f"undef-{num}")

    def coords(self, line: str) -> tuple[float, float]:
        x, y = self.x, self.y
        k = self.k
        for xs, ys in _COORD_RE.findall(line):
            if xs:
                v = self.number(xs) * k
                x = x + v if self.incremental else v
            if ys:
                v = self.number(ys) * k
                y = y + v if self.incremental else v
        return x, y

    def add_hole(self, x: float, y: float) -> None:
        if self.tool is None:
            self.warn("Furo antes de selecionar uma ferramenta; ignorado.", "no-tool")
            return
        self.points.setdefault(self.tool, []).append((x, y))

    def add_slot(self, x0: float, y0: float, x1: float, y1: float) -> None:
        if self.tool is None:
            self.warn("Rasgo antes de selecionar uma ferramenta; ignorado.", "no-tool")
            return
        self.slots.setdefault(self.tool, []).append((x0, y0, x1, y1))


def _plated_from_comment(text: str) -> bool | None:
    t = text.upper().replace(" ", "")
    if "NONPLATED" in t or "NON_PLATED" in t or "NPTH" in t:
        return False
    if "PLATED" in t or "PTH" in t:
        return True
    return None


def _header_comment(st: _State, c: str) -> None:
    cu = c.upper()
    m = _FILE_FORMAT_RE.search(c)
    if m:
        st.int_digits, st.dec_digits = int(m.group(1)), int(m.group(2))
        return
    m = _FORMAT_RE.search(c)
    if m and st.int_digits is None:
        st.int_digits, st.dec_digits = int(m.group(1)), int(m.group(2))
    if "TA.APERFUNCTION" in cu:
        st.pending_plated = _plated_from_comment(cu.split("APERFUNCTION", 1)[1])
    elif "TF.FILEFUNCTION" in cu:
        st.file_plated = _plated_from_comment(cu.split("FILEFUNCTION", 1)[1])
    elif cu.strip().startswith("TYPE="):
        st.file_plated = _plated_from_comment(cu.split("=", 1)[1])


def parse_excellon_text(text: str, name: str = "", source_path: str = "") -> DrillLayer:
    """Interpreta o conteúdo de um arquivo Excellon e devolve uma ``DrillLayer`` (mm).

    ``plated`` de cada ferramenta vem dos atributos KiCad/Altium; na falta deles, de
    "NPTH"/"PTH" no nome do arquivo; senão fica ``None``.
    Levanta ``ParseError`` se o texto não parecer Excellon.
    """
    if not name:
        name = os.path.basename(source_path) if source_path else "Furação"
    st = _State(name)
    in_header = False
    ended = False
    for raw in text.splitlines():
        line = raw.strip()
        if not line:
            continue
        if line.startswith(";"):
            _header_comment(st, line[1:])
            continue
        if "(" in line:  # comentários entre parênteses de alguns CAMs
            line = re.sub(r"\(.*?\)", "", line).strip()
            if not line:
                continue
        u = line.upper()
        if u == "M48":
            in_header = True
            st.recognized += 1
            continue
        if in_header and u in ("%", "M95"):
            in_header = False
            st.recognized += 1
            continue
        # unidades / formato (cabeçalho ou corpo)
        m = _UNITS_RE.match(u)
        if m:
            st.units = "MM" if m.group(1) == "METRIC" else "IN"
            if m.group(2):
                st.zeros = m.group(2)
            if m.group(3):
                st.int_digits, st.dec_digits = len(m.group(3)), len(m.group(4))
            st.recognized += 1
            continue
        if u in ("M71",):
            st.units = "MM"
            st.recognized += 1
            continue
        if u in ("M72",):
            st.units = "IN"
            st.recognized += 1
            continue
        if u.startswith("FMAT") or u.startswith("VER") or u in ("G05", "G81", "G90", "ICI,OFF", "ATC,ON",
                                                                  "ATC,OFF", "OM48", "M47", "G40",
                                                                  "DETECT,ON", "DETECT,OFF"):
            if u == "G05" or u == "G81":
                st.mode = "drill"
            if u == "G90" or u == "ICI,OFF":
                st.incremental = False
            st.recognized += 1
            continue
        if u in ("ICI", "ICI,ON", "G91"):
            st.incremental = True
            st.recognized += 1
            continue
        if u in ("M30", "M00", "M02"):
            ended = True
            st.recognized += 1
            break
        m = _TOOL_DEF_RE.match(u)
        if m and m.group(2) and "C" in m.group(2):
            st.define_tool(int(m.group(1)), m.group(2))
            if not in_header:
                st.select_tool(int(m.group(1)))
            st.recognized += 1
            continue
        if in_header:
            if m:  # T sem C no cabeçalho
                st.recognized += 1
                continue
            st.warn(f"Linha de cabeçalho ignorada: {line[:40]!r}", f"hdr-{u[:8]}")
            continue
        # ----------------------------------------------------------------- corpo
        if m:
            st.select_tool(int(m.group(1)))
            st.recognized += 1
            continue
        if u == "%":
            continue
        if u in ("M15",):
            st.recognized += 1
            continue
        if u in ("M16", "M17"):
            st.recognized += 1
            continue
        g = None
        gm = re.match(r"^G0*(\d+)", u)
        if gm:
            g = int(gm.group(1))
        if "G85" in u:  # slot: X..Y..G85X..Y..
            a, b = u.split("G85", 1)
            x0, y0 = st.coords(a)
            st.x, st.y = x0, y0
            x1, y1 = st.coords(b)
            st.add_slot(x0, y0, x1, y1)
            st.x, st.y = x1, y1
            st.recognized += 1
            continue
        if g in (0, 1, 2, 3):
            st.mode = "rout"
            st.rout_g = 0 if g == 0 else 1
            if g in (2, 3):
                st.warn("Arcos de roteamento (G02/G03) aproximados por segmentos retos.", "rout-arc")
        if u[:1] in ("X", "Y") or (g is not None and ("X" in u or "Y" in u)):
            x, y = st.coords(u)
            if st.mode == "rout":
                if st.rout_g == 1:
                    st.add_slot(st.x, st.y, x, y)
            else:
                st.add_hole(x, y)
            st.x, st.y = x, y
            st.recognized += 1
            continue
        if g is not None:
            st.recognized += 1
            continue
        if u.startswith("R"):
            st.warn("Códigos de repetição (R) não suportados; linha ignorada.", "repeat")
            continue
        if u.startswith("M"):
            st.recognized += 1
            continue
        st.warn(f"Linha ignorada: {line[:40]!r}", f"body-{u[:6]}")
    if st.recognized == 0 or (not st.diam and not st.points and not st.slots):
        raise ParseError(f"'{name}' não parece ser um arquivo de furação Excellon "
                         f"(nenhuma ferramenta ou furo reconhecido).")
    if not ended:
        st.warn("Arquivo terminou sem M30 (pode estar truncado).")

    fname = (source_path or name).upper()
    default_plated: bool | None = None
    if "NPTH" in fname or "NON_PLATED" in fname or "NONPLATED" in fname:
        default_plated = False
    elif "PTH" in fname or "PLATED" in fname:
        default_plated = True

    tools: dict[int, DrillTool] = {}
    for num in sorted(set(st.diam) | set(st.points) | set(st.slots)):
        pts = np.asarray(st.points.get(num, []), dtype=float).reshape(-1, 2)
        slots = np.asarray(st.slots.get(num, []), dtype=float).reshape(-1, 4)
        plated = st.plated.get(num)
        if plated is None:
            plated = st.file_plated if st.file_plated is not None else default_plated
        tools[num] = DrillTool(number=num, diameter=st.diam.get(num, 0.0), points=pts,
                               slots=slots, plated=plated)
    return DrillLayer(name=name, source_path=source_path, tools=tools, warnings=st.warnings)


def parse_excellon(path: str | os.PathLike[str]) -> DrillLayer:
    """Lê um arquivo Excellon do disco. Ver :func:`parse_excellon_text`."""
    p = os.fspath(path)
    return parse_excellon_text(_read_text(p), name=os.path.basename(p), source_path=p)
