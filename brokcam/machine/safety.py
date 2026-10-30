"""Verificações de segurança antes/durante o envio de programas (sem Qt).

Recursos inspirados na engenharia reversa do sender **Candle** (GRBL), reimplementados aqui:

* **Retomar a partir de uma linha** com o estado modal reconstruído (Candle: "Automatically
  set parser state before sending from selected line"). Pular linhas sem restaurar G90/G91,
  G20/G21, G54..G59, F, S e M3 é perigoso: a máquina pode descer Z com o spindle parado ou
  interpretar coordenadas como incrementais. :func:`prepare_program` lê as linhas anteriores
  e monta um preâmbulo seguro (sobe Z, liga o spindle, posiciona XY, desce devagar).
* **Pausa em M6** (Candle: "Pause sender on M6 command"): o GRBL 1.1 não conhece M6 e responde
  ``error:20``. O M6 é trocado por ``M5`` + mensagem + ``M0`` (pausa) e, ao retomar, o spindle
  volta ao estado anterior antes de continuar.
* **Curso da máquina** (Candle: ``machineBounds``/``softLimitsEnabled``): confere se o programa,
  somado ao zero de trabalho atual, cabe no curso ``$130..$132`` antes de começar, em vez de
  descobrir no meio do trabalho com ALARM:2.
"""
from __future__ import annotations

import math
import re
from dataclasses import dataclass, field
from typing import Iterable

__all__ = ["ModalState", "PreparedProgram", "scan_modal_state", "prepare_program",
           "resume_preamble", "envelope_problems", "program_bounds"]

_COMMENT_RE = re.compile(r"\([^)]*\)|;.*$")
_WORD_RE = re.compile(r"([A-Z])\s*([-+]?(?:\d+\.?\d*|\.\d+))")
_M6_RE = re.compile(r"M0*6(?![0-9.])", re.IGNORECASE)


@dataclass
class ModalState:
    """Estado modal do interpretador após uma sequência de linhas (unidades do programa)."""
    units: str = "G21"            # G20 (pol) / G21 (mm)
    distance: str = "G90"         # G90 absoluto / G91 incremental
    plane: str = "G17"
    wcs: str = "G54"
    motion: str = "G0"            # G0/G1/G2/G3/G38.2...
    feed: float | None = None
    spindle: str = "M5"           # M3/M4/M5
    rpm: float | None = None
    coolant: tuple[str, ...] = ()   # ("M7",), ("M8",), ("M7", "M8") ou ()
    x: float | None = None
    y: float | None = None
    z: float | None = None
    tool: int | None = None
    position_uncertain: bool = False    # G28/G30/G53/G92 tornam a posição de trabalho incerta
    bounds: list[float] = field(default_factory=lambda: [math.inf] * 3 + [-math.inf] * 3)

    def _extend(self, x: float | None, y: float | None, z: float | None) -> None:
        b = self.bounds
        for i, v in enumerate((x, y, z)):
            if v is not None:
                b[i] = min(b[i], v)
                b[i + 3] = max(b[i + 3], v)


def _words(line: str) -> list[tuple[str, float]]:
    code = _COMMENT_RE.sub(" ", line).upper()
    if code.lstrip().startswith("$"):
        return []
    out = []
    for m in _WORD_RE.finditer(code):
        try:
            out.append((m.group(1), float(m.group(2))))
        except ValueError:
            continue
    return out


def _apply_line(st: ModalState, line: str) -> None:
    words = _words(line)
    if not words:
        return
    gs = [v for k, v in words if k == "G"]
    ms = [v for k, v in words if k == "M"]
    vals = {k: v for k, v in words if k not in ("G", "M")}
    non_modal_machine = False
    for g in gs:
        gi = round(g * 10)
        if gi in (0, 10, 20, 30) or 382 <= gi <= 385:
            st.motion = f"G{g:g}"
        elif gi in (170, 180, 190):
            st.plane = f"G{g:g}"
        elif gi in (200, 210):
            st.units = f"G{g:g}"
        elif gi in (900, 910):
            st.distance = f"G{g:g}"
        elif 540 <= gi <= 590 and gi % 10 == 0:
            st.wcs = f"G{g:g}"
        elif gi in (280, 300, 920, 921):
            st.position_uncertain = True
            non_modal_machine = True
        elif gi == 530:
            non_modal_machine = True     # coordenadas de máquina nesta linha
        elif gi in (100,):
            non_modal_machine = True     # G10 não move
    for m in ms:
        mi = int(round(m))
        if mi in (3, 4):
            st.spindle = f"M{mi}"
        elif mi in (5, 2, 30):
            st.spindle = "M5"
            if mi in (2, 30):
                st.coolant = ()
        elif mi == 7:
            st.coolant = tuple(sorted(set(st.coolant) | {"M7"}))
        elif mi == 8:
            st.coolant = tuple(sorted(set(st.coolant) | {"M8"}))
        elif mi == 9:
            st.coolant = ()
    if "F" in vals:
        st.feed = vals["F"]
    if "S" in vals:
        st.rpm = vals["S"]
    if "T" in vals:
        st.tool = int(vals["T"])
    if non_modal_machine or not any(a in vals for a in "XYZ"):
        return
    if any(round(g * 10) == 40 for g in gs):      # G4 P... não move
        return
    x0, y0 = st.x, st.y
    inc = st.distance == "G91"
    new = []
    for a in "XYZ":
        cur = getattr(st, a.lower())
        if a in vals:
            if inc:
                v = None if cur is None else cur + vals[a]
            else:
                v = vals[a]
        else:
            v = cur
        new.append(v)
    st.x, st.y, st.z = new
    st._extend(st.x, st.y, st.z)
    # arcos: amplia a caixa pelo círculo inteiro (conservador)
    if st.motion in ("G2", "G3") and st.plane == "G17" and x0 is not None and y0 is not None:
        if "I" in vals or "J" in vals:
            cx, cy = x0 + vals.get("I", 0.0), y0 + vals.get("J", 0.0)
            r = math.hypot(x0 - cx, y0 - cy)
        elif "R" in vals and st.x is not None and st.y is not None:
            r = abs(vals["R"])
            cx, cy = (x0 + st.x) / 2, (y0 + st.y) / 2
        else:
            return
        st._extend(cx - r, cy - r, None)
        st._extend(cx + r, cy + r, None)


def scan_modal_state(lines: Iterable[str], upto: int | None = None) -> ModalState:
    """Estado modal depois de executar as linhas ``[0, upto)`` (todas, se ``upto`` é None)."""
    st = ModalState()
    for i, ln in enumerate(lines):
        if upto is not None and i >= upto:
            break
        _apply_line(st, ln)
    return st


def program_bounds(lines: Iterable[str]) -> tuple[float, float, float, float, float, float] | None:
    """Caixa (xmin, ymin, zmin, xmax, ymax, zmax) do programa em mm, coordenadas de trabalho.

    Considera G90/G91, G20/G21 e arcos (círculo completo, conservador). Devolve ``None``
    se o programa não tem movimentos com coordenadas conhecidas.
    """
    st = scan_modal_state(lines)
    b = st.bounds
    if not all(math.isfinite(v) for v in (b[0], b[1], b[3], b[4])):
        return None
    k = 25.4 if st.units == "G20" else 1.0
    zmin = b[2] * k if math.isfinite(b[2]) else 0.0
    zmax = b[5] * k if math.isfinite(b[5]) else 0.0
    return (b[0] * k, b[1] * k, zmin, b[3] * k, b[4] * k, zmax)


def _f(v: float) -> str:
    s = f"{v:.4f}".rstrip("0").rstrip(".")
    return "0" if s in ("-0", "") else s


def resume_preamble(st: ModalState, safe_z_mm: float = 5.0, plunge_feed: float | None = None,
                    spindle_wait_s: float = 2.0) -> list[str]:
    """Linhas que levam a máquina, com segurança, ao estado em que o programa estaria.

    Ordem: unidades/plano/sistema → sobe para Z seguro (absoluto) → liga spindle/refrigeração e
    espera → vai a XY → desce até o Z anterior com avanço de mergulho → restaura F, G91 e o modo
    de movimento. Valores na unidade do próprio programa.
    """
    k = 1 / 25.4 if st.units == "G20" else 1.0
    safe = float(safe_z_mm) * k
    out = [f"{st.units} G90 {st.plane} {st.wcs}"]
    if st.z is None or st.z < safe:
        out.append(f"G0 Z{_f(safe)}")
    if st.spindle in ("M3", "M4"):
        out.append(f"{st.spindle} S{_f(st.rpm or 0.0)}")
        if spindle_wait_s > 0:
            out.append(f"G4 P{spindle_wait_s:g}")
    for c in st.coolant:
        out.append(c)
    if st.x is not None or st.y is not None:
        xy = " ".join(f"{a}{_f(v)}" for a, v in (("X", st.x), ("Y", st.y)) if v is not None)
        out.append(f"G0 {xy}")
    feed = st.feed if st.feed and st.feed > 0 else None
    if st.z is not None and st.z < safe:
        cap = 100.0 * k                       # mergulho no máx. 100 mm/min (ou equivalente em pol)
        pf = plunge_feed or (min(feed, cap) if feed else cap)
        out.append(f"G1 Z{_f(st.z)} F{_f(pf)}")
    if feed:
        out.append(f"F{_f(feed)}")
    tail = []
    if st.distance == "G91":
        tail.append("G91")
    if st.motion in ("G0", "G1") and (st.motion == "G0" or feed):
        tail.append(st.motion)
    if tail:
        out.append(" ".join(tail))
    return out


@dataclass
class PreparedProgram:
    """Programa pronto para o envio: ``(linha_do_arquivo_1_based, texto)``."""
    items: list[tuple[int, str]]
    warnings: list[str] = field(default_factory=list)
    m6_pauses: int = 0


def prepare_program(lines: list[str], start_line: int = 1, *, m6_pause: bool = False,
                    safe_z_mm: float = 5.0, spindle_wait_s: float = 2.0) -> PreparedProgram:
    """Monta a lista de envio a partir de ``lines`` (texto bruto, sem limpar comentários).

    * ``start_line > 1``: insere :func:`resume_preamble` com o estado das linhas anteriores
      (numerado como a própria linha de início, para o progresso ficar correto).
    * ``m6_pause``: cada linha com M6 vira ``M5`` + ``(MSG,...)`` + ``M0`` + a linha sem o M6 e,
      se o spindle estava ligado, ``M3/M4 S`` + ``G4`` para religá-lo depois da troca.
    """
    warnings: list[str] = []
    items: list[tuple[int, str]] = []
    start_line = max(1, int(start_line))
    st = scan_modal_state(lines, start_line - 1)
    if start_line > 1:
        if st.position_uncertain:
            warnings.append("O trecho anterior usa G28/G30/G53/G92: a posição reconstruída pode não ser exata.")
        if st.motion in ("G2", "G3"):
            warnings.append("A linha anterior era um arco (G2/G3); confira se a linha inicial tem todas as palavras do arco.")
        if st.z is None:
            warnings.append("Nenhum Z foi definido antes da linha inicial; a ferramenta fica no Z seguro.")
        items.append((start_line, f"(MSG,Retomando a partir da linha {start_line})"))
        for t in resume_preamble(st, safe_z_mm, spindle_wait_s=spindle_wait_s):
            items.append((start_line, t))
    n_m6 = 0
    for i in range(start_line - 1, len(lines)):
        ln = lines[i]
        line_no = i + 1
        code = _COMMENT_RE.sub("", ln)
        if m6_pause and _M6_RE.search(code):
            n_m6 += 1
            spindle_on = st.spindle in ("M3", "M4")
            rest = _M6_RE.sub("", code).strip()
            _apply_line(st, ln)
            tool = f"T{st.tool}" if st.tool is not None else "a próxima ferramenta"
            items.append((line_no, "M5"))
            items.append((line_no, f"(MSG,Troque para {tool} e retome)"))
            items.append((line_no, "M0"))
            if rest:
                items.append((line_no, rest))
            if spindle_on and st.spindle != "M5":
                items.append((line_no, f"{st.spindle} S{_f(st.rpm or 0.0)}"))
                if spindle_wait_s > 0:
                    items.append((line_no, f"G4 P{spindle_wait_s:g}"))
            continue
        _apply_line(st, ln)
        items.append((line_no, ln))
    return PreparedProgram(items, warnings, n_m6)


def envelope_problems(bounds_work: tuple[float, float, float, float, float, float],
                      wco: tuple[float, float, float] | None,
                      settings: dict[int, float]) -> list[str]:
    """Eixos em que o programa sairia do curso da máquina (lista vazia = cabe ou não dá para saber).

    ``bounds_work``: caixa do programa em coordenadas de TRABALHO (mm). ``wco``: deslocamento
    de trabalho atual (MPos − WPos). ``settings``: ``$$`` lidos (precisa de $130..$132 e $23).
    Só faz sentido com a máquina referenciada (homing, $22=1): sem homing a posição de máquina
    é arbitrária. O curso do GRBL 1.1 vai de ``-curso`` a 0 (ou de 0 a ``+curso`` se o bit do
    eixo em $23 inverte a direção do homing).
    """
    if wco is None or not settings:
        return []
    if not settings.get(22):
        return []
    mask = int(settings.get(23, 0) or 0)
    probs = []
    tol = 0.01
    for i, ax in enumerate("XYZ"):
        travel = settings.get(130 + i)
        if not travel or travel <= 0:
            continue
        lo, hi = (0.0, travel) if mask & (1 << i) else (-travel, 0.0)
        mn = bounds_work[i] + wco[i]
        mx = bounds_work[i + 3] + wco[i]
        if mn < lo - tol or mx > hi + tol:
            over = max(lo - mn, mx - hi)
            probs.append(f"Eixo {ax}: o programa vai de {mn:.2f} a {mx:.2f} mm (máquina), mas o curso é "
                         f"{lo:.0f} a {hi:.0f} mm — passaria {over:.2f} mm do limite.")
    return probs
