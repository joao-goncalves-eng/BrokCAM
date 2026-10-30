"""Leitores de arquivos de fabricação: Gerber RS-274X/X2 e Excellon.

API pública:

* :func:`parse_gerber` / :func:`parse_gerber_text` -> :class:`~brokcam.core.model.GerberLayer`
* :func:`parse_excellon` / :func:`parse_excellon_text` -> :class:`~brokcam.core.model.DrillLayer`
* :func:`load_file` — detecta o tipo pelo nome/conteúdo e chama o parser certo.
* :func:`guess_role` — papel provável da camada a partir do nome do arquivo.
* :class:`ParseError` — erro com mensagem em português, pronta para a interface.
"""
from __future__ import annotations


class ParseError(Exception):
    """Erro ao ler um arquivo de fabricação (mensagem em PT-BR, pronta para o usuário)."""


from .gerber import parse_gerber, parse_gerber_text  # noqa: E402
from .excellon import parse_excellon, parse_excellon_text  # noqa: E402
from .detect import load_file, guess_role, detect_kind  # noqa: E402

__all__ = [
    "ParseError",
    "parse_gerber",
    "parse_gerber_text",
    "parse_excellon",
    "parse_excellon_text",
    "load_file",
    "guess_role",
    "detect_kind",
]
