"""Conversão rápida de geometria (shapely / numpy) para ``QPainterPath``.

A construção é vetorizada: as coordenadas são extraídas com ``shapely.get_rings`` /
``shapely.get_coordinates`` e serializadas num buffer binário no formato que o
``QDataStream`` usa para ``QPainterPath`` (contagem, [tipo, x, y]*, cStart,
fillRule). O Qt então lê o caminho inteiro de uma vez, sem nenhum laço Python por
vértice — 20.000 polígonos convertem em poucas dezenas de milissegundos.
"""
from __future__ import annotations

from typing import Iterable, Sequence

import numpy as np
import shapely
from PySide6.QtCore import QByteArray, QDataStream, QIODevice, Qt
from PySide6.QtGui import QPainterPath

_REC = np.dtype([("t", ">i4"), ("x", ">f8"), ("y", ">f8")])
_MOVE, _LINE = 0, 1


def _path_from_runs(coords: np.ndarray, starts: np.ndarray,
                    fill: Qt.FillRule = Qt.FillRule.OddEvenFill) -> QPainterPath:
    """Cria um caminho a partir de ``coords`` (N,2) e índices onde começa cada subpath."""
    n = len(coords)
    if n == 0:
        return QPainterPath()
    rec = np.empty(n, dtype=_REC)
    t = np.full(n, _LINE, dtype=np.int32)
    t[starts] = _MOVE
    rec["t"] = t
    rec["x"] = coords[:, 0]
    rec["y"] = coords[:, 1]
    fill_code = 0 if fill == Qt.FillRule.OddEvenFill else 1
    buf = (np.array([n], ">i4").tobytes() + rec.tobytes()
           + np.array([0, fill_code], ">i4").tobytes())
    ba = QByteArray(buf)
    ds = QDataStream(ba, QIODevice.OpenModeFlag.ReadOnly)
    path = QPainterPath()
    ds >> path
    return path


def _flatten_parts(geom) -> np.ndarray:  # type: ignore[no-untyped-def]
    """Lista plana de geometrias simples (desmonta multi e coleções aninhadas)."""
    if geom is None:
        return np.empty(0, dtype=object)
    arr = np.asarray([geom], dtype=object) if not isinstance(geom, np.ndarray) else geom
    for _ in range(6):
        types = shapely.get_type_id(arr)
        multi = types >= 4  # Multi* (4,5,6) e GeometryCollection (7)
        if not multi.any():
            break
        arr = np.concatenate([arr[~multi], shapely.get_parts(arr[multi])])
    return arr[~shapely.is_empty(arr)] if len(arr) else arr


def geometry_to_path(geom, *, point_radius: float = 0.1) -> QPainterPath:  # type: ignore[no-untyped-def]
    """Converte qualquer geometria shapely num único ``QPainterPath`` (OddEven).

    Polígonos viram anéis (furos funcionam pela regra par-ímpar), linhas viram
    polilinhas abertas e pontos viram pequenos círculos de raio ``point_radius``.
    """
    parts = _flatten_parts(geom)
    if len(parts) == 0:
        return QPainterPath()
    types = shapely.get_type_id(parts)
    chunks: list[np.ndarray] = []
    starts: list[np.ndarray] = []
    offset = 0

    polys = parts[types == 3]
    if len(polys):
        rings = shapely.get_rings(polys)
        coords, idx = shapely.get_coordinates(rings, return_index=True)
        if len(coords):
            st = np.flatnonzero(np.r_[True, idx[1:] != idx[:-1]])
            chunks.append(coords)
            starts.append(st + offset)
            offset += len(coords)

    lines = parts[(types == 1) | (types == 2)]
    if len(lines):
        coords, idx = shapely.get_coordinates(lines, return_index=True)
        if len(coords):
            st = np.flatnonzero(np.r_[True, idx[1:] != idx[:-1]])
            chunks.append(coords)
            starts.append(st + offset)
            offset += len(coords)

    pts = parts[types == 0]
    if len(pts):
        centers = shapely.get_coordinates(pts)
        c, s = circles_coords(centers, np.full(len(centers), point_radius))
        chunks.append(c)
        starts.append(s + offset)
        offset += len(c)

    if not chunks:
        return QPainterPath()
    return _path_from_runs(np.vstack(chunks), np.concatenate(starts))


def circles_coords(centers: np.ndarray, radii: np.ndarray, segments: int = 24
                   ) -> tuple[np.ndarray, np.ndarray]:
    """Coordenadas de círculos poligonais (vetorizado). Retorna (coords, starts)."""
    centers = np.asarray(centers, dtype=float).reshape(-1, 2)
    radii = np.broadcast_to(np.asarray(radii, dtype=float), (len(centers),))
    if len(centers) == 0:
        return np.zeros((0, 2)), np.zeros(0, dtype=np.int64)
    ang = np.linspace(0.0, 2 * np.pi, segments + 1)
    unit = np.stack([np.cos(ang), np.sin(ang)], axis=1)                 # (S+1, 2)
    pts = centers[:, None, :] + radii[:, None, None] * unit[None, :, :]  # (N, S+1, 2)
    coords = pts.reshape(-1, 2)
    starts = np.arange(len(centers), dtype=np.int64) * (segments + 1)
    return coords, starts


def circles_to_path(centers: np.ndarray, radii: np.ndarray | float, segments: int = 24) -> QPainterPath:
    coords, starts = circles_coords(centers, np.asarray(radii, dtype=float), segments)
    return _path_from_runs(coords, starts, Qt.FillRule.WindingFill)


def crosses_to_path(centers: np.ndarray, size: float) -> QPainterPath:
    """Pequenas cruzes (marcadores de centro de furo)."""
    c = np.asarray(centers, dtype=float).reshape(-1, 2)
    if len(c) == 0:
        return QPainterPath()
    h = size / 2.0
    seg = np.empty((len(c), 4, 2))
    seg[:, 0] = c + (-h, 0)
    seg[:, 1] = c + (h, 0)
    seg[:, 2] = c + (0, -h)
    seg[:, 3] = c + (0, h)
    coords = seg.reshape(-1, 2)
    starts = np.arange(0, len(coords), 2)
    return _path_from_runs(coords, starts)


def polylines_to_path(lines: Sequence[np.ndarray] | Iterable[np.ndarray]) -> QPainterPath:
    """Lista de polilinhas (N_i, 2+) -> um único caminho aberto."""
    arrs = [np.asarray(a, dtype=float)[:, :2] for a in lines if len(a) >= 2]
    if not arrs:
        return QPainterPath()
    lens = np.fromiter((len(a) for a in arrs), dtype=np.int64, count=len(arrs))
    starts = np.r_[0, np.cumsum(lens)[:-1]]
    return _path_from_runs(np.vstack(arrs), starts)


def segments_to_path(segs: np.ndarray) -> QPainterPath:
    """Segmentos (N, 2, 2|3) -> caminho, encadeando segmentos contíguos em polilinhas."""
    segs = np.asarray(segs, dtype=float)
    if segs.size == 0:
        return QPainterPath()
    a = segs[:, 0, :2]
    b = segs[:, 1, :2]
    brk = np.r_[True, np.any(np.abs(a[1:] - b[:-1]) > 1e-9, axis=1)]
    # cada segmento contribui com seu ponto final; inícios de corrente contribuem também com o inicial
    n_start = int(brk.sum())
    total = len(segs) + n_start
    coords = np.empty((total, 2))
    # posição do ponto final de cada segmento no buffer
    end_pos = np.arange(len(segs)) + np.cumsum(brk)
    coords[end_pos] = b
    start_pos = end_pos[brk] - 1
    coords[start_pos] = a[brk]
    return _path_from_runs(coords, start_pos)
