"""Gera screenshots da interface (offscreen) em docs/screenshots/.

Executado como teste: verifica que cada tela renderiza e grava o PNG para revisão
visual do design nos temas escuro e claro.
"""
from __future__ import annotations

import os
import sys
import tempfile
import time
from pathlib import Path

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
if sys.platform == "win32":
    os.environ.setdefault("QT_QPA_FONTDIR", r"C:\Windows\Fonts")
os.environ.setdefault("BROKCAM_CONFIG_DIR", tempfile.mkdtemp(prefix="brokcam-shots-"))

import pytest  # noqa: E402

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from PySide6.QtCore import QEventLoop, QTimer  # noqa: E402

OUT = ROOT / "docs" / "screenshots"


def wait(ms: int) -> None:
    loop = QEventLoop()
    QTimer.singleShot(ms, loop.quit)
    loop.exec()


def wait_until(cond, timeout: float = 60.0) -> bool:  # type: ignore[no-untyped-def]
    t0 = time.time()
    while time.time() - t0 < timeout:
        if cond():
            return True
        wait(25)
    return cond()


@pytest.fixture(scope="module")
def qapp():
    from brokcam.app import create_app
    return create_app([])


def _window(mode: str):  # type: ignore[no-untyped-def]
    from brokcam.ui import theme
    from brokcam.ui.main_window import MainWindow
    from brokcam.ui.settings import AppSettings
    s = AppSettings()
    s.data["window"] = {}
    s.set_ui("theme", mode)
    s.set_ui("high_contrast", False)
    theme.manager().configure(mode=mode, high_contrast=False)
    win = MainWindow(s)
    win.resize(1600, 960)
    win.show()
    wait(100)
    return win


def _populate(win) -> None:  # type: ignore[no-untyped-def]
    from brokcam.ui import backend
    win.open_example()
    assert wait_until(lambda: win.project.layers and not win.runner.busy())
    if not backend.status()["cam"]:
        return
    wanted = {"top_copper": "isolation", "drill": "drilling", "outline": "cutout"}
    done_roles: set[str] = set()
    for e in list(win.project.layers):
        op = wanted.get(e.role)
        if op is None or (e.role == "drill" and "drill" in done_roles):
            continue
        win.project_panel.select("layer", e.id)
        wait(20)
        win.props._select_op(op)
        n = len(win.project.jobs)
        win.props._primary_clicked()
        assert wait_until(lambda: len(win.project.jobs) > n and not win.runner.busy(), 120)
        done_roles.add(e.role)
    iso = next((j for j in win.project.jobs if j.job.operation == "isolation"), None)
    if iso is not None and backend.status()["gcode"]:
        win.project_panel.select("job", iso.id)
        win.props._primary_clicked()
        assert wait_until(lambda: bool(iso.job.gcode) and not win.runner.busy())


def _close(win) -> None:  # type: ignore[no-untyped-def]
    win.project.set_modified(False)
    win.machine.shutdown()
    win.close()
    win.deleteLater()
    wait(10)


@pytest.mark.parametrize("mode", ["dark", "light"])
def test_screenshots(qapp, mode):
    OUT.mkdir(parents=True, exist_ok=True)
    win = _window(mode)
    assert win.grab().save(str(OUT / f"welcome_{mode}.png"))
    _populate(win)
    top = next((e for e in win.project.layers if e.role == "top_copper"), None)
    if top is not None:
        win.project_panel.select("layer", top.id)
    win._show_dock(win.dock_gcode)
    win.canvas.fit_all()
    wait(150)
    assert win.grab().save(str(OUT / f"main_{mode}.png"))

    # caminho selecionado + simulação parcial
    iso = next((j for j in win.project.jobs if j.job.operation == "isolation"), None)
    if iso is not None:
        win.project_panel.select("job", iso.id)
        win.simulate_job(iso.id)
        win.canvas.sim_toggle()
        win.canvas.sim_seek(win.canvas.sim.total * 0.55 if win.canvas.sim else 0)
        wait(100)
        assert win.grab().save(str(OUT / f"simulation_{mode}.png"))
        win.canvas.sim_stop()

    # aba Máquina conectada ao simulador interno
    from brokcam.ui import backend
    if backend.machine_available():
        win._show_dock(win.dock_machine)
        win.machine.port.setCurrentText(backend.SIMULATOR_PORT)
        win.machine.toggle_connection()
        if wait_until(lambda: win.machine.connected, 15):
            wait_until(lambda: win.machine.dro_w[0].text() != "—", 5)
            if iso is not None and iso.job.gcode:
                win.send_job(iso.id)
            wait(300)
        assert win.grab().save(str(OUT / f"machine_{mode}.png"))
    _close(win)


def test_dialog_screenshots(qapp):
    from brokcam.ui import theme
    from brokcam.ui.help_dialogs import OptionsManual
    from brokcam.ui.preferences import PreferencesDialog
    from brokcam.ui.settings import AppSettings
    OUT.mkdir(parents=True, exist_ok=True)
    theme.manager().configure(mode="dark", high_contrast=False)
    dlg = PreferencesDialog(AppSettings(), start_key="isolation")
    dlg.resize(1000, 700)
    dlg.show()
    wait(100)
    assert dlg.grab().save(str(OUT / "preferences_dark.png"))
    dlg.close()
    dlg.deleteLater()
    theme.manager().configure(mode="light")
    man = OptionsManual()
    man.resize(1100, 720)
    man.show()
    wait(100)
    assert man.grab().save(str(OUT / "manual_light.png"))
    man.close()
    man.deleteLater()
    theme.manager().configure(mode="dark", high_contrast=True)
    win = _window("dark")
    win.settings.set_ui("high_contrast", True)
    theme.manager().configure(high_contrast=True)
    _populate(win)
    win.canvas.fit_all()
    wait(100)
    assert win.grab().save(str(OUT / "main_high_contrast.png"))
    _close(win)
    theme.manager().configure(mode="dark", high_contrast=False)
