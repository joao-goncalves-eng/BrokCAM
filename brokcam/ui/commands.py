"""Comandos de desfazer/refazer (``QUndoCommand``) para operações do projeto."""
from __future__ import annotations

from PySide6.QtGui import QUndoCommand

from .project import JobEntry, LayerEntry, Project, Transform


class AddLayersCmd(QUndoCommand):
    def __init__(self, project: Project, entries: list[LayerEntry]) -> None:
        n = len(entries)
        super().__init__(f"Adicionar {n} camada{'s' if n != 1 else ''}")
        self.project = project
        self.entries = entries

    def redo(self) -> None:
        for e in self.entries:
            self.project.add_layer(e)

    def undo(self) -> None:
        for e in reversed(self.entries):
            self.project.remove_layer(e.id)


class RemoveLayerCmd(QUndoCommand):
    def __init__(self, project: Project, layer_id: int) -> None:
        e = project.layer(layer_id)
        super().__init__(f"Remover camada {e.name if e else ''}")
        self.project = project
        self.layer_id = layer_id
        self.index = 0
        self.entry: LayerEntry | None = e
        self.jobs: list[tuple[int, JobEntry]] = []

    def redo(self) -> None:
        self.jobs = []
        for j in list(self.project.jobs_for(self.layer_id)):
            res = self.project.remove_job(j.id)
            if res:
                self.jobs.append(res)
        res = self.project.remove_layer(self.layer_id)
        if res:
            self.index, self.entry = res

    def undo(self) -> None:
        if self.entry is not None:
            self.project.add_layer(self.entry, self.index)
        for idx, j in reversed(self.jobs):
            self.project.add_job(j, idx)


class AddJobCmd(QUndoCommand):
    def __init__(self, project: Project, entry: JobEntry) -> None:
        super().__init__(f"Gerar {entry.name}")
        self.project = project
        self.entry = entry

    def redo(self) -> None:
        self.project.add_job(self.entry)

    def undo(self) -> None:
        self.project.remove_job(self.entry.id)


class RemoveJobCmd(QUndoCommand):
    def __init__(self, project: Project, job_id: int) -> None:
        j = project.job(job_id)
        super().__init__(f"Remover {j.name if j else 'caminho'}")
        self.project = project
        self.job_id = job_id
        self.index = 0
        self.entry: JobEntry | None = j

    def redo(self) -> None:
        res = self.project.remove_job(self.job_id)
        if res:
            self.index, self.entry = res

    def undo(self) -> None:
        if self.entry is not None:
            self.project.add_job(self.entry, self.index)


class TransformCmd(QUndoCommand):
    def __init__(self, project: Project, layer_id: int, new: Transform) -> None:
        e = project.layer(layer_id)
        super().__init__(f"Transformar {e.name if e else 'camada'}")
        self.project = project
        self.layer_id = layer_id
        self.old = e.transform if e else Transform()
        self.new = new

    def redo(self) -> None:
        self.project.set_layer_transform(self.layer_id, self.new)

    def undo(self) -> None:
        self.project.set_layer_transform(self.layer_id, self.old)


class RoleCmd(QUndoCommand):
    def __init__(self, project: Project, layer_id: int, role: str) -> None:
        e = project.layer(layer_id)
        super().__init__("Alterar papel da camada")
        self.project = project
        self.layer_id = layer_id
        self.old_role = e.role if e else "unknown"
        self.old_color = e.color if e else ""
        self.role = role

    def redo(self) -> None:
        self.project.set_layer_role(self.layer_id, self.role)

    def undo(self) -> None:
        self.project.set_layer_role(self.layer_id, self.old_role)
        if self.old_color:
            self.project.set_layer_color(self.layer_id, self.old_color)


class EditJobOpsCmd(QUndoCommand):
    """Substitui os caminhos de um job (edição manual). Desfazer restaura tudo, inclusive o G-code."""

    def __init__(self, project: Project, job_id: int, new_ops: list, text: str = "Editar caminhos") -> None:
        super().__init__(text)
        self.project = project
        self.job_id = job_id
        j = project.job(job_id)
        self.old_ops = list(j.job.ops) if j else []
        self.old_gcode = j.job.gcode if j else ""
        self.old_edited = bool(j.job.settings.get("edited")) if j else False
        self.new_ops = list(new_ops)

    def redo(self) -> None:
        self.project.set_job_ops(self.job_id, self.new_ops, "", edited=True)

    def undo(self) -> None:
        self.project.set_job_ops(self.job_id, self.old_ops, self.old_gcode, edited=self.old_edited)


class MoveAllCmd(QUndoCommand):
    """Move rigidamente a placa inteira (camadas + caminhos)."""

    def __init__(self, project: Project, rotation: float, dx: float, dy: float,
                 text: str = "Alinhar placa") -> None:
        super().__init__(text)
        self.project = project
        self.rotation = rotation
        self.dx = dx
        self.dy = dy
        self.gcodes = {j.id: j.job.gcode for j in project.jobs}

    def redo(self) -> None:
        self.project.move_all(self.rotation, self.dx, self.dy)

    def undo(self) -> None:
        from .project import rotate_xy
        # inverso de p' = R·p + d  →  p = R⁻¹·(p' − d) = R⁻¹·p' − R⁻¹·d
        inv = rotate_xy([[self.dx, self.dy]], -self.rotation)[0]
        self.project.move_all(-self.rotation, -float(inv[0]), -float(inv[1]))
        for j in self.project.jobs:
            if j.id in self.gcodes:
                j.job.gcode = self.gcodes[j.id]
                self.project.jobChanged.emit(j)


class FlipLayersCmd(QUndoCommand):
    """Vira camadas (e seus caminhos) em torno de um eixo — desfazer é virar de novo."""

    def __init__(self, project: Project, layer_ids: list[int], axis: str, center: float,
                 text: str = "Virar placa (dupla face)") -> None:
        super().__init__(text)
        self.project = project
        self.layer_ids = list(layer_ids)
        self.axis = axis
        self.center = center
        self.gcodes = {j.id: j.job.gcode for j in project.jobs if j.source_id in set(layer_ids)}

    def redo(self) -> None:
        self.project.flip_layers(self.layer_ids, self.axis, self.center)

    def undo(self) -> None:
        self.project.flip_layers(self.layer_ids, self.axis, self.center)
        for j in self.project.jobs:
            if j.id in self.gcodes:
                j.job.gcode = self.gcodes[j.id]
                self.project.jobChanged.emit(j)


class ClearJobGcodeCmd(QUndoCommand):
    """Remove o G-code gerado de um caminho (o caminho em si continua)."""

    def __init__(self, project: Project, job_id: int) -> None:
        super().__init__("Remover G-code")
        self.project = project
        self.job_id = job_id
        j = project.job(job_id)
        self.text = j.job.gcode if j else ""

    def _set(self, text: str) -> None:
        j = self.project.job(self.job_id)
        if j is not None:
            j.job.gcode = text
            self.project.jobChanged.emit(j)
            self.project.set_modified()

    def redo(self) -> None:
        self._set("")

    def undo(self) -> None:
        self._set(self.text)


class AffineCmd(QUndoCommand):
    """Movimento rígido (giro/translação/espelho) de camadas e dos caminhos delas."""

    def __init__(self, project: Project, layer_ids: list[int], A, t, text: str = "Transformar") -> None:  # type: ignore[no-untyped-def]
        import numpy as np
        super().__init__(text)
        self.project = project
        self.layer_ids = list(layer_ids)
        self.A = np.asarray(A, dtype=float)
        self.t = np.asarray(t, dtype=float)
        self.gcodes = {j.id: j.job.gcode for j in project.jobs if j.source_id in set(layer_ids)}

    def redo(self) -> None:
        self.project.transform_layers(self.layer_ids, self.A, self.t)

    def undo(self) -> None:
        from .project import invert_affine
        Ai, ti = invert_affine(self.A, self.t)
        self.project.transform_layers(self.layer_ids, Ai, ti)
        for j in self.project.jobs:
            if j.id in self.gcodes:
                j.job.gcode = self.gcodes[j.id]
                self.project.jobChanged.emit(j)


class GroupCmd(QUndoCommand):
    """Agrupa camadas (gid novo) ou desagrupa (gid=None)."""

    def __init__(self, project: Project, layer_ids: list[int], gid: int | None, name: str = "",
                 text: str = "") -> None:
        super().__init__(text or ("Agrupar camadas" if gid is not None else "Desagrupar camadas"))
        self.project = project
        self.layer_ids = list(layer_ids)
        self.gid = gid
        self.name = name
        self.old = {lid: (e.group if (e := project.layer(lid)) else None) for lid in layer_ids}
        self.old_names = dict(project.groups)

    def redo(self) -> None:
        names = {self.gid: self.name} if self.gid is not None else None
        self.project.set_layer_groups({lid: self.gid for lid in self.layer_ids}, names)

    def undo(self) -> None:
        self.project.groups.update(self.old_names)
        self.project.set_layer_groups(self.old, None)
