"""Modelo do projeto aberto na interface: camadas, transformações, jobs e arquivo ``.bcam``.

O projeto guarda as camadas *originais* (como lidas do arquivo) e as transformações
aplicadas (espelhar, deslocar). A camada efetiva (usada no canvas e no CAM) é
recalculada a partir delas, o que torna desfazer/refazer trivial e permite salvar
o projeto apenas com caminhos dos arquivos + transformações + parâmetros.
"""
from __future__ import annotations

import dataclasses
import json
import logging
import os
from dataclasses import dataclass, field
from typing import Any, Iterable

import numpy as np
import shapely
from shapely import affinity
from PySide6.QtCore import QObject, Signal

from brokcam.core.model import DrillLayer, GeometryLayer, GerberLayer, Job

from .theme import ROLE_COLORS

log = logging.getLogger("brokcam.ui.project")

PROJECT_FORMAT = "brokcam-project"
LEGACY_FORMATS = {"tracecam-project"}       # projetos salvos antes do programa se chamar BrokCAM
PROJECT_VERSION = 1

Layer = GerberLayer | DrillLayer | GeometryLayer


# =========================================================================== transformações

@dataclass
class Transform:
    """Transformação rígida de uma camada: espelhar → girar (em torno da origem) → deslocar.

    Girar em torno da origem (e não do centro da placa) torna a composição de
    alinhamentos sucessivos exata: ``R2·(R1·p + o1) + d = (R2·R1)·p + (R2·o1 + d)``.
    """
    mirror: str | None = None                    # None | "x" (espelha em X, eixo vertical) | "y"
    center: tuple[float, float] | None = None    # centro do espelhamento
    offset: tuple[float, float] = (0.0, 0.0)     # deslocamento final
    rotation: float = 0.0                        # graus, anti-horário, em torno de (0, 0)

    def is_identity(self) -> bool:
        return self.mirror is None and self.offset == (0.0, 0.0) and not self.rotation

    def to_json(self) -> dict[str, Any]:
        return {"mirror": self.mirror, "center": list(self.center) if self.center else None,
                "offset": list(self.offset), "rotation": self.rotation}

    @classmethod
    def from_json(cls, d: dict[str, Any] | None) -> "Transform":
        d = d or {}
        c = d.get("center")
        off = d.get("offset") or (0.0, 0.0)
        return cls(d.get("mirror"), (float(c[0]), float(c[1])) if c else None,
                   (float(off[0]), float(off[1])), float(d.get("rotation", 0.0) or 0.0))

    # ------------------------------------------------------------------ forma matricial
    def matrix(self) -> tuple[np.ndarray, np.ndarray]:
        """Afim ``p' = A·p + t`` equivalente (A ortogonal 2×2)."""
        A = np.eye(2)
        t = np.zeros(2)
        cx, cy = self.center or (0.0, 0.0)
        if self.mirror == "x":
            A = np.diag([-1.0, 1.0])
            t = np.array([2 * cx, 0.0])
        elif self.mirror == "y":
            A = np.diag([1.0, -1.0])
            t = np.array([0.0, 2 * cy])
        R = rotate_xy(np.eye(2).T, self.rotation).T          # colunas = R·e1, R·e2
        return R @ A, R @ t + np.asarray(self.offset, dtype=float)

    @classmethod
    def from_matrix(cls, A: np.ndarray, t: np.ndarray) -> "Transform":
        """Forma canônica: espelho em X (opcional, em torno da origem) → giro → deslocamento."""
        det = float(np.linalg.det(A))
        if det < 0:
            B = A @ np.diag([-1.0, 1.0])                         # A = R·S  →  R = A·S
            mirror: str | None = "x"
        else:
            B = A
            mirror = None
        rot = float(np.degrees(np.arctan2(B[1, 0], B[0, 0]))) % 360.0
        q = round(rot / 90.0)
        if abs(rot - 90.0 * q) < 1e-7:
            rot = (90.0 * q) % 360.0
        if abs(rot) < 1e-9 or abs(rot - 360.0) < 1e-9:
            rot = 0.0
        return cls(mirror, (0.0, 0.0) if mirror else None, (float(t[0]), float(t[1])), rot)

    def compose(self, A2: np.ndarray, t2: np.ndarray) -> "Transform":
        """Aplica ``p'' = A2·p' + t2`` depois desta transformação."""
        A, t = self.matrix()
        return Transform.from_matrix(A2 @ A, A2 @ t + t2)

    def then(self, rotation: float, dx: float, dy: float) -> "Transform":
        """Compõe esta transformação com um giro (em torno da origem) seguido de translação."""
        ox, oy = rotate_xy(np.array([self.offset], dtype=float), rotation)[0]
        rot = (self.rotation + rotation) % 360.0
        return dataclasses.replace(self, rotation=0.0 if abs(rot) < 1e-9 or abs(rot - 360) < 1e-9 else rot,
                                   offset=(float(ox + dx), float(oy + dy)))


def rotate_xy(a: np.ndarray, deg: float) -> np.ndarray:
    """Gira pontos (..., 2) em torno da origem. Múltiplos de 90° são exatos."""
    a = np.array(a, dtype=float, copy=True)
    if not deg or a.size == 0:
        return a
    q = round(deg / 90.0)
    if abs(deg - 90.0 * q) < 1e-9:
        c, s = [(1, 0), (0, 1), (-1, 0), (0, -1)][q % 4]
    else:
        r = np.radians(deg)
        c, s = float(np.cos(r)), float(np.sin(r))
    x = a[..., 0].copy()
    a[..., 0] = c * x - s * a[..., 1]
    a[..., 1] = s * x + c * a[..., 1]
    return a


def _xform_points(pts: np.ndarray, t: Transform) -> np.ndarray:
    a = np.array(pts, dtype=float, copy=True)
    if a.size == 0:
        return a
    cx, cy = t.center or (0.0, 0.0)
    if t.mirror == "x":
        a[..., 0] = 2 * cx - a[..., 0]
    elif t.mirror == "y":
        a[..., 1] = 2 * cy - a[..., 1]
    if t.rotation:
        a = rotate_xy(a, t.rotation)
    a[..., 0] += t.offset[0]
    a[..., 1] += t.offset[1]
    return a


def _xform_geom(g: Any, t: Transform) -> Any:
    if g is None or g.is_empty:
        return g
    cx, cy = t.center or (0.0, 0.0)
    if t.mirror == "x":
        g = affinity.scale(g, -1.0, 1.0, origin=(cx, cy))
    elif t.mirror == "y":
        g = affinity.scale(g, 1.0, -1.0, origin=(cx, cy))
    if t.rotation:
        g = shapely.transform(g, lambda c: rotate_xy(c, t.rotation))
    if t.offset != (0.0, 0.0):
        g = affinity.translate(g, t.offset[0], t.offset[1])
    if t.mirror:
        g = shapely.normalize(g) if hasattr(shapely, "normalize") else g
    return g


def apply_transform(layer: Layer, t: Transform) -> Layer:
    """Cópia transformada da camada (mesmo ``id``)."""
    if t.is_identity():
        return layer
    if isinstance(layer, GerberLayer):
        return dataclasses.replace(
            layer, copper=_xform_geom(layer.copper, t),
            traces=[_xform_points(tr, t) for tr in layer.traces])
    if isinstance(layer, DrillLayer):
        tools = {}
        for n, tool in layer.tools.items():
            slots = np.asarray(tool.slots, dtype=float).reshape(-1, 4)
            if len(slots):
                slots = _xform_points(slots.reshape(-1, 2, 2), t).reshape(-1, 4)
            tools[n] = dataclasses.replace(
                tool, points=_xform_points(np.asarray(tool.points, dtype=float).reshape(-1, 2), t),
                slots=slots)
        return dataclasses.replace(layer, tools=tools)
    if isinstance(layer, GeometryLayer):
        return dataclasses.replace(layer, geometry=_xform_geom(layer.geometry, t))
    return layer


# =========================================================================== entradas

@dataclass(eq=False)
class LayerEntry:
    layer: Layer                        # original, como lido
    role: str = "unknown"
    color: str = ""
    visible: bool = True
    transform: Transform = field(default_factory=Transform)
    _effective: Layer | None = field(default=None, repr=False)
    group: int | None = None            # id do grupo (camadas que se movem juntas) ou None

    def __post_init__(self) -> None:
        if not self.color:
            self.color = ROLE_COLORS.get(self.role, ROLE_COLORS["unknown"])

    @property
    def id(self) -> int:
        return self.layer.id

    @property
    def name(self) -> str:
        return self.layer.name

    @property
    def kind(self) -> str:
        return self.layer.kind

    @property
    def effective(self) -> Layer:
        if self._effective is None:
            self._effective = apply_transform(self.layer, self.transform)
        return self._effective

    def invalidate(self) -> None:
        self._effective = None

    def summary(self) -> str:
        lay = self.effective
        if isinstance(lay, DrillLayer):
            holes = sum(len(t.points) for t in lay.tools.values())
            slots = sum(len(np.asarray(t.slots).reshape(-1, 4)) for t in lay.tools.values())
            s = f"{len(lay.tools)} ferramentas · {holes} furos"
            return s + (f" · {slots} rasgos" if slots else "")
        if isinstance(lay, GerberLayer):
            n = len(shapely.get_parts(lay.copper)) if not lay.copper.is_empty else 0
            return f"{n} regiões de cobre"
        if isinstance(lay, GeometryLayer):
            n = len(shapely.get_parts(lay.geometry)) if not lay.geometry.is_empty else 0
            return f"{n} elementos"
        return ""


@dataclass(eq=False)
class JobEntry:
    job: Job
    source_id: int
    visible: bool = True
    color: str = ""
    estimate_s: float = 0.0

    @property
    def id(self) -> int:
        return self.job.id

    @property
    def name(self) -> str:
        return self.job.name


# =========================================================================== projeto

class Project(QObject):
    layerAdded = Signal(object)        # LayerEntry
    layerRemoved = Signal(int)
    layerChanged = Signal(object)      # LayerEntry (visibilidade/cor/papel)
    layerGeometryChanged = Signal(object)  # LayerEntry (transformação)
    jobAdded = Signal(object)          # JobEntry
    jobRemoved = Signal(int)
    jobChanged = Signal(object)
    jobGeometryChanged = Signal(object)   # JobEntry (caminhos editados/movidos)
    cleared = Signal()
    groupsChanged = Signal()
    modifiedChanged = Signal(bool)
    pathChanged = Signal(str)

    def __init__(self, parent: QObject | None = None) -> None:
        super().__init__(parent)
        self.layers: list[LayerEntry] = []
        self.jobs: list[JobEntry] = []
        self.path: str = ""
        self._modified = False
        self.post_settings: dict[str, Any] = {}
        self.groups: dict[int, str] = {}     # id → nome ("Placa 1")
        self._next_group = 1

    # ------------------------------------------------------------------ grupos
    def new_group_id(self) -> int:
        gid = self._next_group
        self._next_group += 1
        return gid

    def group_name(self, gid: int | None) -> str:
        return self.groups.get(gid, "") if gid is not None else ""

    def suggest_group_name(self) -> str:
        used = set(self.groups.values())
        n = 1
        while f"Placa {n}" in used:
            n += 1
        return f"Placa {n}"

    def group_members(self, layer_id: int) -> list[int]:
        """Camadas que se movem junto com ``layer_id`` (ela mesma, se não estiver em grupo)."""
        e = self.layer(layer_id)
        if e is None or e.group is None:
            return [layer_id]
        return [x.id for x in self.layers if x.group == e.group]

    def set_layer_groups(self, assignment: dict[int, int | None], names: dict[int, str] | None = None) -> None:
        """Define o grupo de cada camada (``None`` = sem grupo) e remove grupos vazios."""
        if names:
            self.groups.update(names)
            self._next_group = max([self._next_group] + [g + 1 for g in names])
        for lid, gid in assignment.items():
            e = self.layer(lid)
            if e is not None and e.group != gid:
                e.group = gid
                self.layerChanged.emit(e)
        used = {e.group for e in self.layers if e.group is not None}
        for gid in [g for g in self.groups if g not in used]:
            del self.groups[gid]
        self.groupsChanged.emit()
        self.set_modified()

    def rename_group(self, gid: int, name: str) -> None:
        if gid in self.groups:
            self.groups[gid] = name
            for e in self.layers:
                if e.group == gid:
                    self.layerChanged.emit(e)
            self.groupsChanged.emit()
            self.set_modified()

    # ------------------------------------------------------------------ estado
    @property
    def modified(self) -> bool:
        return self._modified

    def set_modified(self, on: bool = True) -> None:
        if on != self._modified:
            self._modified = on
            self.modifiedChanged.emit(on)

    def set_path(self, path: str) -> None:
        self.path = path
        self.pathChanged.emit(path)

    def is_empty(self) -> bool:
        return not self.layers and not self.jobs

    # ------------------------------------------------------------------ camadas
    def layer(self, layer_id: int) -> LayerEntry | None:
        return next((e for e in self.layers if e.id == layer_id), None)

    def add_layer(self, entry: LayerEntry, index: int | None = None) -> LayerEntry:
        if index is None or index >= len(self.layers):
            self.layers.append(entry)
        else:
            self.layers.insert(max(0, index), entry)
        self.layerAdded.emit(entry)
        self.set_modified()
        return entry

    def remove_layer(self, layer_id: int) -> tuple[int, LayerEntry] | None:
        for i, e in enumerate(self.layers):
            if e.id == layer_id:
                del self.layers[i]
                self.layerRemoved.emit(layer_id)
                self.set_modified()
                return i, e
        return None

    def set_layer_visible(self, layer_id: int, visible: bool) -> None:
        e = self.layer(layer_id)
        if e and e.visible != visible:
            e.visible = visible
            self.layerChanged.emit(e)

    def set_layer_color(self, layer_id: int, color: str) -> None:
        e = self.layer(layer_id)
        if e:
            e.color = color
            self.layerChanged.emit(e)
            self.set_modified()

    def set_layer_role(self, layer_id: int, role: str) -> None:
        e = self.layer(layer_id)
        if e and e.role != role:
            old_default = ROLE_COLORS.get(e.role)
            e.role = role
            if e.color == old_default:
                e.color = ROLE_COLORS.get(role, e.color)
            self.layerChanged.emit(e)
            self.set_modified()

    def set_layer_transform(self, layer_id: int, t: Transform) -> None:
        """Troca a transformação de uma camada. Os caminhos gerados a partir dela acompanham
        (mesmo movimento relativo, como em :meth:`transform_layers`) — assim ficam iguais aos
        regenerados ao reabrir o ``.bcam``."""
        e = self.layer(layer_id)
        if e:
            A_old, t_old = e.transform.matrix()
            A_new, t_new = t.matrix()
            e.transform = t
            e.invalidate()
            self.layerGeometryChanged.emit(e)
            D = A_new @ np.linalg.inv(A_old)
            d = t_new - D @ t_old
            if not (np.allclose(D, np.eye(2), atol=1e-12) and np.allclose(d, 0.0, atol=1e-12)):
                self._transform_jobs({layer_id}, D, d)
            self.set_modified()

    def layers_by_role(self, role: str) -> list[LayerEntry]:
        return [e for e in self.layers if e.role == role]

    # ------------------------------------------------------------------ jobs
    def job(self, job_id: int) -> JobEntry | None:
        return next((j for j in self.jobs if j.id == job_id), None)

    def add_job(self, entry: JobEntry, index: int | None = None) -> JobEntry:
        if index is None or index >= len(self.jobs):
            self.jobs.append(entry)
        else:
            self.jobs.insert(max(0, index), entry)
        self.jobAdded.emit(entry)
        self.set_modified()
        return entry

    def remove_job(self, job_id: int) -> tuple[int, JobEntry] | None:
        for i, j in enumerate(self.jobs):
            if j.id == job_id:
                del self.jobs[i]
                self.jobRemoved.emit(job_id)
                self.set_modified()
                return i, j
        return None

    def set_job_visible(self, job_id: int, visible: bool) -> None:
        j = self.job(job_id)
        if j and j.visible != visible:
            j.visible = visible
            self.jobChanged.emit(j)

    def set_job_ops(self, job_id: int, ops: list, gcode: str = "", edited: bool | None = None) -> None:
        """Substitui os caminhos de um job (edição manual ou alinhamento)."""
        j = self.job(job_id)
        if j is None:
            return
        j.job.ops = list(ops)
        j.job.gcode = gcode
        if edited is not None:
            if edited:
                j.job.settings["edited"] = True
            else:
                j.job.settings.pop("edited", None)
        self.jobGeometryChanged.emit(j)
        self.set_modified()

    def move_all(self, rotation: float, dx: float, dy: float) -> None:
        """Move rigidamente TODAS as camadas e caminhos: gira em torno da origem e desloca."""
        for e in self.layers:
            e.transform = e.transform.then(rotation, dx, dy)
            e.invalidate()
            self.layerGeometryChanged.emit(e)
        for j in self.jobs:
            for op in j.job.ops:
                op.paths = [_move(p, rotation, dx, dy) for p in op.paths]
                pts = np.asarray(op.points, dtype=float).reshape(-1, 2)
                op.points = _move(pts, rotation, dx, dy) if len(pts) else pts
            j.job.gcode = ""
            self.jobGeometryChanged.emit(j)
        self.set_modified()

    def flip_layers(self, layer_ids: list[int], axis: str, center: float) -> None:
        """Vira (espelha) as camadas indicadas — e os caminhos gerados a partir delas — em torno
        de uma linha vertical ``x = center`` (``axis="x"``) ou horizontal ``y = center`` (``"y"``).

        É uma involução: aplicar de novo desfaz. Os caminhos espelhados têm o sentido invertido
        para preservar o fresamento concordante/convencional escolhido.
        """
        A2, t2 = flip_matrix(axis, center)
        self.transform_layers(layer_ids, A2, t2)

    def transform_layers(self, layer_ids: list[int], A: np.ndarray, t: np.ndarray) -> None:
        """Aplica ``p' = A·p + t`` (movimento rígido, com ou sem espelho) às camadas e aos
        caminhos gerados a partir delas. Com espelho, os caminhos são invertidos para manter o
        sentido de fresamento."""
        A = np.asarray(A, dtype=float)
        t = np.asarray(t, dtype=float)
        ids = set(layer_ids)
        for e in self.layers:
            if e.id in ids:
                e.transform = e.transform.compose(A, t)
                e.invalidate()
                self.layerGeometryChanged.emit(e)
        self._transform_jobs(ids, A, t)
        self.set_modified()

    def _transform_jobs(self, ids: set[int], A: np.ndarray, t: np.ndarray) -> None:
        """Aplica ``p' = A·p + t`` aos caminhos gerados a partir das camadas ``ids``."""
        mirror = float(np.linalg.det(A)) < 0
        for j in self.jobs:
            if j.source_id not in ids:
                continue
            for op in j.job.ops:
                new = []
                for p in op.paths:
                    q = np.asarray(p, dtype=float)[:, :2] @ A.T + t
                    new.append(q[::-1].copy() if mirror else q)
                op.paths = new
                pts = np.asarray(op.points, dtype=float).reshape(-1, 2)
                op.points = pts @ A.T + t if len(pts) else pts
            j.job.gcode = ""
            self.jobGeometryChanged.emit(j)
        self.set_modified()

    def jobs_for(self, layer_id: int) -> list[JobEntry]:
        return [j for j in self.jobs if j.source_id == layer_id]

    # ------------------------------------------------------------------ geral
    def clear(self) -> None:
        self.layers.clear()
        self.jobs.clear()
        self.groups.clear()
        self._next_group = 1
        self.path = ""
        self.post_settings = {}
        self.cleared.emit()
        self.set_modified(False)

    def bounds(self, only_visible: bool = True) -> tuple[float, float, float, float] | None:
        bs: list[tuple[float, float, float, float]] = []
        for e in self.layers:
            if e.visible or not only_visible:
                b = e.effective.bounds
                if b != (0, 0, 0, 0):
                    bs.append(b)
        for j in self.jobs:
            if j.visible or not only_visible:
                b = job_bounds(j.job)
                if b is not None:
                    bs.append(b)
        if not bs:
            return None
        a = np.asarray(bs, dtype=float)
        return (float(a[:, 0].min()), float(a[:, 1].min()), float(a[:, 2].max()), float(a[:, 3].max()))

    def board_bounds(self, reference: str = "outline") -> tuple[float, float, float, float] | None:
        """Limites da placa: pelo contorno (se houver e ``reference == "outline"``) ou por tudo."""
        src = [e for e in self.layers if e.role == "outline"] if reference == "outline" else []
        if not src:
            src = list(self.layers)
        bs = [e.effective.bounds for e in src if e.effective.bounds != (0, 0, 0, 0)]
        if not bs and reference != "outline":
            return self.bounds(only_visible=False)
        if not bs:
            return self.bounds(only_visible=False)
        a = np.asarray(bs, dtype=float)
        return (float(a[:, 0].min()), float(a[:, 1].min()), float(a[:, 2].max()), float(a[:, 3].max()))

    def board_center(self) -> tuple[float, float]:
        """Centro de referência para espelhar (contorno, se houver; senão todas as camadas)."""
        outline = [e for e in self.layers if e.role == "outline"]
        src = outline or self.layers
        bs = [e.layer.bounds for e in src if e.layer.bounds != (0, 0, 0, 0)]
        if not bs:
            return (0.0, 0.0)
        a = np.asarray(bs, dtype=float)
        return (float((a[:, 0].min() + a[:, 2].max()) / 2), float((a[:, 1].min() + a[:, 3].max()) / 2))

    # ------------------------------------------------------------------ arquivo
    def to_json(self, path: str) -> dict[str, Any]:
        base = os.path.dirname(os.path.abspath(path))
        index = {e.id: i for i, e in enumerate(self.layers)}
        layers = []
        for e in self.layers:
            src = e.layer.source_path
            layers.append({
                "path": os.path.abspath(src) if src else "",
                "relpath": _relpath(src, base) if src else "",
                "name": e.name, "kind": e.kind, "role": e.role, "color": e.color,
                "visible": e.visible, "transform": e.transform.to_json(), "group": e.group,
            })
            if not src:
                from brokcam.core.serialize import layer_to_json
                layers[-1]["inline"] = layer_to_json(e.layer)
        jobs = []
        for j in self.jobs:
            d = {
                "name": j.job.name, "operation": j.job.operation,
                "source": index.get(j.source_id, -1), "settings": _jsonable(j.job.settings),
                "visible": j.visible,
            }
            if j.job.settings.get("edited") or j.job.settings.get("moved"):
                # Caminhos editados à mão não podem ser regenerados: grava os caminhos.
                from brokcam.core.serialize import ops_to_json
                d["ops"] = ops_to_json(j.job.ops)
            jobs.append(d)
        return {"format": PROJECT_FORMAT, "version": PROJECT_VERSION, "layers": layers,
                "jobs": jobs, "post": _jsonable(self.post_settings),
                "groups": {str(k): v for k, v in self.groups.items()}}

    def save(self, path: str) -> None:
        data = self.to_json(path)
        tmp = path + ".tmp"
        with open(tmp, "w", encoding="utf-8") as fh:
            json.dump(data, fh, indent=2, ensure_ascii=False)
        os.replace(tmp, path)
        self.set_path(path)
        self.set_modified(False)


def read_project_file(path: str) -> dict[str, Any]:
    with open(path, encoding="utf-8") as fh:
        data = json.load(fh)
    if not isinstance(data, dict) or data.get("format") not in {PROJECT_FORMAT, *LEGACY_FORMATS}:
        raise ValueError("Arquivo não é um projeto BrokCAM (.bcam ou .tcam).")
    base = os.path.dirname(os.path.abspath(path))
    for lay in data.get("layers", []):
        p = lay.get("path", "")
        rel = lay.get("relpath", "")
        if (not p or not os.path.exists(p)) and rel:
            cand = os.path.normpath(os.path.join(base, rel))
            if os.path.exists(cand):
                lay["path"] = cand
    return data


def _relpath(p: str, base: str) -> str:
    try:
        return os.path.relpath(p, base)
    except ValueError:  # outra unidade no Windows
        return ""


def _jsonable(d: Any) -> Any:
    if isinstance(d, dict):
        return {str(k): _jsonable(v) for k, v in d.items()}
    if isinstance(d, (list, tuple)):
        return [_jsonable(v) for v in d]
    if isinstance(d, (np.floating, np.integer)):
        return d.item()
    if isinstance(d, np.ndarray):
        return d.tolist()
    if isinstance(d, (str, int, float, bool)) or d is None:
        return d
    return str(d)


def rotation_about(deg: float, cx: float, cy: float) -> tuple[np.ndarray, np.ndarray]:
    """Giro de ``deg`` graus (anti-horário) em torno de (cx, cy) como ``p' = A·p + t``."""
    A = rotate_xy(np.eye(2), deg).T
    c = np.array([cx, cy], dtype=float)
    return A, c - A @ c


def invert_affine(A: np.ndarray, t: np.ndarray) -> tuple[np.ndarray, np.ndarray]:
    Ai = np.linalg.inv(np.asarray(A, dtype=float))
    return Ai, -Ai @ np.asarray(t, dtype=float)


def flip_matrix(axis: str, center: float) -> tuple[np.ndarray, np.ndarray]:
    """Espelho em torno de ``x = center`` (axis "x") ou ``y = center`` (axis "y")."""
    if axis == "x":
        return np.diag([-1.0, 1.0]), np.array([2.0 * center, 0.0])
    return np.diag([1.0, -1.0]), np.array([0.0, 2.0 * center])


def _move(a: np.ndarray, rotation: float, dx: float, dy: float) -> np.ndarray:
    a = rotate_xy(np.asarray(a, dtype=float), rotation)
    a[..., 0] += dx
    a[..., 1] += dy
    return a


ANCHORS = {
    "bl": (0.0, 0.0), "bc": (0.5, 0.0), "br": (1.0, 0.0),
    "ml": (0.0, 0.5), "c": (0.5, 0.5), "mr": (1.0, 0.5),
    "tl": (0.0, 1.0), "tc": (0.5, 1.0), "tr": (1.0, 1.0),
}


def alignment_delta(bounds: tuple[float, float, float, float], rotation: float, anchor: str,
                    target: tuple[float, float]) -> tuple[float, float, tuple[float, float, float, float]]:
    """Translação que leva o ponto ``anchor`` da placa (após girar) até ``target``.

    Retorna ``(dx, dy, novos_limites)``.
    """
    x0, y0, x1, y1 = bounds
    corners = rotate_xy(np.array([[x0, y0], [x1, y0], [x1, y1], [x0, y1]], dtype=float), rotation)
    rx0, ry0 = corners.min(axis=0)
    rx1, ry1 = corners.max(axis=0)
    fx, fy = ANCHORS.get(anchor, (0.0, 0.0))
    ax, ay = rx0 + fx * (rx1 - rx0), ry0 + fy * (ry1 - ry0)
    dx, dy = target[0] - ax, target[1] - ay
    return float(dx), float(dy), (float(rx0 + dx), float(ry0 + dy), float(rx1 + dx), float(ry1 + dy))


def job_bounds(job: Job) -> tuple[float, float, float, float] | None:
    pts: list[np.ndarray] = []
    for op in job.ops:
        for p in op.paths:
            if len(p):
                pts.append(np.asarray(p, dtype=float)[:, :2])
        if len(op.points):
            pts.append(np.asarray(op.points, dtype=float).reshape(-1, 2))
    if not pts:
        return None
    a = np.vstack(pts)
    return (float(a[:, 0].min()), float(a[:, 1].min()), float(a[:, 0].max()), float(a[:, 1].max()))


def iter_job_points(job: Job) -> Iterable[np.ndarray]:
    for op in job.ops:
        yield from (np.asarray(p, dtype=float) for p in op.paths)

