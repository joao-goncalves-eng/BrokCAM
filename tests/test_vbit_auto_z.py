"""Fresa em V: Z de corte automático (derivado da largura do sulco) e travado."""
from __future__ import annotations

import pytest

from brokcam.cam.library import TOOL_ENTRY, LibraryTool
from brokcam.cam.ncc import NCC
from brokcam.cam.isolation import ISOLATION
from brokcam.cam.tools import (VBIT_MIN_DEPTH, vbit_auto_cut_z, vbit_effective_width,
                               vbit_width_from_cut_z)
from brokcam.core.params import ParamGroup, ParamSpec


def test_auto_cut_z_formula_and_minimum():
    # V 30° ponta 0,1: largura 0,2 → profundidade 0,1/(2·tan15°) ≈ 0,1866
    assert vbit_auto_cut_z(30, 0.1, 0.2) == pytest.approx(-0.186603, abs=1e-6)
    assert vbit_effective_width(30, 0.1, vbit_auto_cut_z(30, 0.1, 0.2)) == pytest.approx(0.2)
    # largura ≤ ponta (ou muito pequena) nunca deixa o corte raso demais
    assert vbit_auto_cut_z(30, 0.1, 0.05) == pytest.approx(-VBIT_MIN_DEPTH)
    assert vbit_auto_cut_z(30, 0.1, 0.1) == pytest.approx(-VBIT_MIN_DEPTH)


@pytest.mark.parametrize("group", [ISOLATION, NCC])
def test_cut_z_is_derived_and_user_value_ignored(group):
    v = group.validate({"tool_shape": "vbit", "vbit_angle": 30, "vbit_tip": 0.1,
                        "vbit_width": 0.2, "cut_z": -1.5})
    assert v["cut_z"] == pytest.approx(vbit_auto_cut_z(30, 0.1, 0.2), abs=1e-6)
    assert group.spec("cut_z").is_locked(v)


@pytest.mark.parametrize("group", [ISOLATION, NCC])
def test_flat_tool_keeps_manual_cut_z(group):
    v = group.validate({"tool_shape": "flat", "vbit_width": 0.2, "cut_z": -0.12})
    assert v["cut_z"] == pytest.approx(-0.12)
    assert not group.spec("cut_z").is_locked(v)


def test_old_settings_without_width_keep_their_depth():
    """Projetos/presets antigos só tinham o Z: a largura equivalente é deduzida dele."""
    v = ISOLATION.validate({"tool_shape": "vbit", "vbit_angle": 30, "vbit_tip": 0.1, "cut_z": -0.08})
    assert v["vbit_width"] == vbit_width_from_cut_z(30, 0.1, -0.08)
    assert v["cut_z"] == pytest.approx(-0.08, abs=1e-3)
    # validar de novo o resultado é estável (idempotente)
    assert ISOLATION.validate(v)["cut_z"] == v["cut_z"]


def test_isolation_job_uses_auto_depth():
    import shapely
    from brokcam.core.model import GerberLayer
    from brokcam.cam.isolation import isolate
    layer = GerberLayer(name="t", source_path="", copper=shapely.box(0, 0, 5, 5), traces=[])
    job = isolate(layer, {"tool_shape": "vbit", "vbit_angle": 30, "vbit_tip": 0.1,
                          "vbit_width": 0.25, "cut_z": -0.01})
    op = job.ops[0]
    assert op.params.cut_z == pytest.approx(vbit_auto_cut_z(30, 0.1, 0.25), abs=1e-6)
    assert op.tool.diameter == pytest.approx(0.25, abs=1e-6)


def test_library_vbit_derives_cut_z_and_roundtrips():
    t = LibraryTool(name="V", shape="vbit", vbit_angle=30, vbit_tip=0.1, vbit_width=0.18, cut_z=-2.0)
    assert t.cut_z == pytest.approx(vbit_auto_cut_z(30, 0.1, 0.18), abs=1e-6)
    back = LibraryTool.from_json(t.to_json())
    assert back.cut_z == t.cut_z and back.vbit_width == 0.18
    # biblioteca antiga (sem vbit_width) preserva o Z
    old = {k: v for k, v in t.to_json().items() if k != "vbit_width"}
    old["cut_z"] = -0.07
    legacy = LibraryTool.from_json(old)
    assert legacy.cut_z == pytest.approx(-0.07, abs=1e-3) and legacy.vbit_width is not None
    # o formulário da biblioteca também deriva e trava
    v = TOOL_ENTRY.validate({**t.form_values(), "cut_z": -3.0})
    assert v["cut_z"] == pytest.approx(t.cut_z, abs=1e-6)
    assert TOOL_ENTRY.spec("cut_z").is_locked(v)


def test_flat_library_tool_has_no_width():
    t = LibraryTool(name="F", shape="flat", diameter=0.8, cut_z=-0.1)
    assert t.vbit_width is None and t.cut_z == -0.1
    assert "vbit_width" not in t.form_values()


def test_groups_without_derive_unchanged():
    g = ParamGroup("x", "X", "", (ParamSpec("cut_z", "Z", "float", -0.1, help="h"),))
    assert g.validate({"cut_z": -0.3})["cut_z"] == -0.3
    assert not g.spec("cut_z").is_locked({"cut_z": -0.3})


# --------------------------------------------------------------- correções do QA

def test_depth_is_clamped_to_field_limit():
    """Ângulo muito fechado + largura grande não gera Z além do limite do campo (-10 mm)."""
    from brokcam.cam.common import tool_from
    from brokcam.cam.tools import VBIT_MAX_DEPTH
    v = ISOLATION.validate({"tool_shape": "vbit", "vbit_angle": 5, "vbit_tip": 0.1, "vbit_width": 2.0})
    assert v["cut_z"] == pytest.approx(-VBIT_MAX_DEPTH)
    # a largura efetiva usada no caminho é a que esse Z realmente corta
    assert tool_from(v).diameter == pytest.approx(vbit_effective_width(5, 0.1, -VBIT_MAX_DEPTH))


def test_laser_mode_ignores_vbit_geometry():
    from brokcam.cam.common import tool_from
    v = ISOLATION.validate({"tool_shape": "vbit", "laser_mode": True, "tool_dia": 0.08,
                            "vbit_width": 0.3, "cut_z": -0.05})
    assert not ISOLATION.spec("cut_z").is_locked(v)
    assert v["cut_z"] == pytest.approx(-0.05)
    assert tool_from(v).diameter == pytest.approx(0.08)


def test_saved_settings_without_width_keep_depth(tmp_path):
    """Configurações gravadas antes da mudança (só Z, sem largura) preservam o Z."""
    from brokcam.ui.settings import AppSettings
    s = AppSettings(tmp_path / "settings.json")
    s.data.setdefault("groups", {})["isolation"] = {"tool_shape": "vbit", "vbit_angle": 30,
                                                    "vbit_tip": 0.1, "cut_z": -0.2}
    v = s.group_values(ISOLATION)
    assert v["cut_z"] == pytest.approx(-0.2, abs=1e-3)
    assert v["vbit_width"] == vbit_width_from_cut_z(30, 0.1, -0.2)
