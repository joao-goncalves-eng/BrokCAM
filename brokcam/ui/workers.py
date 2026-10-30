"""Execução de tarefas pesadas em ``QThreadPool`` com progresso e cancelamento.

A função da tarefa recebe ``progress(frac, msg)`` e ``cancel() -> bool`` e roda numa
thread do pool. Os resultados voltam para a thread da GUI por sinais Qt (conexão
enfileirada), então nenhum widget é tocado fora da thread principal.
"""
from __future__ import annotations

import logging
import threading
import time
import traceback
from typing import Any, Callable

from PySide6.QtCore import QObject, QRunnable, QThreadPool, Signal

from . import backend

log = logging.getLogger("brokcam.ui.workers")

TaskFn = Callable[[Callable[[float, str], None], Callable[[], bool]], Any]


class TaskSignals(QObject):
    progress = Signal(float, str)
    finished = Signal(object)
    failed = Signal(str, str)      # mensagem, traceback
    cancelled = Signal()
    done = Signal()                # sempre emitido ao final


class Task(QRunnable):
    def __init__(self, title: str, fn: TaskFn) -> None:
        super().__init__()
        self.title = title
        self.fn = fn
        self.signals = TaskSignals()
        self._cancel = threading.Event()
        self._last_emit = 0.0
        self.setAutoDelete(False)

    def cancel(self) -> None:
        self._cancel.set()

    def is_cancelled(self) -> bool:
        return self._cancel.is_set()

    def _progress(self, frac: float, msg: str = "") -> None:
        now = time.monotonic()
        # Limita a ~30 atualizações/s para não inundar a fila de eventos.
        if now - self._last_emit >= 0.033 or frac >= 1.0:
            self._last_emit = now
            self.signals.progress.emit(float(max(0.0, min(1.0, frac))), str(msg))

    def run(self) -> None:
        cancelled_exc = backend.cancelled_exception()
        try:
            result = self.fn(self._progress, self.is_cancelled)
            if self._cancel.is_set():
                self.signals.cancelled.emit()
            else:
                self.signals.finished.emit(result)
        except cancelled_exc:
            self.signals.cancelled.emit()
        except Exception as exc:  # noqa: BLE001 - reportado à interface
            tb = traceback.format_exc()
            log.error("Tarefa '%s' falhou: %s\n%s", self.title, exc, tb)
            self.signals.failed.emit(str(exc) or exc.__class__.__name__, tb)
        finally:
            self.signals.done.emit()


class TaskRunner(QObject):
    """Mantém as tarefas ativas e expõe um progresso agregado para a barra de status."""

    started = Signal(object)        # Task
    progress = Signal(object, float, str)
    ended = Signal(object)

    def __init__(self, parent: QObject | None = None) -> None:
        super().__init__(parent)
        self.pool = QThreadPool.globalInstance()
        self.active: list[Task] = []

    def submit(self, title: str, fn: TaskFn, *,
               on_done: Callable[[Any], None] | None = None,
               on_error: Callable[[str, str], None] | None = None,
               on_cancel: Callable[[], None] | None = None) -> Task:
        task = Task(title, fn)
        s = task.signals
        if on_done:
            s.finished.connect(on_done)
        if on_error:
            s.failed.connect(on_error)
        if on_cancel:
            s.cancelled.connect(on_cancel)
        s.progress.connect(lambda f, m, t=task: self.progress.emit(t, f, m))
        s.done.connect(lambda t=task: self._finish(t))
        self.active.append(task)
        self.started.emit(task)
        self.pool.start(task)
        return task

    def _finish(self, task: Task) -> None:
        if task in self.active:
            self.active.remove(task)
        self.ended.emit(task)

    def cancel_all(self) -> None:
        for t in list(self.active):
            t.cancel()

    def busy(self) -> bool:
        return bool(self.active)

    def wait(self, msecs: int = 30000) -> bool:
        return self.pool.waitForDone(msecs)
