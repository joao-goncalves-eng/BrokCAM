"""Fins de curso: pré-teste do homing, proteção por software e aba de diagnóstico."""
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

import pytest  # noqa: E402
from PySide6.QtCore import QEventLoop, QTimer  # noqa: E402

from brokcam.machine.grbl import GrblController, GrblError  # noqa: E402
from brokcam.machine.simulator import GrblSimulator  # noqa: E402


def _connect(**kw):
    sim = GrblSimulator(speed=kw.pop("speed", math.inf), homing_enabled=kw.pop("homing", True))
    for k, v in kw.items():
        setattr(sim, k, v)
    c = GrblController()
    c.connect("SIMULADOR", transport=sim)
    c.wait_status(2.0)
    return c, sim


def test_homing_refused_when_switches_read_active_at_rest():
    c, sim = _connect(limit_pins="XYZ")
    try:
        assert c.limit_pins() == "XYZ"
        assert c.homing_preflight()
        with pytest.raises(GrblError) as ei:
            c.home()
        assert "$5=1" in str(ei.value) and "segurança" in str(ei.value)
        assert "$H" not in sim.executed_lines
    finally:
        c.disconnect()


def test_homing_allowed_when_switches_ok():
    c, sim = _connect()
    try:
        assert c.homing_preflight() == []
        c.home(timeout=10)
    finally:
        c.disconnect()


def test_software_guard_stops_on_new_limit_during_move():
    # a chave X é acionada quando o eixo passa de X=5 (simula bater no fim de curso)
    c, sim = _connect(speed=1.0, homing=False, limit_pins_fn=lambda mp: "X" if mp[0] > 5 else "")
    msgs = []
    c.on_message = msgs.append
    try:
        c.wait_status(1.0)
        c.send_command("G91 G1 X50 F1200", wait=True)
        t0 = time.time()
        while time.time() - t0 < 5 and not msgs:
            time.sleep(0.02)
        assert msgs and "parada pelo BrokCAM" in msgs[0]
        time.sleep(0.5)
        assert c.status is not None and c.status.mpos[0] < 40       # não percorreu os 50 mm
    finally:
        c.disconnect()


def test_guard_ignores_switches_already_active_at_rest():
    c, sim = _connect(speed=500.0, homing=False, limit_pins="Z")
    msgs = []
    c.on_message = msgs.append
    try:
        c.wait_status(1.0)
        c.send_command("G91 G1 X5 F3000", wait=True)
        c.wait_idle(10)
        assert not [m for m in msgs if "parada pelo BrokCAM" in m]
    finally:
        c.disconnect()


# --------------------------------------------------------------------------- interface

@pytest.fixture(scope="module")
def qapp():
    from brokcam.app import create_app
    yield create_app([])


def wait(ms: int) -> None:
    loop = QEventLoop()
    QTimer.singleShot(ms, loop.quit)
    loop.exec()


def wait_until(cond, timeout: float = 15.0) -> bool:  # type: ignore[no-untyped-def]
    t0 = time.time()
    while time.time() - t0 < timeout:
        if cond():
            return True
        wait(20)
    return cond()


def test_limits_tab_diagnoses_and_fixes_invert(qapp, tmp_path, monkeypatch):
    from PySide6.QtWidgets import QMessageBox
    from brokcam.ui.machine_panel import GrblBridge
    from brokcam.ui.machine_tools import MachineToolsPanel
    from brokcam.ui.settings import AppSettings
    monkeypatch.setattr(QMessageBox, "question", staticmethod(lambda *a, **k: QMessageBox.StandardButton.Yes))
    ctl = GrblController()
    bridge = GrblBridge()
    bridge.attach(ctl)
    sim = GrblSimulator(speed=math.inf, homing_enabled=True)
    sim.limit_pins = "XYZ"
    panel = MachineToolsPanel(bridge, AppSettings(tmp_path / "s.json"), lambda: ("", ""), lambda: None)
    panel.show()
    try:
        ctl.connect("SIMULADOR", transport=sim)
        assert wait_until(lambda: panel.connected and panel._limit_settings)
        assert wait_until(lambda: "ACIONADA" in panel.pin_chips["X"].text())
        assert "$5" in panel.limit_diag.text() and "ACIONADAS" in panel.limit_diag.text()
        panel._toggle_invert()
        assert wait_until(lambda: panel._limit_settings.get(5) == 1)
        sim.limit_pins = ""                              # com $5=1 as chaves passam a ler soltas
        assert wait_until(lambda: "solta" in panel.pin_chips["X"].text())
        assert "Nenhuma chave" in panel.limit_diag.text()
    finally:
        ctl.disconnect()
        panel.close()
        panel.deleteLater()
        wait(10)
