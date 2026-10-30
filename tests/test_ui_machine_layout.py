"""Interface: painéis independentes do espaço Máquina, travar painéis, layout salvo e parada segura."""
from __future__ import annotations

import os
import sys
import tempfile
import time

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
if sys.platform == "win32":
    os.environ.setdefault("QT_QPA_FONTDIR", r"C:\Windows\Fonts")
os.environ.setdefault("BROKCAM_CONFIG_DIR", tempfile.mkdtemp(prefix="brokcam-test-"))

import pytest  # noqa: E402
from PySide6.QtCore import QEventLoop, Qt, QTimer  # noqa: E402


@pytest.fixture(scope="module")
def qapp():
    from brokcam.app import create_app
    from brokcam.ui import theme
    app = create_app([])
    theme.manager().configure(mode="dark")
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


def make_win(path):  # type: ignore[no-untyped-def]
    from brokcam.ui.main_window import MainWindow
    from brokcam.ui.settings import AppSettings
    w = MainWindow(AppSettings(path))
    w.resize(1500, 900)
    w.show()
    wait(50)
    return w


def close_win(w) -> None:  # type: ignore[no-untyped-def]
    w.project.set_modified(False)
    w.close()
    w.deleteLater()
    wait(10)


def test_machine_workspace_uses_separate_docks(qapp, tmp_path):
    w = make_win(tmp_path / "s.json")
    try:
        assert all(not d.isVisible() for d in w.machine_docks)
        w.set_workspace("machine")
        wait(20)
        R, B = Qt.DockWidgetArea.RightDockWidgetArea, Qt.DockWidgetArea.BottomDockWidgetArea
        assert w.dockWidgetArea(w.dock_machine) == R and w.dockWidgetArea(w.dock_jog) == R
        assert w.dockWidgetArea(w.dock_program) == B
        group = set(w.tabifiedDockWidgets(w.dock_console))
        assert {w.dock_tools, w.dock_machine_cfg, w.dock_log, w.dock_gcode, w.dock_grbl} <= group
        assert w.dock_program not in group            # programa fica sempre visível ao lado
        # cada seção da máquina está no seu painel
        assert w.dock_jog.widget().widget() is w.machine.jog_panel
        assert w.dock_console.widget() is w.machine.console_panel
        assert w.machine.tabs is None
        # todos podem ser arrastados e encaixados em qualquer borda
        for d in w.machine_docks:
            assert d.allowedAreas() == Qt.DockWidgetArea.AllDockWidgetAreas
            assert d.features() & d.DockWidgetFeature.DockWidgetMovable
    finally:
        close_win(w)


def test_lock_docks_and_layout_is_remembered(qapp, tmp_path):
    path = tmp_path / "s.json"
    w = make_win(path)
    try:
        w.set_workspace("machine")
        w.a_lock_docks.trigger()
        assert w.a_lock_docks.isChecked()
        for d in w.all_docks:
            assert not d.features() & d.DockWidgetFeature.DockWidgetMovable
            assert d.features() & d.DockWidgetFeature.DockWidgetClosable
        assert not w.dock_jog.titleBarWidget().float_btn.isVisible()
        # o usuário move o jog para a esquerda; o arranjo do espaço Máquina é lembrado
        w.a_lock_docks.trigger()
        w.addDockWidget(Qt.DockWidgetArea.LeftDockWidgetArea, w.dock_jog)
        w.a_lock_docks.trigger()
        w.set_workspace("prepare")
        assert not w.dock_jog.isVisible() and w.dock_props.isVisible()
        w.set_workspace("machine")
        assert w.dockWidgetArea(w.dock_jog) == Qt.DockWidgetArea.LeftDockWidgetArea
    finally:
        close_win(w)
    w2 = make_win(path)
    try:
        assert w2.a_lock_docks.isChecked() or wait_until(lambda: w2.a_lock_docks.isChecked(), 2)
        w2.set_workspace("machine")
        assert w2.dockWidgetArea(w2.dock_jog) == Qt.DockWidgetArea.LeftDockWidgetArea
        # restaurar layout padrão volta o jog para a direita
        w2.set_docks_locked(False)
        w2.reset_layout()
        assert w2.dockWidgetArea(w2.dock_jog) == Qt.DockWidgetArea.RightDockWidgetArea
    finally:
        close_win(w2)


def test_stop_program_is_safe_and_resume_from_line(qapp, tmp_path, monkeypatch):
    """Parar usa a parada segura (sem ALARM:3) e Iniciar da linha retoma com o estado restaurado."""
    import math
    from brokcam.machine import GrblController, GrblSimulator
    from PySide6.QtWidgets import QMessageBox
    from brokcam.ui.machine_panel import MachinePanel
    monkeypatch.setattr(QMessageBox, "exec", lambda self: QMessageBox.StandardButton.Yes)
    ctrl = GrblController(status_interval=0.05)
    sim = GrblSimulator(speed=1.0)
    panel = MachinePanel(controller=ctrl, confirm_send=False, split=True)
    try:
        ctrl.connect("SIMULADOR", transport=sim)
        assert wait_until(lambda: panel.connected and panel.state == "Idle", 10)
        alarms = []
        panel.bridge.alarm.connect(lambda c, m: alarms.append(c))
        prog = ["G21 G90", "M3 S1000", "G1 F300", "G1 X30", "G1 Y30", "G1 X0"]
        panel.load_program("\n".join(prog), "teste")
        panel.start_program()
        assert wait_until(lambda: sim.state == "Run" and sim.mpos[0] > 0.5, 10)
        panel.stop_program()
        assert not panel.streaming
        assert wait_until(lambda: panel.state == "Idle", 10)
        wait(300)
        assert alarms == []                               # posição preservada
        sim.speed = math.inf
        panel.start_program(5)                            # retoma em "G1 Y30"
        assert wait_until(lambda: not panel.streaming, 10)
        ex = [ln for ln in sim.executed_lines if not ln.startswith("$")]
        assert "G0 X30" in ex and ex[-1] == "G1 X0"
    finally:
        panel.shutdown()
        panel.deleteLater()
