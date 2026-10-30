"""Operações de CAM do BrokCAM (isolamento, limpeza de cobre, recorte, furação...).

Todas as funções de CAM são puras (sem Qt, sem estado global) e seguem a forma::

    func(layer, settings: dict, progress=None, cancel=None) -> Job

``progress(frac, msg)`` recebe a fração concluída (0..1) e uma mensagem em PT-BR;
``cancel()`` é consultado periodicamente e, se retornar ``True``, a operação levanta
:class:`Cancelled`.

O registro das operações para a interface fica em :mod:`brokcam.cam.operations`
(importado sob demanda para manter este pacote leve).
"""
from __future__ import annotations


class Cancelled(Exception):
    """Levantada quando o usuário cancela uma operação de CAM em andamento."""


__all__ = ["Cancelled"]
