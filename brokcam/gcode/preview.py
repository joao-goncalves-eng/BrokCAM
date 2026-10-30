"""Interpretador de G-code para pré-visualização e estimativa de tempo.

``parse_gcode`` lê um programa (GRBL/FlatCAM/BrokCAM ou de outros CAMs) e devolve os
segmentos de deslocamento rápido e de corte como arrays numpy prontos para desenhar,
além do número da linha de origem de cada segmento (para destacar a linha em
execução durante o envio) e uma estimativa de tempo.

Suporta G0/G1/G2/G3 (arcos discretizados, inclusive helicoidais, com I/J ou R),
G4, G17 (G18/G19 geram aviso e arcos nesses planos viram retas), G20/G21, G90/G91,
G90.1/G91.1, G53, G38.x (tratado como avanço), comentários ``(...)`` e ``;``,
números de linha N, linhas ``$`` (ignoradas) e T/M6/M0 (trocas de ferramenta).
"""
from __future__ import annotations

import math
import re
from dataclasses import dataclass, field

import numpy as np

__all__ = ["GcodePreview", "parse_gcode"]

_COMMENT = re.compile(r"\(([^)]*)\)|;(.*)$")
_WORD = re.compile(r"([A-Z])\s*([-+]?(?:\d+\.?\d*|\.\d+))")


@dataclass
class GcodePreview:
    """Resultado da interpretação de um programa G-code.

    Todas as coordenadas em mm, no sistema de coordenadas de trabalho do programa.
    Índices de linha são 0-based (índice em ``text.splitlines()``); o número exibido
    ao usuário é ``índice + 1``.
    """
    rapids: np.ndarray                 # (N, 2, 3) segmentos G0
    feeds: np.ndarray                  # (M, 2, 3) segmentos G1/G2/G3
    bounds: tuple[float, float, float, float, float, float]  # xmin,ymin,zmin,xmax,ymax,zmax
    segments: np.ndarray               # (K, 2, 3) todos os segmentos em ordem de execução
    is_rapid: np.ndarray               # (K,) bool
    line_of_segment: np.ndarray        # (K,) int32: linha de origem de cada segmento
    rapid_lines: np.ndarray            # (N,) int32: linha de origem de cada rapid
    feed_lines: np.ndarray             # (M,) int32: linha de origem de cada feed
    segment_end_time: np.ndarray       # (K,) float: tempo acumulado (s) ao fim de cada segmento
    estimate_time_s: float             # tempo total estimado (movimentos + pausas G4)
    tool_changes: list[tuple[int, int]] = field(default_factory=list)  # (linha, ferramenta)
    line_count: int = 0
    cut_length_mm: float = 0.0
    rapid_length_mm: float = 0.0
    warnings: list[str] = field(default_factory=list)

    def time_at_line(self, line: int) -> float:
        """Tempo estimado decorrido até o fim da linha ``line`` (0-based)."""
        if not len(self.line_of_segment):
            return 0.0
        i = int(np.searchsorted(self.line_of_segment, line, side="right")) - 1
        return float(self.segment_end_time[i]) if i >= 0 else 0.0

    def segments_of_line(self, line: int) -> np.ndarray:
        """Índices (em ``segments``) dos segmentos gerados pela linha ``line``."""
        lo = np.searchsorted(self.line_of_segment, line, side="left")
        hi = np.searchsorted(self.line_of_segment, line, side="right")
        return np.arange(lo, hi)


def _arc_points(x0: float, y0: float, x1: float, y1: float, cx: float, cy: float,
                cw: bool, tol: float, full_if_same: bool) -> np.ndarray:
    """Pontos (excluindo o inicial) que discretizam um arco no plano XY."""
    r = math.hypot(x0 - cx, y0 - cy)
    a0 = math.atan2(y0 - cy, x0 - cx)
    a1 = math.atan2(y1 - cy, x1 - cx)
    if cw:
        sweep = a0 - a1
        if sweep <= 1e-12:
            sweep += 2 * math.pi
    else:
        sweep = a1 - a0
        if sweep <= 1e-12:
            sweep += 2 * math.pi
    if not full_if_same and sweep >= 2 * math.pi - 1e-9:
        sweep = 0.0
    if r < 1e-9:
        return np.array([[x1, y1]])
    step = 2 * math.acos(max(-1.0, min(1.0, 1 - tol / r))) if r > tol else math.pi / 4
    step = min(max(step, 1e-3), math.pi / 8)
    n = max(1, int(math.ceil(sweep / step)))
    t = np.linspace(0.0, 1.0, n + 1)[1:]
    ang = a0 - sweep * t if cw else a0 + sweep * t
    pts = np.column_stack((cx + r * np.cos(ang), cy + r * np.sin(ang)))
    pts[-1] = (x1, y1)
    return pts


def _trapezoid_times(length: np.ndarray, vmax: np.ndarray, v0: np.ndarray,
                     v1: np.ndarray, acc: float) -> np.ndarray:
    """Tempo de cada segmento com perfil trapezoidal (vetorizado). Velocidades em mm/s."""
    v0 = np.minimum(v0, vmax)
    v1 = np.minimum(v1, vmax)
    d_acc = (vmax ** 2 - v0 ** 2) / (2 * acc)
    d_dec = (vmax ** 2 - v1 ** 2) / (2 * acc)
    full = d_acc + d_dec <= length
    with np.errstate(divide="ignore", invalid="ignore"):
        t_full = (vmax - v0) / acc + (vmax - v1) / acc + (length - d_acc - d_dec) / vmax
        vp = np.sqrt(np.maximum((2 * acc * length + v0 ** 2 + v1 ** 2) / 2, 0.0))
        vp = np.maximum(vp, np.maximum(v0, v1))
        t_tri = (vp - v0) / acc + (vp - v1) / acc
        t_simple = np.where(vmax > 0, length / vmax, 0.0)
    t = np.where(full, t_full, t_tri)
    t = np.where(np.isfinite(t), t, t_simple)
    return np.maximum(t, t_simple)


def parse_gcode(text: str, rapid_feed: float = 1000.0, accel_mm_s2: float | None = None,
                arc_tolerance: float = 0.01, default_feed: float = 100.0) -> GcodePreview:
    """Interpreta ``text`` e devolve um ``GcodePreview``.

    Parâmetros:
        rapid_feed: velocidade dos G0 em mm/min (use $110/$111 da máquina).
        accel_mm_s2: aceleração (ex.: $120) para estimativa mais realista; ``None``
            = sem aceleração (tempo = comprimento / avanço).
        arc_tolerance: erro máximo de corda (mm) ao discretizar arcos.
        default_feed: avanço assumido se o programa cortar antes de definir F.
    """
    lines = text.splitlines()
    starts: list[tuple[float, float, float]] = []
    ends: list[tuple[float, float, float]] = []
    rapid_flags: list[bool] = []
    seg_lines: list[int] = []
    seg_feeds: list[float] = []
    dwell_after: dict[int, float] = {}   # índice do próximo segmento -> segundos
    tool_changes: list[tuple[int, int]] = []
    warnings: list[str] = []

    x = y = z = 0.0
    motion = 0          # 0,1,2,3 ou -1 (G80/sem movimento)
    absolute = True
    arc_abs = False
    scale = 1.0
    feed = 0.0
    plane = 17
    pending_tool: int | None = None
    warned: set[str] = set()

    def warn(key: str, msg: str) -> None:
        if key not in warned:
            warned.add(key)
            warnings.append(msg)

    word_re = _WORD.findall
    comment_sub = _COMMENT.sub

    for idx, raw in enumerate(lines):
        if not raw:
            continue
        line = raw.strip()
        if not line or line[0] == "$" or line[0] == "%":
            continue
        if "(" in line or ";" in line:
            line = comment_sub("", line).strip()
            if not line:
                continue
        words = word_re(line.upper())
        if not words:
            continue

        nx = ny = nz = None
        ii = jj = rr = None
        p_val = None
        g_codes: list[float] = []
        non_motion_axes = False
        machine_coords = False
        for letter, val in words:
            if letter == "X":
                nx = float(val)
            elif letter == "Y":
                ny = float(val)
            elif letter == "Z":
                nz = float(val)
            elif letter == "G":
                g_codes.append(float(val))
            elif letter == "F":
                feed = float(val) * scale
            elif letter == "I":
                ii = float(val)
            elif letter == "J":
                jj = float(val)
            elif letter == "R":
                rr = float(val)
            elif letter == "P":
                p_val = float(val)
            elif letter == "T":
                pending_tool = int(float(val))
            elif letter == "M":
                m = int(float(val))
                if m == 6:
                    tool_changes.append((idx, pending_tool if pending_tool is not None else -1))
                    pending_tool = None
                elif m == 0 and pending_tool is not None:
                    tool_changes.append((idx, pending_tool))
                    pending_tool = None

        seg_motion = motion
        dwell = None
        for g in g_codes:
            if g == 0 or g == 1 or g == 2 or g == 3:
                motion = int(g)
                seg_motion = motion
            elif g == 90:
                absolute = True
            elif g == 91:
                absolute = False
            elif g == 21:
                scale = 1.0
            elif g == 20:
                scale = 25.4
            elif g == 4:
                dwell = p_val or 0.0
                non_motion_axes = True
            elif g == 17:
                plane = 17
            elif g == 18 or g == 19:
                plane = int(g)
                warn("plane", "Planos G18/G19: arcos fora de XY são exibidos como retas.")
            elif abs(g - 90.1) < 1e-6:
                arc_abs = True
            elif abs(g - 91.1) < 1e-6:
                arc_abs = False
            elif g == 53:
                machine_coords = True
                warn("g53", "G53 (coordenadas de máquina) é exibido como coordenadas de trabalho.")
            elif 38.15 < g < 38.55:
                seg_motion = 1
                motion = 1
            elif g == 80:
                motion = -1
                seg_motion = -1
            elif g in (10, 92, 28, 30, 43.1, 49):
                non_motion_axes = True
                if g in (28, 30, 92):
                    warn(f"g{g:g}", f"G{g:g} não é simulado na pré-visualização.")
            elif 81 <= g <= 89:
                warn("canned", "Ciclos fixos (G81-G89) não são suportados pelo GRBL e foram ignorados.")
            elif g == 93:
                warn("g93", "G93 (avanço inverso) não é considerado na estimativa de tempo.")

        if dwell is not None:
            dwell_after[len(starts)] = dwell_after.get(len(starts), 0.0) + float(dwell)
        if non_motion_axes or (nx is None and ny is None and nz is None):
            continue
        if seg_motion < 0:
            continue


        if absolute:
            tx = nx * scale if nx is not None else x
            ty = ny * scale if ny is not None else y
            tz = nz * scale if nz is not None else z
        else:
            tx = x + (nx * scale if nx is not None else 0.0)
            ty = y + (ny * scale if ny is not None else 0.0)
            tz = z + (nz * scale if nz is not None else 0.0)

        if seg_motion in (2, 3) and plane == 17:
            cw = seg_motion == 2
            if rr is not None:
                r = rr * scale
                dx, dy = tx - x, ty - y
                d = math.hypot(dx, dy)
                if d < 1e-12:
                    warn("arcr", "Arco com R e ponto final igual ao inicial foi ignorado.")
                    x, y, z = tx, ty, tz
                    continue
                h2 = r * r - (d / 2) ** 2
                h = math.sqrt(h2) if h2 > 0 else 0.0
                # sentido: centro à direita (CW) para r>0 em arcos menores que 180°
                sign = -1.0 if cw else 1.0
                if r < 0:
                    sign = -sign
                mx, my = x + dx / 2, y + dy / 2
                cx = mx - sign * h * dy / d
                cy = my + sign * h * dx / d
                full = False
            else:
                ci = (ii or 0.0) * scale
                cj = (jj or 0.0) * scale
                if arc_abs:
                    cx, cy = ci, cj
                else:
                    cx, cy = x + ci, y + cj
                full = True
            pts = _arc_points(x, y, tx, ty, cx, cy, cw, arc_tolerance, full)
            n = len(pts)
            zs = z + (tz - z) * np.arange(1, n + 1) / n
            px, py, pz = x, y, z
            for (ax, ay), az in zip(pts.tolist(), zs.tolist()):
                starts.append((px, py, pz))
                ends.append((ax, ay, az))
                px, py, pz = ax, ay, az
            rapid_flags.extend([False] * n)
            seg_lines.extend([idx] * n)
            seg_feeds.extend([feed] * n)
        else:
            starts.append((x, y, z))
            ends.append((tx, ty, tz))
            rapid_flags.append(seg_motion == 0)
            seg_lines.append(idx)
            seg_feeds.append(feed)
        x, y, z = tx, ty, tz

    k = len(starts)
    if k:
        seg = np.empty((k, 2, 3), dtype=np.float64)
        seg[:, 0, :] = np.asarray(starts, dtype=np.float64)
        seg[:, 1, :] = np.asarray(ends, dtype=np.float64)
    else:
        seg = np.zeros((0, 2, 3), dtype=np.float64)
    is_rapid = np.asarray(rapid_flags, dtype=bool)
    lines_arr = np.asarray(seg_lines, dtype=np.int32)
    feeds_arr = np.asarray(seg_feeds, dtype=np.float64)

    # ---- tempo
    vec = seg[:, 1, :] - seg[:, 0, :]
    length = np.sqrt((vec ** 2).sum(axis=1)) if k else np.zeros(0)
    if k and np.any((~is_rapid) & (feeds_arr <= 0)):
        warn("nofeed", f"Corte sem avanço F definido: assumido {default_feed:g} mm/min.")
    v = np.where(is_rapid, rapid_feed, np.where(feeds_arr > 0, feeds_arr, default_feed)) / 60.0
    if accel_mm_s2 and k:
        # velocidade de junção ~ v * cos(ângulo) entre segmentos consecutivos
        with np.errstate(invalid="ignore", divide="ignore"):
            u = vec / length[:, None]
        u = np.nan_to_num(u)
        cosang = np.zeros(k)
        if k > 1:
            cosang[1:] = np.clip((u[1:] * u[:-1]).sum(axis=1), 0.0, 1.0)
        vj = np.zeros(k)
        if k > 1:
            vj[1:] = np.minimum(v[1:], v[:-1]) * cosang[1:]
            # direção muda entre rapid/feed: considera parada
            vj[1:] = np.where(is_rapid[1:] != is_rapid[:-1], 0.0, vj[1:])
        v0 = vj
        v1 = np.zeros(k)
        v1[:-1] = vj[1:]
        t_seg = _trapezoid_times(length, v, v0, v1, float(accel_mm_s2))
    else:
        with np.errstate(divide="ignore", invalid="ignore"):
            t_seg = np.where(v > 0, length / v, 0.0)
    trailing = 0.0
    if dwell_after:
        extra = np.zeros(k + 1)
        for i, sec in dwell_after.items():
            extra[i] += sec
        # pausa antes do segmento i => entra no tempo acumulado do segmento i
        t_seg = t_seg + extra[:k]
        trailing = float(extra[k])
    cum = np.cumsum(t_seg) if k else np.zeros(0)
    total = (float(cum[-1]) if k else 0.0) + trailing

    if k:
        pts = seg.reshape(-1, 3)
        mn = pts.min(axis=0)
        mx = pts.max(axis=0)
        bounds = (float(mn[0]), float(mn[1]), float(mn[2]), float(mx[0]), float(mx[1]), float(mx[2]))
    else:
        bounds = (0.0, 0.0, 0.0, 0.0, 0.0, 0.0)

    return GcodePreview(
        rapids=seg[is_rapid],
        feeds=seg[~is_rapid],
        bounds=bounds,
        segments=seg,
        is_rapid=is_rapid,
        line_of_segment=lines_arr,
        rapid_lines=lines_arr[is_rapid],
        feed_lines=lines_arr[~is_rapid],
        segment_end_time=cum,
        estimate_time_s=total,
        tool_changes=tool_changes,
        line_count=len(lines),
        cut_length_mm=float(length[~is_rapid].sum()) if k else 0.0,
        rapid_length_mm=float(length[is_rapid].sum()) if k else 0.0,
        warnings=warnings,
    )
