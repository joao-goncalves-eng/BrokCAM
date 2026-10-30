"""Fachada única entre a interface e os módulos de domínio (parsers, CAM, G-code, máquina).

Os módulos de domínio são importados de forma tardia e protegida: se algum ainda não
existir (ou falhar ao importar), a interface continua funcionando com recursos
reduzidos e mensagens claras. Se as APIs mudarem de nome, adapte SOMENTE aqui.

Pontos de integração:
* ``brokcam.parsers.detect``   -> :func:`load_file`, :func:`guess_role`, :data:`ParseError`
* ``brokcam.cam.operations``   -> :func:`operations`, :func:`operations_for`
* ``brokcam.cam.Cancelled``    -> :func:`cancelled_exception`
* ``brokcam.gcode.grbl``       -> :func:`grbl_post_group`, :func:`generate_gcode`, :func:`generate_program`
* ``brokcam.gcode.preview``    -> :func:`parse_gcode`
* ``brokcam.machine.grbl``     -> :func:`create_controller`, :func:`list_ports`
* ``brokcam.machine.grbl_codes`` -> :func:`grbl_codes`
"""
from __future__ import annotations

import dataclasses
import importlib
import logging
import math
import os
import re
from dataclasses import dataclass, field
from types import ModuleType
from typing import Any, Callable

import numpy as np

from brokcam.core.model import DrillLayer, GeometryLayer, GerberLayer, Job
from brokcam.core.params import ParamGroup

log = logging.getLogger("brokcam.ui.backend")

SIMULATOR_PORT = "SIMULADOR"


class BackendUnavailable(RuntimeError):
    """Um módulo de domínio necessário ainda não está disponível."""


def _import(name: str) -> ModuleType | None:
    try:
        return importlib.import_module(name)
    except ImportError as exc:
        # Só silencia se o módulo em si não existe; erros internos são registrados.
        if getattr(exc, "name", None) and name.startswith(exc.name or "\0"):
            return None
        log.warning("Falha ao importar %s: %s", name, exc)
        return None
    except Exception as exc:  # pragma: no cover - erro dentro do módulo de outro agente
        log.exception("Erro ao importar %s: %s", name, exc)
        return None


def status() -> dict[str, bool]:
    """Disponibilidade de cada módulo (usado na barra de status e em 'Sobre')."""
    return {
        "parsers": _import("brokcam.parsers.detect") is not None,
        "cam": _import("brokcam.cam.operations") is not None,
        "gcode": _import("brokcam.gcode.grbl") is not None,
        "preview": _import("brokcam.gcode.preview") is not None,
        "machine": _import("brokcam.machine.grbl") is not None,
    }


# =========================================================================== parsers

def parse_error_types() -> tuple[type[BaseException], ...]:
    mod = _import("brokcam.parsers")
    exc = getattr(mod, "ParseError", None) if mod else None
    return (exc,) if isinstance(exc, type) else ()


def load_file(path: str) -> GerberLayer | DrillLayer | GeometryLayer:
    mod = _import("brokcam.parsers.detect")
    if mod is None or not hasattr(mod, "load_file"):
        raise BackendUnavailable("O módulo de leitura de arquivos (parsers) ainda não está disponível.")
    return mod.load_file(path)


_ROLE_PATTERNS: list[tuple[str, str]] = [
    (r"(\.gko|\.gm1|\.gml|edge|outline|contorno|board|\.oln|profile)", "outline"),
    (r"(\.drl|\.xln|\.exc|\.txt$|drill|furo|\.drd|\.tap$)", "drill"),
    (r"(\.gtl|\.cmp|f[._-]?cu|top[._-]?copper|\.top$|copper_top|-top\b)", "top_copper"),
    (r"(\.gbl|\.sol|b[._-]?cu|bottom[._-]?copper|\.bot$|copper_bottom|-bottom\b)", "bottom_copper"),
    (r"(\.gto|\.gbo|silk|legend|\.plc|\.pls)", "silk"),
    (r"(\.gts|\.gbs|mask|\.stc|\.sts)", "mask"),
    (r"(\.gtp|\.gbp|paste|\.crc|\.crs)", "paste"),
]


def guess_role(filename: str) -> str:
    mod = _import("brokcam.parsers.detect")
    if mod is not None and hasattr(mod, "guess_role"):
        try:
            return str(mod.guess_role(filename))
        except Exception:  # pragma: no cover
            log.exception("guess_role falhou para %s", filename)
    low = os.path.basename(filename).lower()
    for pat, role in _ROLE_PATTERNS:
        if re.search(pat, low):
            return role
    return "unknown"


# =========================================================================== CAM

@dataclass
class OperationInfo:
    """Visão da interface sobre uma operação de CAM (independe da classe real)."""
    key: str
    title: str
    description: str
    group: ParamGroup
    func: Callable[..., Job]
    accepts: tuple[str, ...]
    produces: str = "job"          # "job" | "layer" (ex.: painelização gera nova camada)
    raw: Any = field(default=None, repr=False)


def _wrap_op(op: Any) -> OperationInfo:
    return OperationInfo(
        key=getattr(op, "key", ""), title=getattr(op, "title", getattr(op, "key", "?")),
        description=getattr(op, "description", ""), group=getattr(op, "group"),
        func=getattr(op, "func"), accepts=tuple(getattr(op, "accepts", ())),
        produces=str(getattr(op, "produces", "job") or "job"), raw=op)


def operations() -> dict[str, OperationInfo]:
    mod = _import("brokcam.cam.operations")
    ops = getattr(mod, "OPERATIONS", None) if mod else None
    if not ops:
        return {}
    return {k: _wrap_op(v) for k, v in ops.items()}


def operations_for(kind: str) -> list[OperationInfo]:
    mod = _import("brokcam.cam.operations")
    if mod is None:
        return []
    if hasattr(mod, "operations_for"):
        try:
            return [_wrap_op(o) for o in mod.operations_for(kind)]
        except Exception:  # pragma: no cover
            log.exception("operations_for(%s) falhou", kind)
    return [o for o in operations().values() if kind in o.accepts]


def cancelled_exception() -> type[BaseException]:
    mod = _import("brokcam.cam")
    exc = getattr(mod, "Cancelled", None) if mod else None
    return exc if isinstance(exc, type) else _LocalCancelled


class _LocalCancelled(Exception):
    pass


def run_operation(op: OperationInfo, layer: Any, settings: dict[str, Any],
                  progress: Callable[[float, str], None] | None = None,
                  cancel: Callable[[], bool] | None = None) -> Any:
    """Executa a operação. Devolve um ``Job``, uma lista de ``Job`` (ex.: furação com um
    trabalho por broca) ou, se ``op.produces == "layer"``, uma camada."""
    job = op.func(layer, settings, progress=progress, cancel=cancel)
    if isinstance(job, list):
        for j in job:
            if isinstance(j, Job):
                j.operation = j.operation or op.key
                if not j.settings:
                    j.settings = dict(settings)
        return job
    if not isinstance(job, Job):
        return job
    if not job.operation:
        job.operation = op.key
    if not job.settings:
        job.settings = dict(settings)
    return job


# =========================================================================== G-code

def grbl_post_group() -> ParamGroup | None:
    mod = _import("brokcam.gcode.grbl")
    grp = getattr(mod, "GRBL_POST", None) if mod else None
    return grp if isinstance(grp, ParamGroup) else None


def generate_gcode(job: Job, settings: dict[str, Any]) -> str:
    mod = _import("brokcam.gcode.grbl")
    if mod is None or not hasattr(mod, "generate_gcode"):
        raise BackendUnavailable("O pós-processador GRBL ainda não está disponível.")
    return mod.generate_gcode(job, settings)


def generate_program(jobs: list[Job], settings: dict[str, Any]) -> str:
    mod = _import("brokcam.gcode.grbl")
    if mod is None:
        raise BackendUnavailable("O pós-processador GRBL ainda não está disponível.")
    if hasattr(mod, "generate_program"):
        return mod.generate_program(jobs, settings)
    return "\n".join(generate_gcode(j, settings) for j in jobs)


@dataclass
class Preview:
    """Pré-visualização normalizada de um programa G-code."""
    rapids: np.ndarray                 # (N, 2, 3)
    feeds: np.ndarray                  # (M, 2, 3)
    rapid_lines: np.ndarray            # (N,) linha (0-based) de cada deslocamento
    feed_lines: np.ndarray             # (M,) linha (0-based) de cada corte
    bounds: tuple[float, float, float, float]
    estimate_time_s: float


def _normalize_preview(pv: Any, n_lines: int) -> Preview:
    rapids = np.asarray(getattr(pv, "rapids", np.zeros((0, 2, 3))), dtype=float).reshape(-1, 2, 3)
    feeds = np.asarray(getattr(pv, "feeds", np.zeros((0, 2, 3))), dtype=float).reshape(-1, 2, 3)
    los = getattr(pv, "line_of_segment", None)
    rl = np.zeros(len(rapids), dtype=np.int64)
    fl = np.zeros(len(feeds), dtype=np.int64)
    if isinstance(los, dict):
        rl = np.asarray(los.get("rapids", rl), dtype=np.int64)
        fl = np.asarray(los.get("feeds", fl), dtype=np.int64)
    elif isinstance(los, (tuple, list)) and len(los) == 2 and hasattr(los[0], "__len__"):
        rl = np.asarray(los[0], dtype=np.int64)
        fl = np.asarray(los[1], dtype=np.int64)
    elif los is not None:
        arr = np.asarray(los, dtype=np.int64).ravel()
        if len(arr) == len(rapids) + len(feeds):
            rl, fl = arr[:len(rapids)], arr[len(rapids):]
        elif len(arr) == len(feeds):
            fl = arr
    for name, ll in (("rapid_lines", "rapid"), ("feed_lines", "feed")):
        val = getattr(pv, name, None)
        if val is not None:
            if ll == "rapid":
                rl = np.asarray(val, dtype=np.int64)
            else:
                fl = np.asarray(val, dtype=np.int64)
    b = getattr(pv, "bounds", None)
    if b is None or len(b) < 4:
        b = _segments_bounds(rapids, feeds)
    elif len(b) == 6:  # (xmin, ymin, zmin, xmax, ymax, zmax)
        b = (b[0], b[1], b[3], b[4])
    est = getattr(pv, "estimate_time_s", 0.0)
    est = float(est() if callable(est) else est or 0.0)
    return Preview(rapids, feeds, rl, fl, tuple(float(v) for v in b[:4]), est)  # type: ignore[arg-type]


def _segments_bounds(*arrays: np.ndarray) -> tuple[float, float, float, float]:
    pts = [a.reshape(-1, 3)[:, :2] for a in arrays if a.size]
    if not pts:
        return (0.0, 0.0, 0.0, 0.0)
    allp = np.vstack(pts)
    return (float(allp[:, 0].min()), float(allp[:, 1].min()),
            float(allp[:, 0].max()), float(allp[:, 1].max()))


def parse_gcode(text: str, rapid_rate: float | None = None) -> Preview:
    """Usa ``brokcam.gcode.preview`` se existir; senão um interpretador mínimo interno."""
    mod = _import("brokcam.gcode.preview")
    n_lines = text.count("\n") + 1
    if mod is not None and hasattr(mod, "parse_gcode"):
        try:
            if rapid_rate:
                try:
                    return _normalize_preview(mod.parse_gcode(text, rapid_feed=rapid_rate), n_lines)
                except TypeError:
                    pass
            return _normalize_preview(mod.parse_gcode(text), n_lines)
        except Exception:  # pragma: no cover
            log.exception("parse_gcode do módulo de preview falhou; usando o interno")
    return _fallback_parse(text)


_WORD = re.compile(r"([A-Za-z])\s*([-+]?(?:\d+\.?\d*|\.\d+))")


def _fallback_parse(text: str) -> Preview:
    """Interpretador mínimo (G0/G1/G2/G3 lineares, G90/G91, G20/G21) para pré-visualização."""
    x = y = z = 0.0
    mode = 0
    absolute = True
    scale = 1.0
    feed = 100.0
    rapids: list[tuple[float, ...]] = []
    feeds: list[tuple[float, ...]] = []
    rl: list[int] = []
    fl: list[int] = []
    t_feed = 0.0
    for ln, raw in enumerate(text.splitlines()):
        line = raw.split(";", 1)[0]
        line = re.sub(r"\(.*?\)", "", line).strip()
        if not line:
            continue
        words = _WORD.findall(line)
        nx, ny, nz = x, y, z
        moved = False
        for letter, val in words:
            L = letter.upper()
            v = float(val)
            if L == "G":
                g = int(round(v))
                if g in (0, 1, 2, 3):
                    mode = g
                elif g == 90:
                    absolute = True
                elif g == 91:
                    absolute = False
                elif g == 20:
                    scale = 25.4
                elif g == 21:
                    scale = 1.0
            elif L in "XYZ":
                v *= scale
                moved = True
                if L == "X":
                    nx = v if absolute else x + v
                elif L == "Y":
                    ny = v if absolute else y + v
                else:
                    nz = v if absolute else z + v
            elif L == "F":
                feed = max(v * scale, 1e-6)
        if moved and (nx, ny, nz) != (x, y, z):
            seg = (x, y, z, nx, ny, nz)
            if mode == 0:
                rapids.append(seg)
                rl.append(ln)
            else:
                feeds.append(seg)
                fl.append(ln)
                t_feed += math.dist((x, y, z), (nx, ny, nz)) / feed * 60.0
            x, y, z = nx, ny, nz
    r = np.asarray(rapids, dtype=float).reshape(-1, 2, 3)
    f = np.asarray(feeds, dtype=float).reshape(-1, 2, 3)
    t_rapid = float(np.linalg.norm(r[:, 1] - r[:, 0], axis=1).sum()) / 1000.0 * 60.0 if len(r) else 0.0
    return Preview(r, f, np.asarray(rl, dtype=np.int64), np.asarray(fl, dtype=np.int64),
                   _segments_bounds(r, f), t_feed + t_rapid)


def estimate_job_time_s(job: Job, rapid_rate: float = 1000.0) -> float:
    """Estimativa simples (sem G-code): corte/avanço + deslocamentos + mergulhos."""
    total = 0.0
    last: np.ndarray | None = None
    for op in job.ops:
        p = op.params
        travel = max(p.travel_z - p.cut_z, 0.0)
        feed_xy = max(p.feed_xy, 1e-6)
        feed_z = max(p.feed_z, 1e-6)
        passes = 1
        if p.multi_depth and p.depth_per_pass > 0:
            passes = max(1, math.ceil(abs(p.cut_z) / p.depth_per_pass))
        for path in op.paths:
            if len(path) < 2:
                continue
            ln = float(np.hypot(*np.diff(path[:, :2], axis=0).T).sum())
            total += passes * ln / feed_xy * 60.0 + passes * travel / feed_z * 60.0
            if last is not None:
                total += float(np.hypot(*(path[0, :2] - last))) / rapid_rate * 60.0
            last = path[-1, :2]
        pts = np.asarray(op.points).reshape(-1, 2)
        if len(pts):
            total += len(pts) * (travel / feed_z * 60.0 * 2 + p.dwell_s)
            d = np.hypot(*np.diff(pts, axis=0).T).sum() if len(pts) > 1 else 0.0
            total += float(d) / rapid_rate * 60.0
            last = pts[-1]
    return total


# =========================================================================== máquina

def machine_available() -> bool:
    mod = _import("brokcam.machine.grbl")
    return mod is not None and hasattr(mod, "GrblController")


def create_controller() -> Any | None:
    mod = _import("brokcam.machine.grbl")
    if mod is None or not hasattr(mod, "GrblController"):
        return None
    try:
        return mod.GrblController()
    except Exception:  # pragma: no cover
        log.exception("Falha ao criar GrblController")
        return None


def list_ports(controller: Any | None) -> list[str]:
    ports: list[Any] = []
    try:
        if controller is not None and hasattr(controller, "list_ports"):
            ports = list(controller.list_ports())
        else:
            mod = _import("brokcam.machine.grbl")
            fn = getattr(getattr(mod, "GrblController", None), "list_ports", None) if mod else None
            if fn is not None:
                ports = list(fn())
    except Exception:  # pragma: no cover
        log.exception("Falha ao listar portas")
    out: list[str] = []
    for p in ports:
        if isinstance(p, str):
            out.append(p)
        elif isinstance(p, (tuple, list)) and p:
            out.append(str(p[0]))
        else:
            out.append(str(getattr(p, "device", p)))
    if SIMULATOR_PORT not in out:
        out.append(SIMULATOR_PORT)
    return out


def call_first(obj: Any, names: tuple[str, ...], *args: Any, **kwargs: Any) -> bool:
    """Chama o primeiro método existente de ``names``. Retorna False se nenhum existir."""
    for n in names:
        fn = getattr(obj, n, None)
        if callable(fn):
            fn(*args, **kwargs)
            return True
    return False


# Bytes de tempo real do GRBL 1.1 para overrides.
_OVR_BYTES = {
    ("feed", "reset"): 0x90, ("feed", "+10"): 0x91, ("feed", "-10"): 0x92,
    ("feed", "+1"): 0x93, ("feed", "-1"): 0x94,
    ("rapid", "100"): 0x95, ("rapid", "50"): 0x96, ("rapid", "25"): 0x97,
    ("spindle", "reset"): 0x99, ("spindle", "+10"): 0x9A, ("spindle", "-10"): 0x9B,
    ("spindle", "+1"): 0x9C, ("spindle", "-1"): 0x9D,
}


def override(controller: Any, kind: str, action: str) -> None:
    """Override de avanço/rápido/spindle (``kind``) com ``action`` em +10,-10,+1,-1,reset."""
    if controller is None:
        return
    for name in (f"override_{kind}", f"{kind}_override", "override"):
        fn = getattr(controller, name, None)
        if callable(fn):
            try:
                if name == "override":
                    fn(kind, action)
                else:
                    fn(action)
                return
            except TypeError:
                continue
    code = _OVR_BYTES.get((kind, action))
    if code is not None:
        call_first(controller, ("send_realtime", "realtime", "write_realtime"), bytes([code]))


def status_to_dict(st: Any) -> dict[str, Any]:
    """Converte o objeto de status do controlador (dataclass ``GrblStatus``) em dict."""
    if isinstance(st, dict):
        return dict(st)
    if dataclasses.is_dataclass(st) and not isinstance(st, type):
        return dataclasses.asdict(st)
    try:
        return dict(vars(st))
    except TypeError:
        return {"state": str(st)}


def normalize_status(st: dict[str, Any]) -> dict[str, Any]:
    """Normaliza o dicionário de status do controlador para chaves conhecidas."""
    low = {str(k).lower(): v for k, v in st.items()}

    def vec(*keys: str) -> tuple[float, float, float] | None:
        for k in keys:
            v = low.get(k)
            if v is None:
                continue
            if isinstance(v, str):
                v = [float(t) for t in v.split(",")]
            v = list(v) + [0.0, 0.0, 0.0]
            return (float(v[0]), float(v[1]), float(v[2]))
        return None

    mpos = vec("mpos", "machine_pos", "machine")
    wpos = vec("wpos", "work_pos", "work")
    wco = vec("wco", "work_offset")
    if wpos is None and mpos is not None and wco is not None:
        wpos = tuple(m - o for m, o in zip(mpos, wco))  # type: ignore[assignment]
    if mpos is None and wpos is not None and wco is not None:
        mpos = tuple(w + o for w, o in zip(wpos, wco))  # type: ignore[assignment]
    ov = low.get("ov") or low.get("overrides")
    if isinstance(ov, str):
        ov = [int(float(t)) for t in ov.split(",")]
    fs = low.get("fs")
    feed = low.get("feed", low.get("f"))
    spindle = low.get("spindle", low.get("s"))
    if isinstance(fs, (list, tuple)) and len(fs) >= 2:
        feed, spindle = fs[0], fs[1]
    elif isinstance(fs, str) and "," in fs:
        feed, spindle = (float(t) for t in fs.split(",")[:2])
    return {
        "state": str(low.get("state", low.get("status", "")) or ""),
        "mpos": mpos, "wpos": wpos, "wco": wco,
        "feed": feed, "spindle": spindle,
        "ov": list(ov) if ov else None,
        "buffer": low.get("bf") or low.get("buffer") or low.get("planner_free"),
        "line": low.get("ln") or low.get("line") or low.get("line_number"),
        "pins": low.get("pn") or low.get("pins") or "",
    }


# Códigos de reserva (usados só se brokcam.machine.grbl_codes não existir).
_FALLBACK_STATES = {
    "Idle": "Ocioso — pronto para receber comandos.",
    "Run": "Executando movimento.",
    "Hold": "Pausado (feed hold). Use Retomar para continuar.",
    "Jog": "Movimento manual (jog) em andamento.",
    "Alarm": "Alarme — movimento bloqueado. Verifique a causa, depois use Destravar ($X) ou Home ($H).",
    "Door": "Porta de segurança aberta.",
    "Check": "Modo de verificação de G-code ($C): nada se move.",
    "Home": "Executando ciclo de homing.",
    "Sleep": "Modo de suspensão. Use Reset para sair.",
}


class _Codes:
    def __init__(self, mod: ModuleType | None) -> None:
        self.ERRORS: dict[int, Any] = dict(getattr(mod, "ERRORS", {}) or {})
        self.ALARMS: dict[int, Any] = dict(getattr(mod, "ALARMS", {}) or {})
        self.SETTINGS: dict[int, Any] = dict(getattr(mod, "SETTINGS", {}) or {})
        self.STATES: dict[str, Any] = dict(getattr(mod, "STATES", {}) or {}) or dict(_FALLBACK_STATES)


def grbl_codes() -> _Codes:
    return _Codes(_import("brokcam.machine.grbl_codes"))


def state_text(codes: _Codes, state: str) -> tuple[str, str]:
    """(título PT-BR, explicação + ação sugerida) de um estado como ``Hold:0``."""
    mod = _import("brokcam.machine.grbl_codes")
    entry: Any = None
    if mod is not None and hasattr(mod, "state_info"):
        try:
            entry = mod.state_info(state)
        except Exception:  # pragma: no cover
            entry = None
    if entry is None:
        base = state.split(":")[0]
        entry = codes.STATES.get(state) or codes.STATES.get(base + ":0") or codes.STATES.get(base)
    return code_text(entry)


def code_text(entry: Any) -> tuple[str, str]:
    """(título curto, explicação) a partir de uma entrada de ERRORS/ALARMS/STATES."""
    if entry is None:
        return "", ""
    if isinstance(entry, str):
        return "", entry
    if isinstance(entry, (tuple, list)):
        items = [str(e) for e in entry]
        return (items[0] if items else "", " ".join(items[1:]))
    if isinstance(entry, dict):
        title = entry.get("title") or entry.get("name") or entry.get("short") or ""
        text = entry.get("help") or entry.get("description") or entry.get("text") or ""
        return str(title), str(text)
    title = getattr(entry, "title", None) or getattr(entry, "name", "") or ""
    text = getattr(entry, "help", None) or getattr(entry, "description", "") or ""
    return str(title), str(text)


def setting_info(entry: Any) -> tuple[str, str, str]:
    """(nome, unidade, explicação) de uma entrada de SETTINGS."""
    if entry is None:
        return "", "", ""
    if isinstance(entry, str):
        return entry, "", ""
    if isinstance(entry, (tuple, list)):
        items = [str(e) for e in entry] + ["", "", ""]
        return items[0], items[1], items[2]
    if isinstance(entry, dict):
        return (str(entry.get("name") or entry.get("title") or ""),
                str(entry.get("unit") or entry.get("units") or ""),
                str(entry.get("help") or entry.get("description") or ""))
    return (str(getattr(entry, "name", "") or getattr(entry, "title", "")),
            str(getattr(entry, "unit", "") or getattr(entry, "units", "")),
            str(getattr(entry, "help", "") or getattr(entry, "description", "")))
