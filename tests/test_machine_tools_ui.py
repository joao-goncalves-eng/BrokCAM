"""Painel "Comandos e sonda": posição, offset Z, heightmap e macros com o simulador."""
from __future__ import annotations

import math
import os
import sys
import tempfile
import time

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
if sys.platform == "win32":
    os.environ.setdefault("QT_QPA_FONTDIR", r"C:\Windows\Fonts")
os.environ.setdefault("BROKCAM_CONFIG_DIR", tempfile.mkdtemp(prefix="brokcam-test-"))

import numpy as np  # noqa: E402
import pytest  # noqa: E402
from PySide6.QtCore import QEventLoop, QTimer  # noqa: E402


@pytest.fixture(scope="module")
def qapp():
    from brokcam.app import create_app
    app = create_app([])
    yield app


def wait(ms: int) -> None:
    loop = QEventLoop()
    QTimer.singleShot(ms, loop.quit)
    loop.exec()


def wait_until(cond, timeout: float = 20.0) -> bool:  # type: ignore[no-untyped-def]
    t0 = time.time()
    while time.time() - t0 < timeout:
        if cond():
            return True
        wait(20)
    return cond()


def surface(x, y):
    return -20.0 + 0.005 * x - 0.004 * y


@pytest.fixture()
def setup(qapp, tmp_path):
    from brokcam.machine import GrblController, GrblSimulator
    from brokcam.ui.machine_panel import GrblBridge
    from brokcam.ui.machine_tools import MachineToolsPanel
    from brokcam.ui.settings import AppSettings
    ctl = GrblController()
    bridge = GrblBridge()
    bridge.attach(ctl)
    sim = GrblSimulator(speed=math.inf)
    sim.probe_surface = surface
    program = {"text": "G21 G90\nG0 Z1\nG0 X0 Y0\nG1 Z-0.05 F60\nG1 X30 Y0 F120\nG1 X30 Y20\nG0 Z1\nM2\n",
               "name": "teste"}
    panel = MachineToolsPanel(bridge, AppSettings(tmp_path / "s.json"),
                              get_program=lambda: (program["text"], program["name"]),
                              get_board_bounds=lambda: (0.0, 0.0, 30.0, 20.0))
    panel.resize(1100, 500)
    panel.show()
    changed = []
    panel.programChanged.connect(lambda t, n: (changed.append((t, n)), program.update(text=t, name=n)))
    ctl.connect("SIMULADOR", transport=sim)
    assert wait_until(lambda: panel.connected)
    yield panel, ctl, sim, changed
    ctl.disconnect()
    panel.close()
    panel.deleteLater()
    wait(10)


def test_set_position_and_zero_buttons(setup):
    panel, ctl, sim, _ = setup
    panel.set_val["X"].setValue(5)
    panel.set_val["Y"].setValue(6)
    panel.set_val["Z"].setValue(7)
    panel._set_pos()
    assert wait_until(lambda: "concluído" in panel.banner.text())
    ctl.send_command("?", wait=False)
    assert wait_until(lambda: ctl.status is not None and ctl.status.wpos is not None
                      and abs(ctl.status.wpos[0] - 5) < 1e-3 and abs(ctl.status.wpos[2] - 7) < 1e-3)


def test_fine_z_adjust_accumulates(setup):
    panel, ctl, sim, _ = setup
    ctl.set_position(z=0)
    ctl.send_command("?", wait=False)
    assert wait_until(lambda: ctl.status is not None and ctl.status.wpos is not None)
    panel._adjust_z(0.05)
    assert wait_until(lambda: abs(panel.z_adjust_total - 0.05) < 1e-9)
    panel._adjust_z(-0.01)
    assert wait_until(lambda: abs(panel.z_adjust_total - 0.04) < 1e-9)
    assert "mais fundo" in panel.z_total.text()


def test_heightmap_probe_and_apply(setup):
    panel, ctl, sim, changed = setup
    ctl.set_position(x=0, y=0, z=5)                    # ponta 5 mm acima do zero declarado
    panel._area_from("board")
    panel.hm_spacing.setValue(10)
    panel.hm_clear.setValue(2)
    panel.hm_depth.setValue(25)
    panel.hm_feed.setValue(300)
    panel._start_grid()
    assert wait_until(lambda: not panel._probing, 30)
    assert panel.hm is not None and panel.hm.complete and panel.hm.z.shape == (3, 4)
    off = ctl.work_offset(fresh=True)
    exp = surface(panel.hm.xs[None, :] + off[0], panel.hm.ys[:, None] + off[1]) - off[2]
    assert np.allclose(panel.hm.z, exp, atol=0.01)
    assert panel.hm.stats()["range"] == pytest.approx(0.15 + 0.08, abs=0.02)
    panel._apply()
    assert changed and "(BrokCAM autolevel" in changed[-1][0]
    assert changed[-1][1].endswith("(autolevel)")
    panel._unapply()
    assert "(BrokCAM autolevel" not in changed[-1][0]


def test_heightmap_cancel(setup):
    panel, ctl, sim, _ = setup
    ctl.set_position(x=0, y=0, z=5)
    panel._area_from("board")
    panel.hm_spacing.setValue(3)
    panel.hm_depth.setValue(25)
    panel._start_grid()
    panel._cancel_grid()
    assert wait_until(lambda: not panel._probing, 30)
    assert "Falhou" in panel.banner.text() or panel.hm.complete


def test_macros_persist_and_run(setup, tmp_path):
    panel, ctl, sim, _ = setup
    n = len(panel.macros)
    panel.macros.append({"name": "Teste", "gcode": "G90\nG0 Z3 (sobe)"})
    panel._save_macros()
    panel._fill_macros(n)
    panel._run_macro()
    assert wait_until(lambda: "concluído" in panel.banner.text())
    assert panel.settings.get("machine_macros")[-1]["name"] == "Teste"


def test_commands_blocked_when_disconnected(setup):
    panel, ctl, sim, _ = setup
    ctl.disconnect()
    assert wait_until(lambda: not panel.connected)
    assert not panel.hm_start.isEnabled()
    panel._adjust_z(0.1)
    assert "Conecte-se" in panel.banner.text()
