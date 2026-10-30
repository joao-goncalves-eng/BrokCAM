"""Detecção do tipo de arquivo (Gerber × Excellon) e do papel provável da camada."""
from __future__ import annotations

import os
import re
from typing import Literal

from brokcam.core.model import SourceLayer

from . import ParseError
from .excellon import parse_excellon_text
from .gerber import _read_text, parse_gerber_text

__all__ = ["load_file", "guess_role", "detect_kind", "GERBER_EXTS", "DRILL_EXTS"]

Kind = Literal["gerber", "excellon"]

GERBER_EXTS = frozenset({
    ".gbr", ".ger", ".gb", ".pho", ".art", ".274", ".grb", ".geb",
    ".gtl", ".gbl", ".gto", ".gbo", ".gts", ".gbs", ".gtp", ".gbp", ".gko", ".gm", ".gml",
    ".gm1", ".gm2", ".gm3", ".gm13", ".gm15", ".gpt", ".gpb", ".gd1", ".gg1",
    ".g1", ".g2", ".g3", ".g4", ".gp1", ".gp2",
    ".cmp", ".sol", ".plc", ".pls", ".stc", ".sts", ".crc", ".crs", ".dim", ".oln", ".gbx",
})
DRILL_EXTS = frozenset({".drl", ".xln", ".exc", ".drd", ".dri", ".tap", ".ncd", ".drill"})
AMBIGUOUS_EXTS = frozenset({".txt", ".nc", ".cnc", ".dat", ""})

_GERBER_HINT = re.compile(r"%FS[LTD]?[AI]|%MO(?:IN|MM)|%ADD\d+|^G04|^D\d{2,}\*|%TF\.", re.M)
_DRILL_HINT = re.compile(r"^M48\s*$|^(?:METRIC|INCH)\b|^T\d+C\d|^T\d+F\d+S\d+C|^;\s*FILE_FORMAT", re.M | re.I)


def detect_kind(text: str, filename: str = "") -> Kind | None:
    """Decide se o conteúdo é Gerber ou Excellon (conteúdo prevalece; extensão desempata)."""
    head = text[:20000]
    g = len(_GERBER_HINT.findall(head))
    d = len(_DRILL_HINT.findall(head))
    if g and g >= d:
        return "gerber"
    if d:
        return "excellon"
    ext = os.path.splitext(filename)[1].lower()
    if ext in GERBER_EXTS:
        return "gerber"
    if ext in DRILL_EXTS:
        return "excellon"
    return None


def load_file(path: str | os.PathLike[str]) -> SourceLayer:
    """Abre um arquivo de fabricação detectando o formato pela extensão e pelo conteúdo.

    Retorna ``GerberLayer`` ou ``DrillLayer``. Levanta ``ParseError`` com mensagem
    clara em português quando o arquivo não existe, não é legível ou o formato não é
    reconhecido.
    """
    p = os.fspath(path)
    if os.path.isdir(p):
        raise ParseError(f"'{p}' é uma pasta, não um arquivo.")
    text = _read_text(p)
    fname = os.path.basename(p)
    if not text.strip():
        raise ParseError(f"O arquivo '{fname}' está vazio.")
    kind = detect_kind(text, fname)
    ext = os.path.splitext(fname)[1].lower()
    if kind is None:
        raise ParseError(
            f"Não foi possível reconhecer o formato de '{fname}'. São aceitos arquivos Gerber "
            f"(RS-274X: .gbr, .gtl, .gbl, .gko...) e de furação Excellon (.drl, .xln, .txt...).")
    if kind == "gerber":
        layer: SourceLayer = parse_gerber_text(text, name=fname, source_path=p)
        if ext in DRILL_EXTS:
            layer.warnings.insert(0, f"A extensão '{ext}' sugere furação, mas o conteúdo é Gerber.")
    else:
        layer = parse_excellon_text(text, name=fname, source_path=p)
        if ext in GERBER_EXTS:
            layer.warnings.insert(0, f"A extensão '{ext}' sugere Gerber, mas o conteúdo é Excellon.")
    return layer


# --------------------------------------------------------------------------- papel da camada

_ROLE_BY_EXT = {
    ".gtl": "top_copper", ".cmp": "top_copper", ".top": "top_copper",
    ".gbl": "bottom_copper", ".sol": "bottom_copper", ".bot": "bottom_copper",
    ".gko": "outline", ".gm1": "outline", ".gml": "outline", ".gm": "outline", ".dim": "outline",
    ".oln": "outline", ".gm13": "outline", ".gm15": "outline", ".gbx": "outline",
    ".gto": "silk", ".gbo": "silk", ".plc": "silk", ".pls": "silk",
    ".gts": "mask", ".gbs": "mask", ".stc": "mask", ".sts": "mask",
    ".gtp": "paste", ".gbp": "paste", ".crc": "paste", ".crs": "paste",
    ".drl": "drill", ".xln": "drill", ".exc": "drill", ".drd": "drill", ".dri": "drill",
    ".ncd": "drill", ".tap": "drill", ".drill": "drill",
}

# (padrão no nome sem extensão, papel) — testados em ordem; os mais específicos primeiro.
_ROLE_PATTERNS: list[tuple[re.Pattern[str], str]] = [
    (re.compile(p, re.I), role) for p, role in [
        (r"(edge[_.\- ]?cuts|outline|contorno|board[_\- ]?outline|mechanical ?1|keep[_\- ]?out|"
         r"\bdim(ension)?\b|profile|borda)", "outline"),
        (r"(n?pth|drill|furo|fura|\bdrl\b|plated[_\- ]?through|holes?)", "drill"),
        (r"(paste|cream|pasta)", "paste"),
        (r"(mask|stop|m[aá]scara|solder[_\- ]?resist)", "mask"),
        (r"(silk|legend|overlay|serigraf|placement)", "silk"),
        (r"(\bf[_.]cu\b|[_\-.]f[_.]cu|top[_\- ]?(copper|layer)?|front|\bcomponent|copper[_\- ]?top|"
         r"\bl1\b|[_\-]l1\b|\bgtl\b)", "top_copper"),
        (r"(\bb[_.]cu\b|[_\-.]b[_.]cu|bottom[_\- ]?(copper|layer)?|\bbot\b|back|solder[_\- ]?side|"
         r"copper[_\- ]?bottom|\bgbl\b)", "bottom_copper"),
    ]
]


def guess_role(filename: str) -> str:
    """Papel provável de um arquivo a partir do nome (KiCad, Eagle, Altium, EasyEDA...).

    Retorna um de: ``"top_copper"``, ``"bottom_copper"``, ``"outline"``, ``"drill"``,
    ``"silk"``, ``"mask"``, ``"paste"`` ou ``"unknown"``.
    """
    base = os.path.basename(filename)
    stem, ext = os.path.splitext(base)
    ext = ext.lower()
    by_ext = _ROLE_BY_EXT.get(ext)
    if by_ext is not None:  # extensões de papel inequívoco (Protel/Altium/Eagle/furação)
        return by_ext
    s = stem.replace(" ", "_")
    # camadas internas (In1_Cu, Inner1, G1/G2...) não são topo nem fundo
    if re.search(r"(^|[_\-.])in\d+[_.]cu|inner|mid[_\- ]?layer", s, re.I):
        return "unknown"
    for pat, role in _ROLE_PATTERNS:
        if pat.search(s):
            # "TopSolderMask" etc. já foram classificados por mask/paste/silk antes do cobre
            return role
    if ext == ".txt":  # Altium/Protel exportam a furação como .txt
        return "drill"
    return "unknown"
