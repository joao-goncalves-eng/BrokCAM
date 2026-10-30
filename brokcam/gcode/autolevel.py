"""Heightmap (autolevel): corrige o Z do G-code pela superfície real da placa.

Uma placa de fenolite/FR4 nunca está perfeitamente plana na mesa — variações de 0,05 a
0,2 mm são comuns, e isso já basta para uma fresa em V cortar fundo demais num canto e
nem encostar no outro. A solução (a mesma do Candle/bCNC) é:

1. sondar uma grade de pontos sobre a área da placa e registrar o Z de cada contato;
2. ao enviar o programa, quebrar os movimentos longos em segmentos curtos e somar a
   cada ponto a altura interpolada (bilinear) da superfície naquele X/Y.

As alturas são guardadas em coordenadas de trabalho e aplicadas **relativas ao ponto de
referência** (o zero XY por padrão): assim, se o zero Z for refeito depois (troca de
ferramenta), o mapa continua válido.
"""
from __future__ import annotations

import json
import math
import re
import time
from dataclasses import dataclass, field
from typing import Any, Iterable

import numpy as np


# --------------------------------------------------------------------------- grade

def grid_axes(x0: float, y0: float, x1: float, y1: float, nx: int, ny: int) -> tuple[np.ndarray, np.ndarray]:
    nx = max(2, int(nx))
    ny = max(2, int(ny))
    xa, xb = sorted((float(x0), float(x1)))
    ya, yb = sorted((float(y0), float(y1)))
    return np.linspace(xa, xb, nx), np.linspace(ya, yb, ny)


def grid_counts_for_spacing(x0: float, y0: float, x1: float, y1: float, spacing: float) -> tuple[int, int]:
    """Quantidade de pontos por eixo para um espaçamento máximo ``spacing`` (mm)."""
    sp = max(float(spacing), 0.5)
    return (max(2, math.ceil(abs(x1 - x0) / sp) + 1), max(2, math.ceil(abs(y1 - y0) / sp) + 1))


def probe_order(xs: np.ndarray, ys: np.ndarray) -> list[tuple[int, int]]:
    """Ordem serpentina (vai e volta) — minimiza o deslocamento entre sondagens."""
    order: list[tuple[int, int]] = []
    for j in range(len(ys)):
        cols = range(len(xs)) if j % 2 == 0 else range(len(xs) - 1, -1, -1)
        order.extend((j, i) for i in cols)
    return order


# --------------------------------------------------------------------------- mapa

@dataclass
class HeightMap:
    """Alturas medidas ``z[j, i]`` (mm, coordenadas de trabalho) em ``(xs[i], ys[j])``."""
    xs: np.ndarray
    ys: np.ndarray
    z: np.ndarray
    created: str = field(default_factory=lambda: time.strftime("%Y-%m-%d %H:%M"))
    note: str = ""

    def __post_init__(self) -> None:
        self.xs = np.asarray(self.xs, dtype=float)
        self.ys = np.asarray(self.ys, dtype=float)
        self.z = np.asarray(self.z, dtype=float).reshape(len(self.ys), len(self.xs))

    # ------------------------------------------------------------------ consulta
    @property
    def complete(self) -> bool:
        return bool(np.isfinite(self.z).all())

    @property
    def bounds(self) -> tuple[float, float, float, float]:
        return float(self.xs[0]), float(self.ys[0]), float(self.xs[-1]), float(self.ys[-1])

    def stats(self) -> dict[str, float]:
        v = self.z[np.isfinite(self.z)]
        if not len(v):
            return {"min": 0.0, "max": 0.0, "range": 0.0, "mean": 0.0}
        return {"min": float(v.min()), "max": float(v.max()), "range": float(v.max() - v.min()),
                "mean": float(v.mean())}

    def at(self, x: Any, y: Any) -> Any:
        """Altura interpolada (bilinear); fora da grade usa a borda mais próxima."""
        xs, ys, z = self.xs, self.ys, self.z
        x = np.clip(np.asarray(x, dtype=float), xs[0], xs[-1])
        y = np.clip(np.asarray(y, dtype=float), ys[0], ys[-1])
        i = np.clip(np.searchsorted(xs, x, side="right") - 1, 0, len(xs) - 2)
        j = np.clip(np.searchsorted(ys, y, side="right") - 1, 0, len(ys) - 2)
        tx = (x - xs[i]) / np.where(xs[i + 1] > xs[i], xs[i + 1] - xs[i], 1.0)
        ty = (y - ys[j]) / np.where(ys[j + 1] > ys[j], ys[j + 1] - ys[j], 1.0)
        z00, z10 = z[j, i], z[j, i + 1]
        z01, z11 = z[j + 1, i], z[j + 1, i + 1]
        return (z00 * (1 - tx) * (1 - ty) + z10 * tx * (1 - ty) + z01 * (1 - tx) * ty + z11 * tx * ty)

    # ------------------------------------------------------------------ arquivo
    def to_json(self) -> dict[str, Any]:
        return {"format": "brokcam-heightmap", "version": 1, "created": self.created, "note": self.note,
                "xs": self.xs.round(4).tolist(), "ys": self.ys.round(4).tolist(),
                "z": np.where(np.isfinite(self.z), self.z, np.nan).round(4).tolist()}

    @classmethod
    def from_json(cls, d: dict[str, Any]) -> "HeightMap":
        if d.get("format") != "brokcam-heightmap":
            raise ValueError("Arquivo não é um heightmap do BrokCAM.")
        z = np.array([[np.nan if v is None else v for v in row] for row in d["z"]], dtype=float)
        return cls(np.asarray(d["xs"], float), np.asarray(d["ys"], float), z,
                   d.get("created", ""), d.get("note", ""))

    def save(self, path: str) -> None:
        with open(path, "w", encoding="utf-8") as fh:
            json.dump(self.to_json(), fh, indent=1)

    @classmethod
    def load(cls, path: str) -> "HeightMap":
        with open(path, encoding="utf-8") as fh:
            return cls.from_json(json.load(fh))


# --------------------------------------------------------------------------- aplicação ao G-code

_WORD = re.compile(r"([A-Z])\s*([-+]?(?:\d+\.?\d*|\.\d+))", re.IGNORECASE)
_COMMENT = re.compile(r"\([^)]*\)|;.*$")


@dataclass
class AutolevelResult:
    text: str
    moves_adjusted: int
    lines_in: int
    lines_out: int
    max_correction: float
    min_correction: float
    warnings: list[str] = field(default_factory=list)


def _fmt(v: float, dec: int) -> str:
    s = f"{v:.{dec}f}"
    if "." in s:
        s = s.rstrip("0").rstrip(".")
    return "0" if s in ("-0", "") else s


def apply_heightmap(text: str | Iterable[str], hm: HeightMap, segment: float = 1.0,
                    reference: tuple[float, float] | None = (0.0, 0.0), decimals: int = 4,
                    arc_tolerance: float = 0.01) -> AutolevelResult:
    """Devolve o G-code com o Z corrigido pela superfície do ``hm``.

    * Movimentos G1 longos são divididos em segmentos de no máximo ``segment`` mm.
    * Arcos G2/G3 (plano XY, I/J) viram segmentos de reta e também são corrigidos.
    * A correção é ``H(x, y) − H(referência)``; com ``reference=None`` usa a altura absoluta.
    * Só modo absoluto (G90) e milímetros (G21) são corrigidos; trechos em G91 passam sem
      alteração (com aviso), e G20 gera erro (o mapa é em mm).
    """
    if not hm.complete:
        raise ValueError("O heightmap tem pontos não sondados — termine a sondagem antes de aplicar.")
    lines = text.splitlines() if isinstance(text, str) else list(text)
    ref = float(hm.at(*reference)) if reference is not None else 0.0
    seg = max(float(segment), 0.05)
    out: list[str] = [f"(BrokCAM autolevel: grade {len(hm.xs)}x{len(hm.ys)}, variação "
                      f"{hm.stats()['range']:.3f} mm, segmentos <= {seg:g} mm)"]
    warnings: list[str] = []
    pos: list[float | None] = [None, None, None]
    absolute = True
    motion = 0
    adjusted = 0
    corr_min, corr_max = math.inf, -math.inf
    warned_rel = False

    def corr(x: float, y: float) -> float:
        nonlocal corr_min, corr_max
        c = float(hm.at(x, y)) - ref
        corr_min = min(corr_min, c)
        corr_max = max(corr_max, c)
        return c

    for raw in lines:
        code = _COMMENT.sub("", raw).strip().upper()
        if not code or code.startswith("$") or code.startswith("%"):
            out.append(raw)
            continue
        words = _WORD.findall(code)
        gs = [float(v) for k, v in words if k.upper() == "G"]
        for g in gs:
            if g == 20:
                raise ValueError("O programa usa polegadas (G20); o autolevel trabalha em milímetros (G21).")
            if g == 90:
                absolute = True
            elif g == 91:
                absolute = False
            elif g in (0, 1, 2, 3):
                motion = int(g)
        non_modal = any(g in (4, 10, 28, 30, 53, 92) or 38 <= g < 39 for g in gs)
        axes = {k.upper(): float(v) for k, v in words if k.upper() in "XYZIJ"}
        if not any(k in axes for k in "XYZ") or non_modal:
            if non_modal and any(k in axes for k in "XYZ"):
                pos = [None, None, None]        # posição desconhecida após G28/G38/G53/G92
            out.append(raw)
            continue
        if not absolute:
            if not warned_rel:
                warnings.append("Trechos em coordenadas relativas (G91) foram mantidos sem correção.")
                warned_rel = True
            for n, k in enumerate("XYZ"):
                if k in axes and pos[n] is not None:
                    pos[n] = pos[n] + axes[k]  # type: ignore[operator]
            out.append(raw)
            continue
        start = list(pos)
        tgt = [axes.get(k, pos[n]) for n, k in enumerate("XYZ")]
        others = " ".join(f"{k.upper()}{v}" for k, v in words
                          if k.upper() not in "GXYZIJ" and k.upper() != "N")
        if None in tgt or None in start:
            # sem posição conhecida: corrige só o ponto final, se possível
            if None not in tgt:
                c = corr(tgt[0], tgt[1])  # type: ignore[arg-type]
                adjusted += 1
                gword = f"G{motion}"
                out.append(" ".join(w for w in (gword, f"X{_fmt(tgt[0], decimals)}", f"Y{_fmt(tgt[1], decimals)}",
                                                f"Z{_fmt(tgt[2] + c, decimals)}", others) if w))
            else:
                out.append(raw)
            pos = tgt
            continue
        x0, y0, z0 = start  # type: ignore[misc]
        x1, y1, z1 = tgt    # type: ignore[misc]
        if motion in (2, 3) and ("I" in axes or "J" in axes):
            cx, cy = x0 + axes.get("I", 0.0), y0 + axes.get("J", 0.0)
            r = math.hypot(x0 - cx, y0 - cy)
            a0 = math.atan2(y0 - cy, x0 - cx)
            a1 = math.atan2(y1 - cy, x1 - cx)
            if motion == 2:
                while a1 >= a0:
                    a1 -= 2 * math.pi
            else:
                while a1 <= a0:
                    a1 += 2 * math.pi
            if math.isclose(x0, x1, abs_tol=1e-9) and math.isclose(y0, y1, abs_tol=1e-9):
                a1 = a0 + (-2 * math.pi if motion == 2 else 2 * math.pi)
            sweep = abs(a1 - a0)
            step_a = 2 * math.acos(max(-1.0, min(1.0, 1 - arc_tolerance / r))) if r > arc_tolerance else sweep
            n = max(1, math.ceil(max(sweep * r / seg, sweep / max(step_a, 1e-6))))
            t = np.linspace(0, 1, n + 1)[1:]
            ang = a0 + (a1 - a0) * t
            pts = np.column_stack([cx + r * np.cos(ang), cy + r * np.sin(ang), z0 + (z1 - z0) * t])
            pts[-1] = (x1, y1, z1)
            gword = "G1"
        else:
            d = math.hypot(x1 - x0, y1 - y0)
            n = max(1, math.ceil(d / seg)) if motion == 1 else 1
            t = np.linspace(0, 1, n + 1)[1:]
            pts = np.column_stack([x0 + (x1 - x0) * t, y0 + (y1 - y0) * t, z0 + (z1 - z0) * t])
            gword = f"G{motion}"
        cz = hm.at(pts[:, 0], pts[:, 1]) - ref
        corr_min = min(corr_min, float(np.min(cz)))
        corr_max = max(corr_max, float(np.max(cz)))
        for k, (px, py, pz) in enumerate(pts):
            parts = [gword, f"X{_fmt(px, decimals)}", f"Y{_fmt(py, decimals)}", f"Z{_fmt(pz + cz[k], decimals)}"]
            if k == 0 and others:
                parts.append(others)
            out.append(" ".join(parts))
        adjusted += len(pts)
        pos = tgt
    if corr_min is math.inf:
        corr_min = corr_max = 0.0
    return AutolevelResult("\n".join(out) + "\n", adjusted, len(lines), len(out), corr_max, corr_min, warnings)
