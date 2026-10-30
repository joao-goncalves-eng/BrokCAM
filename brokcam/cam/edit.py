"""Edição manual de caminhos de ferramenta.

Permite ao usuário remover cortes inúteis depois da geração: caminhos inteiros, furos
individuais ou apenas o trecho que cai dentro de uma área retangular. Funções puras
(sem Qt): recebem a lista de ``PathOp`` e devolvem uma **nova** lista, sem alterar a
original — o que torna o desfazer trivial na interface.

Uma referência de item é a tupla ``(op_index, kind, index)`` com ``kind`` igual a
``"path"`` (polilinha em ``op.paths``) ou ``"point"`` (furo em ``op.points``).
"""
from __future__ import annotations

import copy
from dataclasses import dataclass
from typing import Iterable

import numpy as np
import shapely
from shapely.geometry import box

from brokcam.core.model import Job, PathOp

Ref = tuple[int, str, int]


# --------------------------------------------------------------------------- índice

@dataclass
class SegmentIndex:
    """Todos os segmentos e furos de um job em arrays planos, para busca rápida."""
    a: np.ndarray            # (S,2) início dos segmentos
    b: np.ndarray            # (S,2) fim dos segmentos
    seg_ref: np.ndarray      # (S,2) int -> (op_index, path_index)
    pts: np.ndarray          # (P,2) furos
    pt_ref: np.ndarray       # (P,2) int -> (op_index, point_index)
    pt_radius: np.ndarray    # (P,) raio da ferramenta do furo

    @classmethod
    def build(cls, ops: list[PathOp]) -> "SegmentIndex":
        a, b, sr, pts, pr, rad = [], [], [], [], [], []
        for oi, op in enumerate(ops):
            for pi, p in enumerate(op.paths):
                arr = np.asarray(p, dtype=float)[:, :2]
                if len(arr) == 1:
                    arr = np.vstack([arr, arr])
                if len(arr) < 2:
                    continue
                a.append(arr[:-1])
                b.append(arr[1:])
                sr.append(np.tile((oi, pi), (len(arr) - 1, 1)))
            p = np.asarray(op.points, dtype=float).reshape(-1, 2)
            if len(p):
                pts.append(p)
                pr.append(np.column_stack([np.full(len(p), oi), np.arange(len(p))]))
                rad.append(np.full(len(p), max(op.tool.diameter, 0.05) / 2.0))
        z2 = np.zeros((0, 2))
        zi = np.zeros((0, 2), dtype=int)
        return cls(
            a=np.vstack(a) if a else z2, b=np.vstack(b) if b else z2,
            seg_ref=np.vstack(sr).astype(int) if sr else zi,
            pts=np.vstack(pts) if pts else z2, pt_ref=np.vstack(pr).astype(int) if pr else zi,
            pt_radius=np.concatenate(rad) if rad else np.zeros(0),
        )

    def segment_distances(self, x: float, y: float) -> np.ndarray:
        if not len(self.a):
            return np.zeros(0)
        p = np.array([x, y])
        d = self.b - self.a
        L2 = np.einsum("ij,ij->i", d, d)
        t = np.where(L2 > 0, np.einsum("ij,ij->i", p - self.a, d) / np.where(L2 > 0, L2, 1), 0.0)
        t = np.clip(t, 0.0, 1.0)
        q = self.a + d * t[:, None]
        return np.hypot(q[:, 0] - x, q[:, 1] - y)


def pick(ops: list[PathOp], x: float, y: float, tol: float,
         index: SegmentIndex | None = None) -> Ref | None:
    """Item mais próximo de (x, y) dentro da tolerância ``tol`` (mm), ou ``None``.

    Furos têm prioridade quando o clique cai dentro do círculo da broca.
    """
    idx = index or SegmentIndex.build(ops)
    best: tuple[float, Ref] | None = None
    if len(idx.pts):
        dp = np.hypot(idx.pts[:, 0] - x, idx.pts[:, 1] - y) - idx.pt_radius
        i = int(np.argmin(dp))
        if dp[i] <= tol:
            oi, pi = idx.pt_ref[i]
            best = (min(float(dp[i]), 0.0), (int(oi), "point", int(pi)))
    ds = idx.segment_distances(x, y)
    if len(ds):
        i = int(np.argmin(ds))
        if ds[i] <= tol and (best is None or ds[i] < best[0]):
            oi, pi = idx.seg_ref[i]
            best = (float(ds[i]), (int(oi), "path", int(pi)))
    return None if best is None else best[1]


def select_rect(ops: list[PathOp], x0: float, y0: float, x1: float, y1: float,
                mode: str = "touch") -> set[Ref]:
    """Itens dentro de um retângulo.

    ``mode="inside"``: só caminhos totalmente contidos (seleção da esquerda para a direita).
    ``mode="touch"``: qualquer caminho que cruze ou toque o retângulo.
    """
    xa, xb = sorted((x0, x1))
    ya, yb = sorted((y0, y1))
    rect = box(xa, ya, xb, yb)
    out: set[Ref] = set()
    for oi, op in enumerate(ops):
        if op.paths:
            lines = [np.asarray(p, dtype=float)[:, :2] for p in op.paths]
            geoms = np.array([shapely.linestrings(l) if len(l) >= 2 else shapely.points(l[0])
                              if len(l) else shapely.Point() for l in lines], dtype=object)
            hit = shapely.contains(rect, geoms) if mode == "inside" else shapely.intersects(rect, geoms)
            if mode == "inside":
                hit |= shapely.covers(rect, geoms)
            out.update((oi, "path", int(i)) for i in np.flatnonzero(hit))
        pts = np.asarray(op.points, dtype=float).reshape(-1, 2)
        if len(pts):
            m = (pts[:, 0] >= xa) & (pts[:, 0] <= xb) & (pts[:, 1] >= ya) & (pts[:, 1] <= yb)
            out.update((oi, "point", int(i)) for i in np.flatnonzero(m))
    return out


# --------------------------------------------------------------------------- edição

def _copy_ops(ops: list[PathOp]) -> list[PathOp]:
    return [copy.copy(op) for op in ops]


def delete(ops: list[PathOp], refs: Iterable[Ref], drop_empty: bool = True) -> list[PathOp]:
    """Remove os itens ``refs``. Operações que ficam vazias são descartadas."""
    by_op: dict[int, dict[str, set[int]]] = {}
    for oi, kind, i in refs:
        by_op.setdefault(oi, {"path": set(), "point": set()})[kind].add(i)
    out: list[PathOp] = []
    for oi, op in enumerate(_copy_ops(ops)):
        rm = by_op.get(oi)
        if rm:
            op.paths = [p for i, p in enumerate(op.paths) if i not in rm["path"]]
            pts = np.asarray(op.points, dtype=float).reshape(-1, 2)
            if rm["point"] and len(pts):
                keep = np.ones(len(pts), dtype=bool)
                keep[list(rm["point"])] = False
                op.points = pts[keep]
        if drop_empty and not op.paths and not len(op.points):
            continue
        out.append(op)
    return out


def keep_only(ops: list[PathOp], refs: Iterable[Ref]) -> list[PathOp]:
    """Remove tudo exceto ``refs`` (inverso de :func:`delete`)."""
    keep = set(refs)
    return delete(ops, set(all_refs(ops)) - keep)


def all_refs(ops: list[PathOp]) -> list[Ref]:
    refs: list[Ref] = []
    for oi, op in enumerate(ops):
        refs += [(oi, "path", i) for i in range(len(op.paths))]
        refs += [(oi, "point", i) for i in range(len(np.asarray(op.points).reshape(-1, 2)))]
    return refs


def _is_closed(a: np.ndarray) -> bool:
    return len(a) > 2 and bool(np.allclose(a[0], a[-1]))


def _pieces(g: shapely.Geometry) -> list[np.ndarray]:
    if g.is_empty:
        return []
    # Sem line_merge: ele poderia inverter o sentido de pedaços (climb ↔ convencional).
    parts = shapely.get_parts(g) if g.geom_type.startswith(("Multi", "Geometry")) else [g]
    out = []
    for p in parts:
        if p.geom_type == "LineString" and not p.is_empty:
            c = shapely.get_coordinates(p)
            if len(c) >= 2:
                out.append(c)
    return out


def trim_rect(ops: list[PathOp], x0: float, y0: float, x1: float, y1: float,
              min_length: float = 0.01) -> list[PathOp]:
    """Apaga o trecho dos caminhos (e os furos) que cai dentro do retângulo.

    Caminhos cortados viram polilinhas abertas; um anel fechado cortado uma vez vira uma
    única polilinha que começa e termina na borda do retângulo, preservando o sentido.
    Pedaços menores que ``min_length`` (mm) são descartados.
    """
    xa, xb = sorted((x0, x1))
    ya, yb = sorted((y0, y1))
    rect = box(xa, ya, xb, yb)
    out: list[PathOp] = []
    for op in _copy_ops(ops):
        new_paths: list[np.ndarray] = []
        for p in op.paths:
            a = np.asarray(p, dtype=float)[:, :2]
            if len(a) < 2:
                continue
            ls = shapely.linestrings(a)
            if not ls.intersects(rect):
                new_paths.append(np.asarray(p, dtype=float))
                continue
            if rect.covers(ls):
                continue
            closed = _is_closed(a)
            if closed:
                # Rotaciona o anel para começar num vértice fora do retângulo: assim a
                # diferença produz no máximo um pedaço por "saída" e não quebra na costura.
                inside = (a[:-1, 0] >= xa) & (a[:-1, 0] <= xb) & (a[:-1, 1] >= ya) & (a[:-1, 1] <= yb)
                outside = np.flatnonzero(~inside)
                if len(outside):
                    k = int(outside[0])
                    a = np.vstack([a[k:-1], a[:k + 1]])
                    ls = shapely.linestrings(a)
            rest = ls.difference(rect)
            pieces = _pieces(rest)
            if closed and len(pieces) >= 2 and np.allclose(pieces[0][0], a[0]) \
                    and np.allclose(pieces[-1][-1], a[-1]):
                pieces = [np.vstack([pieces[-1], pieces[0][1:]])] + pieces[1:-1]
            # garante a ordem de percurso original (difference pode reordenar partes)
            if len(pieces) > 1:
                proj = [ls.project(shapely.points(pc[0])) for pc in pieces]
                pieces = [pc for _, pc in sorted(zip(proj, pieces), key=lambda t: t[0])]
            for pc in pieces:
                if float(np.hypot(*np.diff(pc, axis=0).T).sum()) >= min_length:
                    new_paths.append(pc)
        op.paths = new_paths
        pts = np.asarray(op.points, dtype=float).reshape(-1, 2)
        if len(pts):
            m = (pts[:, 0] >= xa) & (pts[:, 0] <= xb) & (pts[:, 1] >= ya) & (pts[:, 1] <= yb)
            op.points = pts[~m]
        if op.paths or len(op.points):
            out.append(op)
    return out


def reverse(ops: list[PathOp], refs: Iterable[Ref]) -> list[PathOp]:
    """Inverte o sentido dos caminhos selecionados (troca concordante ↔ convencional)."""
    sel: dict[int, set[int]] = {}
    for oi, kind, i in refs:
        if kind == "path":
            sel.setdefault(oi, set()).add(i)
    out = _copy_ops(ops)
    for oi, idxs in sel.items():
        op = out[oi]
        op.paths = [np.asarray(p)[::-1].copy() if i in idxs else p for i, p in enumerate(op.paths)]
    return out


def reoptimize(ops: list[PathOp], start: tuple[float, float] = (0.0, 0.0),
               time_limit: float = 0.3) -> list[PathOp]:
    """Reordena cortes e furos após a edição para minimizar o deslocamento em vazio."""
    from .optimize import optimize_job
    job = Job(name="tmp", source_id=0, ops=_copy_ops(ops))
    # Sem inverter: trechos de anéis cortados viram caminhos abertos, e invertê-los
    # trocaria o sentido de fresamento (concordante ↔ convencional).
    optimize_job(job, start, time_limit, False)
    return job.ops


def count(ops: list[PathOp]) -> dict[str, int]:
    return {"paths": sum(len(op.paths) for op in ops),
            "points": sum(len(np.asarray(op.points).reshape(-1, 2)) for op in ops)}
