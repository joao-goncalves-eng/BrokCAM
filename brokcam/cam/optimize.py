"""Ordenação de furos e caminhos para minimizar o deslocamento em vazio (G0).

Estratégia (sem scipy):

1. **Vizinho mais próximo** acelerado:
   * pontos — grade espacial uniforme (busca em anéis de células com critério de
     parada exato e reconstrução da grade quando ela fica esparsa);
   * polilinhas — limite inferior vetorizado (distância à caixa envolvente dos anéis
     fechados, distância exata às pontas das abertas, desigualdade triangular com a
     última distância exata calculada) e avaliação exata só dos candidatos.
2. **2-opt em janela, vetorizado**: para cada comprimento de segmento ``k`` calcula
   em numpy o ganho de inverter todos os segmentos ``[i, i+k]`` de uma vez e aplica
   um conjunto de movimentos que não se sobrepõem. Repete até não haver ganho ou
   esgotar o tempo.
3. Polilinhas fechadas são "rotacionadas" para começar no vértice mais próximo da
   posição anterior; abertas podem ser invertidas (se permitido).
"""
from __future__ import annotations

import math
import time
from typing import Sequence

import numpy as np

from brokcam.core.model import Job

Point = tuple[float, float]


# =========================================================================== métricas

def travel_length(items: np.ndarray | Sequence[np.ndarray], start: Point = (0.0, 0.0)) -> float:
    """Comprimento total de deslocamento em vazio.

    ``items`` pode ser um array (K,2) de furos (visitados em ordem, partindo de
    ``start``) ou uma lista de polilinhas (deslocamento = fim de uma → início da
    próxima).
    """
    s = np.asarray(start, dtype=float)
    if isinstance(items, np.ndarray) and items.ndim == 2:
        if not len(items):
            return 0.0
        seq = np.vstack([s[None, :], items[:, :2]])
        return float(np.hypot(*np.diff(seq, axis=0).T).sum())
    paths = [p for p in items if len(p)]
    if not paths:
        return 0.0
    firsts = np.array([p[0, :2] for p in paths])
    lasts = np.vstack([s[None, :], np.array([p[-1, :2] for p in paths[:-1]])]) if len(paths) > 1 \
        else s[None, :]
    return float(np.hypot(*(firsts - lasts).T).sum())


# =========================================================================== 2-opt

def _two_opt(entry: np.ndarray, exit_: np.ndarray, start: np.ndarray, deadline: float,
             window: int = 40) -> np.ndarray:
    """2-opt em janela sobre uma sequência de itens com ponto de entrada e saída.

    Inverter o bloco ``[i..j]`` inverte a ordem dos itens e troca entrada↔saída de
    cada um (o custo interno do bloco é preservado). Retorna ``(perm, flipped)`` codificado
    como array (n,2): coluna 0 = índice original, coluna 1 = 1 se o item foi invertido.
    Modifica ``entry``/``exit_`` no lugar.
    """
    n = len(entry)
    perm = np.arange(n)
    flip = np.zeros(n, dtype=np.int8)
    if n < 2:
        return np.column_stack([perm, flip])
    same = bool(np.all(entry == exit_))
    eps = 1e-9
    improved = True
    while improved and time.perf_counter() < deadline:
        improved = False
        kmin = 1 if same else 0
        for k in range(kmin, min(window, n - 1) + 1):
            if time.perf_counter() >= deadline:
                break
            m = n - k                     # i em [0, m), j = i + k
            prev = np.vstack([start[None, :], exit_[:-1]])
            i = np.arange(m)
            j = i + k
            p_i = prev[i]
            e_i = entry[i]
            x_j = exit_[j]
            before = np.hypot(*(p_i - e_i).T)
            after = np.hypot(*(p_i - x_j).T)
            has_next = j + 1 < n
            jn = np.minimum(j + 1, n - 1)
            e_n = entry[jn]
            before = before + np.where(has_next, np.hypot(*(x_j - e_n).T), 0.0)
            after = after + np.where(has_next, np.hypot(*(e_i - e_n).T), 0.0)
            gain = before - after
            cand = np.flatnonzero(gain > eps)
            if not len(cand):
                continue
            cand = cand[np.argsort(-gain[cand], kind="stable")]
            occupied = np.zeros(n + 2, dtype=bool)   # índice deslocado em +1
            applied = 0
            for ii in cand.tolist():
                jj = ii + k
                lo, hi = ii, jj + 2            # faixa [ii-1, jj+1] deslocada em +1
                if occupied[lo:hi + 1].any():
                    continue
                occupied[lo:hi + 1] = True
                sl = slice(ii, jj + 1)
                ne = exit_[sl][::-1].copy()
                nx = entry[sl][::-1].copy()
                entry[sl] = ne
                exit_[sl] = nx
                perm[sl] = perm[sl][::-1].copy()
                flip[sl] = 1 - flip[sl][::-1]
                applied += 1
            if applied:
                improved = True
    return np.column_stack([perm, flip])


# =========================================================================== pontos

def _nn_small(pts: np.ndarray, start: np.ndarray) -> np.ndarray:
    n = len(pts)
    alive = np.ones(n, dtype=bool)
    order = np.empty(n, dtype=np.int64)
    cur = start
    for k in range(n):
        d = np.hypot(pts[:, 0] - cur[0], pts[:, 1] - cur[1])
        d[~alive] = np.inf
        b = int(np.argmin(d))
        order[k] = b
        alive[b] = False
        cur = pts[b]
    return order


class _Grid:
    """Grade espacial uniforme com listas de índices vivos por célula."""

    def __init__(self, pts: np.ndarray, idx: np.ndarray) -> None:
        sub = pts[idx]
        self.xmin = float(sub[:, 0].min())
        self.ymin = float(sub[:, 1].min())
        w = float(sub[:, 0].max()) - self.xmin
        h = float(sub[:, 1].max()) - self.ymin
        m = len(idx)
        area = w * h
        if area > 0:
            cs = math.sqrt(area / m) * 1.5
        else:
            cs = max(w, h) / max(m, 1) * 2.0
        self.cs = cs if cs > 0 else 1.0
        self.nx = int(w / self.cs) + 1
        self.ny = int(h / self.cs) + 1
        cx = np.minimum(((sub[:, 0] - self.xmin) / self.cs).astype(np.int64), self.nx - 1)
        cy = np.minimum(((sub[:, 1] - self.ymin) / self.cs).astype(np.int64), self.ny - 1)
        cell = cy * self.nx + cx
        order = np.argsort(cell, kind="stable")
        cs_sorted = cell[order]
        idx_sorted = idx[order]
        bounds = np.searchsorted(cs_sorted, np.arange(self.nx * self.ny + 1))
        self.cells: list[list[int]] = [idx_sorted[bounds[c]:bounds[c + 1]].tolist()
                                       for c in range(self.nx * self.ny)]
        self.cell_of = {int(i): int(c) for i, c in zip(idx.tolist(), cell.tolist())}
        self.count = m

    def remove(self, i: int) -> None:
        self.cells[self.cell_of[i]].remove(i)
        self.count -= 1

    def nearest(self, x: float, y: float, xs: list[float], ys: list[float]) -> int:
        cs = self.cs
        ccx = math.floor((x - self.xmin) / cs)
        ccy = math.floor((y - self.ymin) / cs)
        nx, ny = self.nx, self.ny
        rmax = max(abs(ccx), abs(ccx - (nx - 1)), abs(ccy), abs(ccy - (ny - 1))) + 1
        best = -1
        bd = math.inf
        cells = self.cells
        hypot = math.hypot
        r = 0
        while r <= rmax:
            y0, y1 = ccy - r, ccy + r
            x0, x1 = ccx - r, ccx + r
            for cy in range(max(y0, 0), min(y1, ny - 1) + 1):
                edge_row = cy == y0 or cy == y1
                if edge_row:
                    xr = range(max(x0, 0), min(x1, nx - 1) + 1)
                else:
                    xr = [c for c in (x0, x1) if 0 <= c < nx]
                base = cy * nx
                for cx in xr:
                    lst = cells[base + cx]
                    for i in lst:
                        d = hypot(xs[i] - x, ys[i] - y)
                        if d < bd:
                            bd = d
                            best = i
            if best >= 0 and bd <= r * cs:
                break
            r += 1
        return best


def _nn_grid(pts: np.ndarray, start: np.ndarray) -> np.ndarray:
    n = len(pts)
    xs = pts[:, 0].tolist()
    ys = pts[:, 1].tolist()
    alive = np.ones(n, dtype=bool)
    grid = _Grid(pts, np.arange(n))
    built = n
    order = np.empty(n, dtype=np.int64)
    x, y = float(start[0]), float(start[1])
    for k in range(n):
        if grid.count < built // 4 and grid.count > 64:
            grid = _Grid(pts, np.flatnonzero(alive))
            built = grid.count
        b = grid.nearest(x, y, xs, ys)
        grid.remove(b)
        alive[b] = False
        order[k] = b
        x, y = xs[b], ys[b]
    return order


def order_points(points: np.ndarray, start: Point = (0.0, 0.0), time_limit: float = 0.3) -> np.ndarray:
    """Índices de ``points`` (K,2) na ordem de visita otimizada (cada um exatamente uma vez)."""
    pts = np.asarray(points, dtype=float)[:, :2] if len(points) else np.zeros((0, 2))
    n = len(pts)
    if n <= 1:
        return np.arange(n)
    t0 = time.perf_counter()
    s = np.asarray(start, dtype=float)
    order = _nn_small(pts, s) if n <= 200 else _nn_grid(pts, s)
    if time_limit > 0 and n > 2:
        e = pts[order].copy()
        res = _two_opt(e, e.copy(), s, t0 + max(time_limit, 0.0))
        order = order[res[:, 0]]
    return order


def optimize_points(points: np.ndarray, start: Point = (0.0, 0.0), time_limit: float = 0.3) -> np.ndarray:
    """Retorna ``points`` reordenados para minimizar o deslocamento."""
    pts = np.asarray(points, dtype=float)
    if len(pts) <= 1:
        return pts.copy()
    return pts[order_points(pts, start, time_limit)]


# =========================================================================== polilinhas

def _rotate_ring(ring: np.ndarray, k: int) -> np.ndarray:
    body = ring[:-1]
    if k == 0:
        return ring
    body = np.roll(body, -k, axis=0)
    return np.vstack([body, body[:1]])


def optimize_paths(paths: Sequence[np.ndarray], start: Point = (0.0, 0.0), allow_reverse: bool = True,
                   rotate_closed: bool = True, time_limit: float = 0.3) -> list[np.ndarray]:
    """Reordena polilinhas para minimizar o deslocamento em vazio.

    * Fechadas (1º ponto == último): entram pelo vértice mais próximo (``rotate_closed``);
      o sentido de giro é preservado (importa para climb/convencional).
    * Abertas: podem ser percorridas ao contrário se ``allow_reverse``.
    """
    P = [np.asarray(p, dtype=float) for p in paths if len(p)]
    n = len(P)
    if n == 0:
        return []
    t0 = time.perf_counter()
    s = np.asarray(start, dtype=float)[:2]
    firsts = np.array([p[0, :2] for p in P])
    lasts = np.array([p[-1, :2] for p in P])
    lens = np.fromiter((len(p) for p in P), dtype=np.int64, count=n)
    closed = (lens > 2) & (np.abs(firsts - lasts).max(axis=1) <= 1e-9)
    closed_rot = closed if rotate_closed else np.zeros(n, dtype=bool)
    bmin = np.array([p[:, :2].min(axis=0) for p in P])
    bmax = np.array([p[:, :2].max(axis=0) for p in P])
    ex_d = np.zeros(n)               # última distância exata calculada (anéis)
    ex_pt = np.zeros((n, 2))          # ponto em que foi calculada
    has_ex = np.zeros(n, dtype=bool)
    alive = np.ones(n, dtype=bool)

    order: list[int] = []
    rev: list[bool] = []
    rot: list[int] = []
    cur = s.copy()
    for _ in range(n):
        # limites inferiores vetorizados
        d_first = np.hypot(*(firsts - cur).T)
        d_last = np.hypot(*(lasts - cur).T)
        lb_open = np.minimum(d_first, d_last) if allow_reverse else d_first
        dx = np.maximum(np.maximum(bmin[:, 0] - cur[0], cur[0] - bmax[:, 0]), 0.0)
        dy = np.maximum(np.maximum(bmin[:, 1] - cur[1], cur[1] - bmax[:, 1]), 0.0)
        lb_ring = np.hypot(dx, dy)
        tri = np.where(has_ex, ex_d - np.hypot(*(ex_pt - cur).T), 0.0)
        lb_ring = np.maximum(lb_ring, tri)
        lb = np.where(closed_rot, lb_ring, lb_open)
        lb[~alive] = np.inf
        exact = ~closed_rot.copy()
        while True:
            b = int(np.argmin(lb))
            if exact[b]:
                break
            pts = P[b][:-1, :2]
            dd = np.hypot(pts[:, 0] - cur[0], pts[:, 1] - cur[1])
            k = int(np.argmin(dd))
            lb[b] = dd[k]
            exact[b] = True
            ex_d[b] = dd[k]
            ex_pt[b] = cur
            has_ex[b] = True
        alive[b] = False
        order.append(b)
        if closed_rot[b]:
            pts = P[b][:-1, :2]
            k = int(np.argmin(np.hypot(pts[:, 0] - cur[0], pts[:, 1] - cur[1])))
            rot.append(k)
            rev.append(False)
            cur = pts[k].copy()
        else:
            r = bool(allow_reverse and d_last[b] < d_first[b])
            rev.append(r)
            rot.append(0)
            cur = (firsts[b] if r else lasts[b]).copy()

    seq: list[np.ndarray] = []
    for b, r, k in zip(order, rev, rot):
        p = P[b]
        if closed_rot[b] and k:
            p = _rotate_ring(p, k)
        elif r:
            p = p[::-1]
        seq.append(p)

    remaining = time_limit - (time.perf_counter() - t0)
    can_2opt = allow_reverse or bool(np.all(closed_rot))
    if remaining > 0 and n > 2 and can_2opt:
        entry = np.array([p[0, :2] for p in seq])
        exit_ = np.array([p[-1, :2] for p in seq])
        res = _two_opt(entry, exit_, s, time.perf_counter() + remaining)
        new: list[np.ndarray] = []
        for i, f in res.tolist():
            p = seq[i]
            ring = len(p) > 2 and p[0, 0] == p[-1, 0] and p[0, 1] == p[-1, 1]
            # anel fechado: entrada == saída, então não é preciso invertê-lo (e inverter
            # trocaria o sentido de giro, climb ↔ convencional)
            new.append(p[::-1] if f and not ring else p)
        seq = new
        if rotate_closed:
            # re-escolhe a entrada dos anéis conforme a nova vizinhança
            prev = s
            out: list[np.ndarray] = []
            for p in seq:
                if len(p) > 2 and p[0, 0] == p[-1, 0] and p[0, 1] == p[-1, 1]:
                    body = p[:-1, :2]
                    k = int(np.argmin(np.hypot(body[:, 0] - prev[0], body[:, 1] - prev[1])))
                    p = _rotate_ring(p, k)
                out.append(p)
                prev = p[-1, :2]
            seq = out
    return seq


def optimize_job(job: Job, start: Point = (0.0, 0.0), time_limit: float = 0.3,
                 allow_reverse: bool = True) -> Job:
    """Reordena no lugar os caminhos/furos de todas as operações de ``job``.

    O ponto de partida de cada operação é o fim da anterior.
    """
    cur = start
    for op in job.ops:
        if op.kind == "drill" and len(op.points):
            op.points = optimize_points(op.points, cur, time_limit)
            cur = tuple(op.points[-1, :2])
        if op.paths:
            op.paths = optimize_paths(op.paths, cur, allow_reverse=allow_reverse, time_limit=time_limit)
            cur = tuple(op.paths[-1][-1, :2])
    return job
