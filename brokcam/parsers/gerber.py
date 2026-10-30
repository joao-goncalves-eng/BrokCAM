"""Leitor de Gerber RS-274X (com atributos X2) otimizado para shapely 2.

Estratégia de desempenho
------------------------
1. O texto inteiro é tokenizado com uma única expressão regular compilada
   (blocos estendidos ``%...%`` e comandos de palavra terminados em ``*``).
2. Durante a interpretação NADA é unido: flashes, trilhas e regiões são apenas
   acumulados em listas (coordenadas numpy), separados por *bloco de polaridade*.
3. Ao fechar um bloco, as primitivas viram geometria de forma vetorizada:
   ``shapely.transform`` replica o modelo de cada abertura em todos os pontos de
   flash, ``shapely.buffer`` engrossa todas as linhas centrais de uma vez e
   ``shapely.disjoint_subset_union_all`` funde tudo numa só chamada ao GEOS.
4. Só há operações booleanas entre blocos quando a polaridade muda (LPD/LPC).

Unidade de saída: milímetros. Coordenadas em polegadas são convertidas na leitura.
"""
from __future__ import annotations

import math
import os
import re
from dataclasses import dataclass, field

import numpy as np
import shapely
from shapely.geometry import MultiPolygon, Polygon
from shapely.geometry.base import BaseGeometry

from brokcam.core.model import Aperture, GerberLayer

from . import ParseError

__all__ = ["parse_gerber", "parse_gerber_text"]

MM_PER_INCH = 25.4

# Bloco estendido %...% (grupo 1) ou comando de palavra terminado em * (grupo 2).
_TOKEN_RE = re.compile(r"%([^%]*)%|([^%*]*)\*", re.S)
_WS_RE = re.compile(r"\s+")
_NUM = r"[+-]?(?:\d+\.?\d*|\.\d+)"
_WORD_RE = re.compile(
    rf"^(?:G0*(\d+))?(?:X({_NUM}))?(?:Y({_NUM}))?(?:I({_NUM}))?(?:J({_NUM}))?"
    r"(?:D0*(\d+))?(?:M0*(\d+))?$"
)
_FS_RE = re.compile(r"^FS([LTD]?)([AI]?)(?:N\d+)?(?:G\d+)?X(\d)(\d)Y(\d)(\d)")
_AD_RE = re.compile(r"^ADD0*(\d+)([A-Za-z_.$][^,]*)(?:,(.*))?$", re.S)
_SR_RE = re.compile(rf"^SR(?:X(\d+))?(?:Y(\d+))?(?:I({_NUM}))?(?:J({_NUM}))?$")
_EXPR_TOKEN_RE = re.compile(r"\s*(\$\d+|(?:\d+\.?\d*|\.\d+)(?:[eE][+-]?\d+)?|[-+xX/()])")


# União otimizada: o GEOS divide a entrada em subconjuntos disjuntos e só une o que
# realmente se toca (≈10× mais rápido que union_all em placas com milhares de pads).
_fast_union = getattr(shapely, "disjoint_subset_union_all", shapely.union_all)


def _union(geoms: "np.ndarray | list[BaseGeometry]") -> BaseGeometry:
    arr = np.asarray(geoms, dtype=object)
    arr = arr[~shapely.is_empty(arr)] if len(arr) else arr
    if len(arr) == 0:
        return Polygon()
    if len(arr) == 1:
        return arr[0]
    return _fast_union(arr)


# --------------------------------------------------------------------------- expressões de macro

class _MacroExpr:
    """Avaliador de expressões aritméticas de macros (``$1x2+0.5``, parênteses, unário)."""

    __slots__ = ("tokens", "pos", "vars")

    def __init__(self, text: str, variables: dict[int, float]) -> None:
        self.tokens: list[str] = []
        pos = 0
        text = text.strip()
        while pos < len(text):
            m = _EXPR_TOKEN_RE.match(text, pos)
            if not m:
                raise ValueError(f"expressão de macro inválida: {text!r}")
            self.tokens.append(m.group(1))
            pos = m.end()
        self.pos = 0
        self.vars = variables

    def evaluate(self) -> float:
        if not self.tokens:
            return 0.0
        v = self._sum()
        if self.pos != len(self.tokens):
            raise ValueError("símbolos sobrando na expressão de macro")
        return v

    def _peek(self) -> str | None:
        return self.tokens[self.pos] if self.pos < len(self.tokens) else None

    def _sum(self) -> float:
        v = self._prod()
        while self._peek() in ("+", "-"):
            op = self.tokens[self.pos]
            self.pos += 1
            r = self._prod()
            v = v + r if op == "+" else v - r
        return v

    def _prod(self) -> float:
        v = self._unary()
        while self._peek() in ("x", "X", "/"):
            op = self.tokens[self.pos]
            self.pos += 1
            r = self._unary()
            if op == "/":
                v = v / r if r != 0 else 0.0
            else:
                v = v * r
        return v

    def _unary(self) -> float:
        t = self._peek()
        if t in ("+", "-"):
            self.pos += 1
            v = self._unary()
            return -v if t == "-" else v
        return self._atom()

    def _atom(self) -> float:
        t = self._peek()
        if t is None:
            raise ValueError("expressão de macro incompleta")
        self.pos += 1
        if t == "(":
            v = self._sum()
            if self._peek() != ")":
                raise ValueError("parêntese não fechado em macro")
            self.pos += 1
            return v
        if t.startswith("$"):
            return float(self.vars.get(int(t[1:]), 0.0))
        return float(t)


def _eval_expr(text: str, variables: dict[int, float]) -> float:
    return _MacroExpr(text, variables).evaluate()


# --------------------------------------------------------------------------- geometria auxiliar

def _rotate(g: BaseGeometry, angle_deg: float) -> BaseGeometry:
    """Rotaciona em torno da origem do macro (sentido anti-horário)."""
    if not angle_deg or g.is_empty:
        return g
    a = math.radians(angle_deg)
    c, s = math.cos(a), math.sin(a)
    rot = np.array([[c, s], [-s, c]])
    return shapely.transform(g, lambda xy: xy @ rot)


def _scale(g: BaseGeometry, k: float) -> BaseGeometry:
    if k == 1.0 or g.is_empty:
        return g
    return shapely.transform(g, lambda xy: xy * k)


def _rect(cx: float, cy: float, w: float, h: float) -> Polygon:
    hw, hh = abs(w) / 2, abs(h) / 2
    return shapely.box(cx - hw, cy - hh, cx + hw, cy + hh)


def _circle(cx: float, cy: float, d: float, quad_segs: int) -> BaseGeometry:
    if d <= 0:
        return Polygon()
    return shapely.Point(cx, cy).buffer(d / 2, quad_segs=quad_segs)


def _vector_line(sx: float, sy: float, ex: float, ey: float, w: float) -> Polygon:
    dx, dy = ex - sx, ey - sy
    ln = math.hypot(dx, dy)
    if ln == 0 or w <= 0:
        return Polygon()
    nx, ny = -dy / ln * w / 2, dx / ln * w / 2
    return Polygon([(sx + nx, sy + ny), (ex + nx, ey + ny), (ex - nx, ey - ny), (sx - nx, sy - ny)])


def _regular_polygon(cx: float, cy: float, d: float, n: int, rot: float) -> Polygon:
    n = max(3, int(round(n)))
    ang = np.radians(rot) + np.arange(n) * 2 * np.pi / n
    return Polygon(np.column_stack([cx + d / 2 * np.cos(ang), cy + d / 2 * np.sin(ang)]))


def _polygonal(g: BaseGeometry) -> BaseGeometry:
    """Mantém apenas as partes poligonais (descarta linhas/pontos degenerados)."""
    if g is None or g.is_empty:
        return Polygon()
    if isinstance(g, (Polygon, MultiPolygon)):
        return g
    parts = shapely.get_parts(g)
    polys: list[Polygon] = []
    for p in parts:
        if isinstance(p, Polygon):
            polys.append(p)
        elif isinstance(p, MultiPolygon):
            polys.extend(p.geoms)
    if not polys:
        return Polygon()
    return polys[0] if len(polys) == 1 else MultiPolygon(polys)


def _arc_points(x0: float, y0: float, x1: float, y1: float, cx: float, cy: float,
                cw: bool, full: bool, tol: float) -> np.ndarray:
    """Discretiza um arco por tolerância de corda. Retorna pontos SEM o inicial."""
    r0 = math.hypot(x0 - cx, y0 - cy)
    r1 = math.hypot(x1 - cx, y1 - cy)
    a0 = math.atan2(y0 - cy, x0 - cx)
    a1 = math.atan2(y1 - cy, x1 - cx)
    two_pi = 2 * math.pi
    if cw:
        sweep = -((a0 - a1) % two_pi)
        if full and abs(sweep) < 1e-9:
            sweep = -two_pi
    else:
        sweep = (a1 - a0) % two_pi
        if full and abs(sweep) < 1e-9:
            sweep = two_pi
    r = max(r0, r1)
    if r < 1e-9 or abs(sweep) < 1e-12:
        return np.array([[x1, y1]], dtype=float)
    if tol >= r:
        step = math.pi / 2
    else:
        step = 2 * math.acos(1 - tol / r)
    n = max(2, int(math.ceil(abs(sweep) / step)))
    t = np.linspace(0.0, 1.0, n + 1)[1:]
    ang = a0 + sweep * t
    rad = r0 + (r1 - r0) * t
    pts = np.column_stack([cx + rad * np.cos(ang), cy + rad * np.sin(ang)])
    pts[-1] = (x1, y1)
    return pts


def _sweep_angle(x0: float, y0: float, x1: float, y1: float, cx: float, cy: float, cw: bool) -> float:
    a0 = math.atan2(y0 - cy, x0 - cx)
    a1 = math.atan2(y1 - cy, x1 - cx)
    return (a0 - a1) % (2 * math.pi) if cw else (a1 - a0) % (2 * math.pi)


# --------------------------------------------------------------------------- macros

@dataclass
class _Macro:
    name: str
    statements: list[str]

    def build(self, args: list[float], quad_segs: int, warnings: list[str]) -> BaseGeometry:
        """Avalia o macro com os parâmetros do %AD (unidades do arquivo)."""
        variables: dict[int, float] = {i + 1: v for i, v in enumerate(args)}
        geom: BaseGeometry = Polygon()
        for st in self.statements:
            if not st:
                continue
            if st[0] == "0" and (len(st) == 1 or not st[1].isdigit() and st[1] != "."):
                continue  # comentário (primitiva 0)
            st = _WS_RE.sub("", st)
            if not st:
                continue
            if st.startswith("$") and "=" in st:
                lhs, rhs = st.split("=", 1)
                try:
                    variables[int(lhs[1:])] = _eval_expr(rhs, variables)
                except ValueError as exc:
                    warnings.append(f"Macro {self.name}: {exc}")
                continue
            fields = st.split(",")
            try:
                code = int(float(fields[0]))
                mods = [_eval_expr(f, variables) for f in fields[1:]]
            except ValueError as exc:
                warnings.append(f"Macro {self.name}: primitiva ignorada ({exc}).")
                continue
            try:
                exposure, prim = self._primitive(code, mods, quad_segs)
            except (IndexError, ValueError) as exc:
                warnings.append(f"Macro {self.name}: primitiva {code} malformada ({exc}).")
                continue
            if prim is None:
                warnings.append(f"Macro {self.name}: primitiva {code} desconhecida, ignorada.")
                continue
            if prim.is_empty:
                continue
            if exposure:
                geom = prim if geom.is_empty else geom.union(prim)
            elif not geom.is_empty:
                geom = geom.difference(prim)
        return _polygonal(geom)

    @staticmethod
    def _primitive(code: int, m: list[float], qs: int) -> tuple[bool, BaseGeometry | None]:
        def mod(i: int, default: float = 0.0) -> float:
            return m[i] if i < len(m) else default

        if code == 1:  # círculo: exp, diâmetro, cx, cy, [rot]
            g = _circle(mod(2), mod(3), mod(1), qs)
            return mod(0) != 0, _rotate(g, mod(4))
        if code in (20, 2):  # linha vetorial: exp, largura, sx, sy, ex, ey, rot
            g = _vector_line(mod(2), mod(3), mod(4), mod(5), mod(1))
            return mod(0) != 0, _rotate(g, mod(6))
        if code == 21:  # linha central: exp, larg, alt, cx, cy, rot
            g = _rect(mod(3), mod(4), mod(1), mod(2))
            return mod(0) != 0, _rotate(g, mod(5))
        if code == 22:  # linha canto inferior esquerdo (obsoleta): exp, larg, alt, x, y, rot
            w, h = mod(1), mod(2)
            g = shapely.box(mod(3), mod(4), mod(3) + w, mod(4) + h)
            return mod(0) != 0, _rotate(g, mod(5))
        if code == 4:  # contorno: exp, n, x0, y0 ... xn, yn, rot
            n = int(round(mod(1)))
            pts = np.array(m[2:2 + 2 * (n + 1)], dtype=float).reshape(-1, 2)
            rot = mod(2 + 2 * (n + 1))
            if len(pts) < 3:
                return True, Polygon()
            poly = Polygon(pts)
            if not poly.is_valid:
                poly = _polygonal(shapely.make_valid(poly))
            return mod(0) != 0, _rotate(poly, rot)
        if code == 5:  # polígono regular: exp, n, cx, cy, diâmetro, rot
            g = _regular_polygon(mod(2), mod(3), mod(4), int(mod(1)), 0.0)
            return mod(0) != 0, _rotate(g, mod(5))
        if code == 6:  # moiré (obsoleta)
            cx, cy, od, th, gap, nmax, cth, clen = (mod(i) for i in range(8))
            g: BaseGeometry = Polygon()
            d = od
            for _ in range(max(0, int(nmax))):
                if d <= 0:
                    break
                ring = _circle(cx, cy, d, qs).difference(_circle(cx, cy, d - 2 * th, qs))
                g = g.union(ring)
                d -= 2 * (th + gap)
            g = g.union(_rect(cx, cy, clen, cth)).union(_rect(cx, cy, cth, clen))
            return True, _rotate(g, mod(8))
        if code == 7:  # térmico: cx, cy, diâm. ext., diâm. int., gap, rot
            cx, cy, od, idia, gap = (mod(i) for i in range(5))
            ring = _circle(cx, cy, od, qs).difference(_circle(cx, cy, idia, qs))
            cross = _rect(cx, cy, od * 2, gap).union(_rect(cx, cy, gap, od * 2))
            return True, _rotate(ring.difference(cross), mod(5))
        return True, None


# --------------------------------------------------------------------------- blocos de polaridade

@dataclass
class _Block:
    """Primitivas acumuladas sob uma única polaridade (sem nenhuma união ainda)."""
    dark: bool
    flashes: dict[str, list[tuple[float, float]]] = field(default_factory=dict)
    line_coords: list[np.ndarray] = field(default_factory=list)   # linhas de aberturas circulares
    line_radii: list[float] = field(default_factory=list)
    sweeps: dict[str, list[tuple[float, float, float, float]]] = field(default_factory=dict)
    regions: list[np.ndarray] = field(default_factory=list)
    extra: list[BaseGeometry] = field(default_factory=list)
    geometry: BaseGeometry | None = None   # cache após finalizar

    def empty(self) -> bool:
        return self.geometry is None and not (
            self.flashes or self.line_coords or self.sweeps or self.regions or self.extra)


class _Interpreter:
    def __init__(self, name: str, quad_segs: int, arc_tolerance: float) -> None:
        self.name = name
        self.qs = max(1, int(quad_segs))
        self.tol = max(1e-5, float(arc_tolerance))
        self.warnings: list[str] = []
        self._warned: set[str] = set()
        # formato e unidades
        self.fs_set = False
        self.zero_mode = "L"
        self.incremental = False
        self.int_x, self.dec_x, self.int_y, self.dec_y = 2, 4, 2, 4
        self.units: str | None = None
        self.k = MM_PER_INCH
        # estado gráfico
        self.x = 0.0
        self.y = 0.0
        self.interp = 1
        self.quadrant: str | None = None
        self.last_d = 2
        self.region = False
        self.contour: list[np.ndarray] = []
        self.cur_ap: str | None = None
        self.polyline: list[np.ndarray] | None = None   # trilha circular em andamento
        self.polyline_ap: str | None = None
        # aberturas
        self.apertures: dict[str, Aperture] = {}
        self.templates: dict[str, BaseGeometry] = {}
        self.radius: dict[str, float] = {}               # só aberturas circulares sem furo
        self.hull: dict[str, np.ndarray] = {}            # casco convexo do modelo
        self.macros: dict[str, _Macro] = {}
        # blocos
        self.blocks: list[_Block] = [_Block(dark=True)]
        self.traces: list[np.ndarray] = []
        self.sr_stack: tuple[int, int, int, float, float, int] | None = None
        self.ab_stack: list[tuple[str, int, int, float, float]] = []
        self.recognized = 0
        self.ended = False

    # ------------------------------------------------------------------ util
    def warn(self, msg: str, key: str | None = None) -> None:
        key = key or msg
        if key in self._warned:
            return
        self._warned.add(key)
        self.warnings.append(msg)

    @property
    def block(self) -> _Block:
        return self.blocks[-1]

    def _ensure_units(self) -> None:
        if self.units is None:
            self.units = "IN"
            self.k = MM_PER_INCH
            self.warn("Unidade não declarada (%MO ausente): assumindo polegadas.", "no-mo")

    def _ensure_format(self) -> None:
        if not self.fs_set:
            self._ensure_units()
            if self.units == "MM":
                self.int_x = self.int_y = 3
                self.dec_x = self.dec_y = 3
            else:
                self.int_x = self.int_y = 2
                self.dec_x = self.dec_y = 4
            self.fs_set = True
            self.warn(
                f"Formato de coordenadas não declarado (%FS ausente): assumindo zeros à esquerda "
                f"omitidos, absoluto, {self.int_x}.{self.dec_x}. Confira as dimensões da placa.",
                "no-fs",
            )

    def _num(self, s: str, intd: int, dec: int) -> float:
        if "." in s:
            return float(s)
        neg = s.startswith("-")
        digits = s.lstrip("+-")
        if self.zero_mode == "T":
            digits = digits.ljust(intd + dec, "0")
        v = int(digits) / (10 ** dec) if digits else 0.0
        return -v if neg else v

    # ------------------------------------------------------------------ blocos
    def close_polyline(self) -> None:
        if self.polyline is not None:
            if len(self.polyline) > 0:
                coords = np.vstack(self.polyline)
                self.block.line_coords.append(coords)
                self.block.line_radii.append(self.radius[self.polyline_ap])  # type: ignore[index]
                self.traces.append(coords)
            self.polyline = None
            self.polyline_ap = None

    def new_block(self, dark: bool) -> None:
        self.close_polyline()
        if self.block.empty():
            self.block.dark = dark
        else:
            self.blocks.append(_Block(dark=dark))

    # ------------------------------------------------------------------ aberturas
    def define_aperture(self, body: str) -> None:
        m = _AD_RE.match(body)
        if not m:
            self.warn(f"Definição de abertura inválida ignorada: %{body}%")
            return
        self._ensure_units()
        code = "D" + m.group(1)
        shape = m.group(2).strip()
        raw = [p for p in (m.group(3) or "").split("X") if p.strip() != ""]
        try:
            args = [float(p) for p in raw]
        except ValueError:
            self.warn(f"Parâmetros inválidos na abertura {code}: {raw}")
            return
        k = self.k
        qs = self.qs

        def arg(i: int, default: float = 0.0) -> float:
            return args[i] if i < len(args) else default

        geom: BaseGeometry
        params: tuple[float, ...]
        if shape == "C":
            d = arg(0)
            geom = _circle(0, 0, d * k, qs)
            hole = arg(1)
            params = (d * k,) + ((hole * k,) if len(args) > 1 else ())
            if hole > 0:
                geom = geom.difference(_circle(0, 0, hole * k, qs))
            elif d > 0:
                self.radius[code] = d * k / 2
        elif shape == "R":
            w, h = arg(0) * k, arg(1, arg(0)) * k
            geom = _rect(0, 0, w, h)
            hole = arg(2) * k
            if hole > 0:
                geom = geom.difference(_circle(0, 0, hole, qs))
            params = tuple(a * k for a in args)
        elif shape == "O":
            w, h = arg(0) * k, arg(1, arg(0)) * k
            if abs(w - h) < 1e-12:
                geom = _circle(0, 0, w, qs)
            elif w > h:
                geom = shapely.LineString([(-(w - h) / 2, 0), ((w - h) / 2, 0)]).buffer(h / 2, quad_segs=qs)
            else:
                geom = shapely.LineString([(0, -(h - w) / 2), (0, (h - w) / 2)]).buffer(w / 2, quad_segs=qs)
            hole = arg(2) * k
            if hole > 0:
                geom = geom.difference(_circle(0, 0, hole, qs))
            params = tuple(a * k for a in args)
        elif shape == "P":
            od, n, rot, hole = arg(0) * k, arg(1, 3), arg(2), arg(3) * k
            geom = _regular_polygon(0, 0, od, int(n), rot)
            if hole > 0:
                geom = geom.difference(_circle(0, 0, hole, qs))
            params = (od, n, rot) + ((hole,) if len(args) > 3 else ())
        elif shape in self.macros:
            geom = _scale(self.macros[shape].build(args, qs, self.warnings), k)
            params = tuple(args)
        else:
            self.warn(f"Abertura {code} usa forma/macro desconhecida '{shape}'; ignorada.")
            return
        self.apertures[code] = Aperture(code=code, shape=shape, params=params)
        self._set_template(code, _polygonal(geom))

    def _set_template(self, code: str, geom: BaseGeometry) -> None:
        self.templates[code] = geom
        if not geom.is_empty:
            hull = shapely.convex_hull(geom)
            coords = shapely.get_coordinates(hull)
            self.hull[code] = coords[:-1] if len(coords) > 1 else coords
        else:
            self.hull[code] = np.zeros((0, 2))

    # ------------------------------------------------------------------ operações
    def _arc(self, x1: float, y1: float, i: float, j: float) -> np.ndarray:
        x0, y0 = self.x, self.y
        cw = self.interp == 2
        if self.quadrant == "single":
            best = None
            for si in (1, -1):
                for sj in (1, -1):
                    cx, cy = x0 + si * abs(i), y0 + sj * abs(j)
                    sw = _sweep_angle(x0, y0, x1, y1, cx, cy, cw)
                    if sw > math.pi / 2 + 1e-3:
                        continue
                    err = abs(math.hypot(x0 - cx, y0 - cy) - math.hypot(x1 - cx, y1 - cy))
                    if best is None or err < best[0]:
                        best = (err, cx, cy)
            if best is None:
                return np.array([[x1, y1]], dtype=float)
            _, cx, cy = best
            return _arc_points(x0, y0, x1, y1, cx, cy, cw, False, self.tol)
        cx, cy = x0 + i, y0 + j
        full = abs(x0 - x1) < 1e-9 and abs(y0 - y1) < 1e-9
        return _arc_points(x0, y0, x1, y1, cx, cy, cw, full, self.tol)

    def interpolate(self, x1: float, y1: float, i: float, j: float) -> None:
        if self.interp == 1:
            pts = np.array([[x1, y1]], dtype=float)
        else:
            if self.quadrant is None:
                self.quadrant = "multi"
                self.warn("Modo de quadrante (G74/G75) não declarado: assumindo G75 (multiquadrante).",
                          "no-quadrant")
            pts = self._arc(x1, y1, i, j)
        if self.region:
            if not self.contour:
                self.contour.append(np.array([[self.x, self.y]], dtype=float))
            self.contour.append(pts)
            return
        ap = self.cur_ap
        if ap is None or ap not in self.templates:
            self.warn(f"Traço desenhado com abertura indefinida ({ap}); ignorado.", f"undef-{ap}")
            return
        if ap in self.radius:
            if self.polyline is None or self.polyline_ap != ap:
                self.close_polyline()
                self.polyline = [np.array([[self.x, self.y]], dtype=float)]
                self.polyline_ap = ap
            self.polyline.append(pts)
        else:
            self.close_polyline()
            seq = np.vstack([[[self.x, self.y]], pts])
            lst = self.block.sweeps.setdefault(ap, [])
            for a, b in zip(seq[:-1], seq[1:]):
                lst.append((a[0], a[1], b[0], b[1]))

    def move(self) -> None:
        self.close_polyline()
        if self.region:
            self._close_contour()

    def flash(self, x: float, y: float) -> None:
        self.close_polyline()
        if self.region:
            self.warn("Flash (D03) dentro de região G36 ignorado.", "flash-region")
            return
        ap = self.cur_ap
        if ap is None or ap not in self.templates:
            self.warn(f"Flash com abertura indefinida ({ap}); ignorado.", f"undef-{ap}")
            return
        self.block.flashes.setdefault(ap, []).append((x, y))

    def _close_contour(self) -> None:
        if self.contour:
            pts = np.vstack(self.contour)
            if len(pts) >= 3:
                self.block.regions.append(pts)
        self.contour = []

    # ------------------------------------------------------------------ comandos
    def word(self, w: str) -> None:
        if not w:
            return
        if w.startswith("G04") or (w.startswith("G4") and (len(w) == 2 or not w[2].isdigit())):
            self.recognized += 1
            return
        m = _WORD_RE.match(w)
        if not m:
            if w.startswith("G54") or w.startswith("G55"):
                m = _WORD_RE.match(w[3:])
            if not m:
                self.warn(f"Comando desconhecido ignorado: {w[:40]!r}", f"unk-{w[:6]}")
                return
        g, xs, ys, is_, js, d, mc = m.groups()
        self.recognized += 1
        if g is not None:
            gi = int(g)
            if gi in (1, 2, 3):
                self.interp = gi
            elif gi == 36:
                self.close_polyline()
                self.region = True
                self.contour = []
            elif gi == 37:
                self._close_contour()
                self.region = False
            elif gi == 74:
                self.quadrant = "single"
            elif gi == 75:
                self.quadrant = "multi"
            elif gi == 70:
                self._set_units("IN")
            elif gi == 71:
                self._set_units("MM")
            elif gi == 90:
                self.incremental = False
            elif gi == 91:
                self.incremental = True
            elif gi in (54, 55, 4):
                pass
            else:
                self.warn(f"Código G{gi} não suportado; ignorado.", f"g{gi}")
        if mc is not None:
            if int(mc) in (0, 2):
                self.ended = True
            return
        op: int | None = None
        if d is not None:
            di = int(d)
            if di >= 10:
                self.close_polyline()
                code = f"D{di}"
                self.cur_ap = code
                if code not in self.templates:
                    self.warn(f"Abertura {code} selecionada sem definição (%AD).", f"undef-{code}")
                return
            if di in (1, 2, 3):
                op = di
                self.last_d = di
        has_xy = xs is not None or ys is not None
        if op is None:
            if not has_xy and is_ is None and js is None:
                return
            op = self.last_d  # uso modal obsoleto de D01/D02/D03
            self.warn("Coordenadas sem código D (uso modal obsoleto): repetindo a última operação.",
                      "modal-d")
        self._ensure_format()
        k = self.k
        x, y = self.x, self.y
        if xs is not None:
            vx = self._num(xs, self.int_x, self.dec_x) * k
            x = x + vx if self.incremental else vx
        if ys is not None:
            vy = self._num(ys, self.int_y, self.dec_y) * k
            y = y + vy if self.incremental else vy
        i = self._num(is_, self.int_x, self.dec_x) * k if is_ is not None else 0.0
        j = self._num(js, self.int_y, self.dec_y) * k if js is not None else 0.0
        if op == 1:
            self.interpolate(x, y, i, j)
        elif op == 2:
            self.move()
        else:
            self.flash(x, y)
        self.x, self.y = x, y

    def _set_units(self, u: str) -> None:
        self.units = u
        self.k = MM_PER_INCH if u == "IN" else 1.0

    def extended(self, body: str) -> None:
        body = body.replace("\r", "").replace("\n", "")
        if not body:
            return
        self.recognized += 1
        head = body[:2]
        if head == "AM":
            parts = body[2:].split("*")
            name = parts[0].strip()
            self.macros[name] = _Macro(name, [p.strip() for p in parts[1:] if p.strip()])
            return
        # demais blocos estendidos podem conter vários comandos separados por '*'
        for cmd in body.split("*"):
            cmd = cmd.strip()
            if cmd:
                self._extended_cmd(cmd)

    def _extended_cmd(self, cmd: str) -> None:
        head = cmd[:2]
        if head == "FS":
            m = _FS_RE.match(_WS_RE.sub("", cmd))
            if not m:
                self.warn(f"Formato %FS não reconhecido: {cmd}")
                return
            zm, ai, ix, dx, iy, dy = m.groups()
            self.zero_mode = "T" if zm == "T" else "L"
            self.incremental = ai == "I"
            self.int_x, self.dec_x, self.int_y, self.dec_y = int(ix), int(dx), int(iy), int(dy)
            self.fs_set = True
        elif head == "MO":
            u = cmd[2:4].upper()
            if u in ("IN", "MM"):
                self._set_units(u)
            else:
                self.warn(f"Unidade desconhecida em %{cmd}%")
        elif head == "AD":
            self.define_aperture(cmd)
        elif head == "LP":
            dark = cmd[2:3].upper() != "C"
            if dark != self.block.dark:
                self.new_block(dark)
        elif head == "SR":
            self._step_repeat(cmd)
        elif head == "AB":
            self._aperture_block(cmd)
        elif head in ("TF", "TA", "TO", "TD", "LN", "IN", "IC"):
            pass  # atributos X2 / nomes: só metadados
        elif head == "IP":
            if "NEG" in cmd:
                self.warn("Imagem negativa (%IPNEG%) não suportada; usando polaridade positiva.")
        elif head in ("OF", "SF", "MI", "IR", "AS", "LM", "LR", "LS"):
            nums = [float(v) for v in re.findall(_NUM, cmd[2:])]
            if head in ("OF", "MI", "IR", "LR"):
                neutral = all(v == 0 for v in nums)
            elif head in ("SF", "LS"):
                neutral = all(v == 1 for v in nums)
            elif head == "LM":
                neutral = cmd[2:].upper() == "N"
            else:  # AS
                neutral = cmd[2:].upper() in ("AXBY", "")
            if not neutral:
                self.warn(f"Comando de transformação %{cmd}% não suportado; ignorado "
                          f"(a geometria pode ficar deslocada/espelhada/escalada).", f"tf-{head}")
        else:
            self.warn(f"Bloco estendido desconhecido ignorado: %{cmd[:30]}%", f"ext-{head}")

    def _step_repeat(self, cmd: str) -> None:
        m = _SR_RE.match(cmd)
        # fecha um SR aberto (todo %SR% encerra o anterior)
        if self.sr_stack is not None:
            self._close_sr()
        if not m:
            self.warn(f"%{cmd}% malformado; ignorado.")
            return
        nx, ny = int(m.group(1) or 1), int(m.group(2) or 1)
        di = float(m.group(3) or 0) * self.k
        dj = float(m.group(4) or 0) * self.k
        if nx <= 1 and ny <= 1:
            return
        self.close_polyline()
        dark = self.block.dark
        self.blocks.append(_Block(dark=dark))
        self.sr_stack = (len(self.blocks) - 1, nx, ny, di, dj, len(self.traces))

    def _close_sr(self) -> None:
        assert self.sr_stack is not None
        start, nx, ny, di, dj, tstart = self.sr_stack
        self.sr_stack = None
        self.close_polyline()
        inner = [b for b in self.blocks[start:] if not b.empty()]
        last_dark = self.blocks[-1].dark
        del self.blocks[start:]
        geoms = [(b.dark, self._finish(b)) for b in inner]
        traces = self.traces[tstart:]
        del self.traces[tstart:]
        for iy in range(ny):
            for ix in range(nx):
                ox, oy = ix * di, iy * dj
                for dark, g in geoms:
                    nb = _Block(dark=dark)
                    nb.geometry = shapely.transform(g, lambda c, ox=ox, oy=oy: c + (ox, oy))
                    self.blocks.append(nb)
                for t in traces:
                    self.traces.append(t + (ox, oy))
        self.blocks.append(_Block(dark=last_dark))

    def _aperture_block(self, cmd: str) -> None:
        self.close_polyline()
        if len(cmd) > 2:  # abertura de bloco: ABD12
            code = "D" + str(int(cmd[3:])) if cmd[2:3] == "D" and cmd[3:].isdigit() else None
            if code is None:
                self.warn(f"%{cmd}% malformado; ignorado.")
                return
            self.blocks.append(_Block(dark=True))
            self.ab_stack.append((code, len(self.blocks) - 1, len(self.traces), self.x, self.y))
            return
        if not self.ab_stack:
            self.warn("%AB% de fechamento sem abertura correspondente.")
            return
        code, start, tstart, x, y = self.ab_stack.pop()
        inner = [b for b in self.blocks[start:] if not b.empty()]
        del self.blocks[start:]
        del self.traces[tstart:]
        geom = _combine([(b.dark, self._finish(b)) for b in inner])
        self.apertures[code] = Aperture(code=code, shape="BLOCK", params=())
        self._set_template(code, geom)
        self.radius.pop(code, None)
        self.blocks.append(_Block(dark=True))
        self.x, self.y = x, y

    # ------------------------------------------------------------------ finalização
    def _finish(self, b: _Block) -> BaseGeometry:
        """Converte as primitivas de um bloco em geometria (vetorizado)."""
        if b.geometry is not None:
            return b.geometry
        parts: list[np.ndarray] = []
        # flashes: replica o modelo da abertura com um único shapely.transform
        for ap, pts in b.flashes.items():
            tpl = self.templates.get(ap)
            if tpl is None or tpl.is_empty:
                continue
            off = np.asarray(pts, dtype=float)
            n = shapely.get_num_coordinates(tpl)
            arr = np.full(len(off), tpl, dtype=object)
            rep = np.repeat(off, n, axis=0)
            parts.append(shapely.transform(arr, lambda c, rep=rep: c + rep))
        # trilhas de aberturas circulares: um único shapely.buffer vetorizado
        if b.line_coords:
            lens = np.fromiter((len(c) for c in b.line_coords), dtype=np.int64, count=len(b.line_coords))
            radii = np.asarray(b.line_radii, dtype=float)
            single = lens == 1
            if single.any():
                pts = np.vstack([c for c, s in zip(b.line_coords, single) if s])
                parts.append(shapely.buffer(shapely.points(pts), radii[single], quad_segs=self.qs))
            multi = ~single
            if multi.any():
                coords = [c for c, s in zip(b.line_coords, multi) if s]
                idx = np.repeat(np.arange(len(coords)), lens[multi])
                lines = shapely.linestrings(np.vstack(coords), indices=idx)
                parts.append(shapely.buffer(lines, radii[multi], quad_segs=self.qs))
        # trilhas com aberturas não circulares: casco convexo do modelo nas duas pontas
        for ap, segs in b.sweeps.items():
            hull = self.hull.get(ap)
            if hull is None or len(hull) == 0:
                continue
            s = np.asarray(segs, dtype=float)
            a = hull[None, :, :] + s[:, None, 0:2]
            c = hull[None, :, :] + s[:, None, 2:4]
            mp = shapely.multipoints(np.concatenate([a, c], axis=1))
            parts.append(shapely.convex_hull(mp))
        # regiões G36/G37
        if b.regions:
            closed = [r if np.array_equal(r[0], r[-1]) else np.vstack([r, r[:1]]) for r in b.regions]
            lens = np.fromiter((len(r) for r in closed), dtype=np.int64, count=len(closed))
            idx = np.repeat(np.arange(len(closed)), lens)
            rings = shapely.linearrings(np.vstack(closed), indices=idx)
            polys = shapely.polygons(rings)
            bad = ~shapely.is_valid(polys)
            if bad.any():
                try:
                    polys[bad] = shapely.make_valid(polys[bad], method="structure", keep_collapsed=False)
                except TypeError:  # GEOS antigo
                    polys[bad] = shapely.buffer(polys[bad], 0)
            parts.append(polys)
        if b.extra:
            parts.append(np.asarray(b.extra, dtype=object))
        if not parts:
            b.geometry = Polygon()
        else:
            allp = np.concatenate(parts)
            b.geometry = _polygonal(_union(allp))
        return b.geometry

    def result(self) -> BaseGeometry:
        self.close_polyline()
        if self.region:
            self._close_contour()
            self.warn("Região G36 sem G37 de fechamento.")
        while self.ab_stack:
            self.warn("Bloco de abertura %AB% não fechado.")
            self._aperture_block("AB")
        if self.sr_stack is not None:
            self._close_sr()
        return _combine([(b.dark, self._finish(b)) for b in self.blocks if not b.empty()])


def _combine(seq: list[tuple[bool, BaseGeometry]]) -> BaseGeometry:
    """Aplica os blocos na ordem: escuros são unidos, claros (LPC) subtraídos."""
    result: BaseGeometry = Polygon()
    pending: list[BaseGeometry] = []
    for dark, g in seq:
        if g.is_empty:
            continue
        if dark:
            pending.append(g)
        else:
            if pending:
                pending.append(result)
                result = _union(pending)
                pending = []
            if not result.is_empty:
                result = result.difference(g)
    if pending:
        pending.append(result)
        result = _union(pending)
    return _polygonal(result)


# --------------------------------------------------------------------------- API pública

def parse_gerber_text(text: str, name: str = "", source_path: str = "", *,
                      quad_segs: int = 16, arc_tolerance: float = 0.005) -> GerberLayer:
    """Interpreta o conteúdo de um arquivo Gerber RS-274X e devolve uma ``GerberLayer``.

    Parâmetros:
        text: conteúdo do arquivo.
        name: nome da camada (padrão: nome do arquivo ou "Gerber").
        source_path: caminho de origem (apenas informativo).
        quad_segs: segmentos por quarto de círculo em pads e pontas de trilhas.
        arc_tolerance: erro máximo de corda (mm) ao discretizar arcos G02/G03.

    ``copper`` é a união de todas as áreas escuras com a polaridade clear aplicada na
    ordem do arquivo; ``traces`` contém as linhas centrais (mm) das trilhas desenhadas
    com aberturas circulares. Avisos não fatais vão para ``warnings``.

    Levanta ``ParseError`` se o texto não contiver nenhum comando Gerber reconhecível.
    """
    if not name:
        name = os.path.basename(source_path) if source_path else "Gerber"
    it = _Interpreter(name, quad_segs, arc_tolerance)
    for m in _TOKEN_RE.finditer(text):
        if it.ended:
            break
        ext = m.group(1)
        if ext is not None:
            it.extended(ext)
        else:
            w = m.group(2)
            if w.startswith("G04") or w.lstrip().startswith("G04"):
                it.recognized += 1
                continue
            it.word(_WS_RE.sub("", w))
    if it.recognized == 0:
        raise ParseError(f"'{name}' não parece ser um arquivo Gerber: nenhum comando reconhecido.")
    if not it.fs_set and not it.apertures:
        raise ParseError(f"'{name}' não parece ser um arquivo Gerber RS-274X válido "
                         f"(sem %FS e sem definições de abertura).")
    copper = it.result()
    if not it.ended:
        it.warn("Arquivo terminou sem M02 (pode estar truncado).")
    if copper.is_empty:
        it.warn("Nenhuma área de cobre resultante nesta camada.")
    return GerberLayer(name=name, source_path=source_path, copper=copper,
                       apertures=it.apertures, traces=it.traces, warnings=it.warnings)


def _read_text(path: str) -> str:
    try:
        with open(path, "rb") as fh:
            data = fh.read()
    except FileNotFoundError:
        raise ParseError(f"Arquivo não encontrado: {path}") from None
    except OSError as exc:
        raise ParseError(f"Não foi possível abrir '{path}': {exc.strerror or exc}") from None
    if b"\x00" in data[:4096]:
        raise ParseError(f"'{os.path.basename(path)}' parece ser um arquivo binário, não texto.")
    try:
        return data.decode("utf-8")
    except UnicodeDecodeError:
        return data.decode("latin-1")


def parse_gerber(path: str | os.PathLike[str], *, quad_segs: int = 16,
                 arc_tolerance: float = 0.005) -> GerberLayer:
    """Lê um arquivo Gerber do disco. Ver :func:`parse_gerber_text`."""
    p = os.fspath(path)
    return parse_gerber_text(_read_text(p), name=os.path.basename(p), source_path=p,
                             quad_segs=quad_segs, arc_tolerance=arc_tolerance)
