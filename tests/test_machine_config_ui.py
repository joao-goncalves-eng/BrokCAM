"""Aba de configurações da máquina (offscreen) ligada ao simulador MKS DLC32."""
from __future__ import annotations

import json
import math
import os
import sys
import tempfile
import time
from pathlib import Path

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
if sys.platform == "win32":
    os.environ.setdefault("QT_QPA_FONTDIR", r"C:\Windows\Fonts")
os.environ.setdefault("BROKCAM_CONFIG_DIR", tempfile.mkdtemp(prefix="brokcam-cfg-test-"))

import pytest  # noqa: E402

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from PySide6.QtCore import QEventLoop, QTimer  # noqa: E402

SHOTS = ROOT / "docs" / "screenshots"


@pytest.fixture(scope="module")
def qapp():
    from brokcam.app import create_app
    from brokcam.ui import theme
    app = create_app([])
    theme.manager().configure(mode="dark", high_contrast=False)
    yield app


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


def _setup(firmware: str = "mks_dlc32"):  # type: ignore[no-untyped-def]
    from brokcam.machine import GrblController, GrblSimulator
    from brokcam.ui.machine_config import MachineConfigPanel
    from brokcam.ui.machine_panel import GrblBridge
    bridge = GrblBridge()
    ctl = GrblController(status_interval=0.05)
    bridge.attach(ctl)
    sim = GrblSimulator(speed=math.inf, firmware=firmware)
    panel = MachineConfigPanel(bridge)
    panel.resize(1280, 760)
    panel.show()
    ctl.connect("SIMULADOR-DLC32" if firmware != "grbl" else "SIMULADOR", transport=sim)
    return panel, ctl, sim


def _writes(sim) -> list[str]:  # type: ignore[no-untyped-def]
    return [ln.strip() for ln in sim.executed_lines if ln.strip().startswith("$") and "=" in ln
            and not ln.strip().startswith("$J=")]


def test_panel_reads_status_and_settings(qapp):
    panel, ctl, sim = _setup()
    try:
        assert wait_until(lambda: panel.connected and bool(panel.rows) and not panel.busy)
        assert panel.fw.kind == "mks_dlc32"
        assert "MKS DLC32" in panel.fw_title.text()
        assert "110" in panel.rows and "Sta/SSID" in panel.rows and "36" in panel.rows
        assert "Stepper/Pulse" not in panel.rows                 # alias de $0 não duplica
        assert "Rede / WiFi" in panel.cat_items
        assert panel.rows["3"].kind == "mask" and panel.rows["Radio/Mode"].kind == "enum"
        assert panel.rows["Sta/Password"].kind == "password"
        assert wait_until(lambda: panel.st_pos["MPos"][0].text() != "—")
        assert "G54" in panel.params and panel.parser_words
        assert panel.net["ip"].text() == "192.168.4.1"
        assert panel.url_edit.text() == "tcp://192.168.4.1:23"
        assert "livre" in panel.st_pins["X"].text()
        # nada alterado ainda
        assert panel.changes() == [] and not panel.write_btn.isEnabled()
        # ajuda contextual ao focar
        panel.tabs.setCurrentIndex(1)
        panel._focus_row(panel.rows["110"])
        assert "$110" in panel.help.title.text() and "Valor típico" in panel.help.body.text()
        # filtro
        panel.filter_edit.setText("wifi")
        assert not panel.cat_items["Rede / WiFi"].isHidden()
        assert panel.cat_items["Homing"].isHidden()
        panel.filter_edit.clear()
        # comparar com o perfil de referência
        panel.set_compare(True)
        assert panel.rows["110"].item.text(4).startswith("≠")
        assert panel.rows["0"].item.text(4).startswith("=")
    finally:
        ctl.disconnect()
        panel.close()


def test_edit_and_write_only_changed(qapp, tmp_path):
    panel, ctl, sim = _setup()
    try:
        assert wait_until(lambda: bool(panel.rows) and not panel.busy)
        before = len(_writes(sim))
        assert panel.set_new_value("110", "1500")
        assert panel.rows["110"].is_modified() and panel.write_btn.isEnabled()
        assert panel.changes() == [("110", "1500")]
        # valor inválido bloqueia a gravação
        panel.set_new_value("AP/IP", "300.1.1.1")
        assert panel.rows["AP/IP"].error and not panel.write_btn.isEnabled()
        panel.set_new_value("AP/IP", "192.168.4.1")
        assert not panel.rows["AP/IP"].is_modified()
        panel.confirm_writes = False
        panel.write_changes()
        assert wait_until(lambda: not panel.busy and sim.settings[110] == 1500.0)
        assert _writes(sim)[before:] == ["$110=1500"]            # só a configuração alterada
        assert wait_until(lambda: panel.rows["110"].machine == "1500")
        assert not panel.rows["110"].is_modified()
        # estendida (WiFi)
        panel.set_new_value("Sta/SSID", "Oficina 2")
        panel.write_changes()
        assert wait_until(lambda: not panel.busy and sim.ext["Sta/SSID"] == "Oficina 2")
        assert _writes(sim)[-1] == "$Sta/SSID=Oficina 2"
        # backup / restauração (carrega nos editores, não grava)
        path = tmp_path / "bk.json"
        panel.save_backup(str(path))
        data = json.loads(path.read_text(encoding="utf-8"))
        assert data["numbered"]["110"] == "1500" and "Sta/Password" not in data["extended"]
        data["numbered"]["111"] = "2500"
        path.write_text(json.dumps(data), encoding="utf-8")
        n_writes = len(_writes(sim))
        applied, missing = panel.load_backup(str(path))
        assert applied > 30 and not missing
        assert [k for k, _ in panel.changes()] == ["111"]
        assert len(_writes(sim)) == n_writes
        panel.discard_changes()
        assert panel.changes() == []
        # bloqueado durante envio de programa
        panel.set_new_value("112", "500")
        panel._set_streaming(True)
        assert not panel.write_btn.isEnabled()
        panel._set_streaming(False)
        assert panel.write_btn.isEnabled()
    finally:
        ctl.disconnect()
        panel.close()


def test_disconnected_and_classic_grbl(qapp):
    from brokcam.machine import GrblController
    from brokcam.ui.machine_config import MachineConfigPanel
    from brokcam.ui.machine_panel import GrblBridge
    bridge = GrblBridge()
    bridge.attach(GrblController())
    panel = MachineConfigPanel(bridge)
    panel.show()
    wait(50)
    assert panel.banner.isVisible() and not panel.refresh_btn.isEnabled()
    assert not panel.write_btn.isEnabled()
    panel.close()

    panel, ctl, sim = _setup("grbl")
    try:
        assert wait_until(lambda: bool(panel.rows) and not panel.busy)
        assert panel.fw.kind == "grbl"
        assert "Rede / WiFi" not in panel.cat_items
        assert "não tem WiFi" in panel.net_note.text()
        assert not panel.restart_btn.isEnabled()
    finally:
        ctl.disconnect()
        panel.close()


@pytest.mark.parametrize("mode", ["dark", "light"])
def test_machine_config_screenshots(qapp, mode):
    from brokcam.ui import theme
    theme.manager().configure(mode=mode, high_contrast=False)
    panel, ctl, sim = _setup()
    try:
        assert wait_until(lambda: bool(panel.rows) and not panel.busy)
        ctl.jog(dx=12.5, dy=7.25, feed=1000, wait=True)
        ctl.set_work_zero("XY")
        panel.refresh_all()
        assert wait_until(lambda: not panel.busy)
        wait_until(lambda: panel.st_pos["WCO"][0].text() not in ("—", "0,000"), 3)
        panel.resize(1360, 820)
        SHOTS.mkdir(parents=True, exist_ok=True)
        panel.tabs.setCurrentIndex(0)
        wait(120)
        assert panel.grab().save(str(SHOTS / f"machine_config_status_{mode}.png"))
        panel.tabs.setCurrentIndex(1)
        panel.set_compare(True)
        panel.set_new_value("110", "3000")
        panel.set_new_value("1", "255")
        panel._focus_row(panel.rows["110"])
        wait(120)
        assert panel.grab().save(str(SHOTS / f"machine_config_{mode}.png"))
        panel.tabs.setCurrentIndex(2)
        wait(120)
        assert panel.grab().save(str(SHOTS / f"machine_config_network_{mode}.png"))
    finally:
        ctl.disconnect()
        panel.close()
        theme.manager().configure(mode="dark", high_contrast=False)
