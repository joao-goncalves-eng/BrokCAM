"""Conversão e formatação de unidades de exibição (internamente tudo é mm)."""
from __future__ import annotations

MM_PER_IN = 25.4

_current = "mm"

# Unidades de ParamSpec que representam comprimento (convertidas para polegadas).
LENGTH_UNITS = {"mm": "pol", "mm/min": "pol/min", "mm/s": "pol/s", "mm²": "pol²"}


def set_display_units(units: str) -> None:
    global _current
    _current = "in" if units == "in" else "mm"


def display_units() -> str:
    return _current


def unit_label() -> str:
    return "pol" if _current == "in" else "mm"


def to_display(mm: float) -> float:
    return mm / MM_PER_IN if _current == "in" else mm


def from_display(v: float) -> float:
    return v * MM_PER_IN if _current == "in" else v


def fmt_len(mm: float, decimals: int | None = None, with_unit: bool = True) -> str:
    if decimals is None:
        decimals = 4 if _current == "in" else 3
    s = f"{to_display(mm):.{decimals}f}".replace(".", ",")
    return f"{s} {unit_label()}" if with_unit else s


def fmt_num(v: float, decimals: int = 2) -> str:
    s = f"{v:,.{decimals}f}"
    return s.replace(",", "\0").replace(".", ",").replace("\0", ".")


def fmt_time(seconds: float) -> str:
    seconds = max(0, int(round(seconds)))
    h, rem = divmod(seconds, 3600)
    m, s = divmod(rem, 60)
    if h:
        return f"{h} h {m:02d} min"
    if m:
        return f"{m} min {s:02d} s"
    return f"{s} s"


def spec_factor(unit: str, units: str | None = None) -> tuple[float, str]:
    """(fator divisor, rótulo) para exibir um valor de ParamSpec com unidade ``unit``."""
    u = units or _current
    if u == "in" and unit in LENGTH_UNITS:
        return MM_PER_IN, LENGTH_UNITS[unit]
    return 1.0, unit
