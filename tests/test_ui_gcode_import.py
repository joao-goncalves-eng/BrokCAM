"""Interface: abrir G-code como caminhos editáveis + geometria (e salvar/reabrir no .bcam)."""
from __future__ import annotations

import os
import sys
import tempfile
import time
from pathlib import Path

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
if sys.platform == "win32":
    os.environ.setdefault("QT_QPA_FONTDIR", r"C:\Windows\Fonts")
os.environ.setdefault("BROKCAM_CONFIG_DIR", tempfile.mkdtemp(prefix="brokcam-test-"))

import numpy as np  # noqa: E402
import pytest  # noqa: E402
from PySide6.QtCore import QEventLoop, QTimer  # noqa: E402

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))
DATA = ROOT / "tests" / "data"


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


def wait_until(cond, timeout: float = 60.0) -> bool:  # type: ignore[no-untyped-def]
    t0 = time.time()
    while time.time() - t0 < timeout:
        if cond():
            return True
        wait(20)
    return cond()


@pytest.fixture()
def gcode_files(tmp_path):
    from brokcam.cam.cutout import cutout
    from brokcam.cam.drilling import drill
    from brokcam.cam.isolation import isolate
    from brokcam.gcode.grbl import generate_gcode
    from brokcam.parsers import load_file
    out = []
    for name, fn, src in (("board-F_Cu.nc", isolate, "board-F_Cu.gbr"), ("board-PTH.nc", drill, "board-PTH.drl"),
                          ("board-Edge_Cuts.nc", cutout, "board-Edge_Cuts.gbr")):
        p = tmp_path / name
        p.write_text(generate_gcode(fn(load_file(str(DATA / src)), {}), {}))
        out.append(str(p))
    return out


@pytest.fixture()
def win(qapp, tmp_path):
    from brokcam.ui.main_window import MainWindow
    from brokcam.ui.settings import AppSettings
    w = MainWindow(AppSettings(tmp_path / "settings.json"))
    w.resize(1400, 860)
    w.show()
    yield w
    w.project.set_modified(False)
    w.machine.shutdown()
    w.close()
    w.deleteLater()
    wait(10)


def test_open_gcode_files_become_editable_jobs_grouped(win, gcode_files, tmp_path):
    win.open_files(gcode_files)
    assert wait_until(lambda: len(win.project.jobs) == 3 and not win.runner.busy())
    assert len(win.project.layers) == 3 and all(e.kind == "geometry" for e in win.project.layers)
    assert len({e.group for e in win.project.layers}) == 1 and win.project.layers[0].group is not None
    roles = {os.path.basename(e.name).split(" ")[0]: e.role for e in win.project.layers}
    assert roles["board-PTH.nc"] == "drill"
    j_iso = next(j for j in win.project.jobs if "F_Cu" in j.name)
    assert j_iso.job.settings["imported"] and j_iso.job.gcode == Path(gcode_files[0]).read_text()
    assert j_iso.estimate_s > 0
    # um Ctrl+Z desfaz a importação inteira
    win.undo.undo()
    assert not win.project.jobs and not win.project.layers
    win.undo.redo()
    assert len(win.project.jobs) == 3
    # editar: apagar um caminho descarta o texto original e o G-code passa a ser regerado
    n0 = len(j_iso.job.ops[0].paths)
    win._on_edit_action(j_iso.id, "delete", {(0, "path", 0)})
    assert len(j_iso.job.ops[0].paths) == n0 - 1 and not j_iso.job.gcode
    win.generate_gcode(j_iso.id)
    assert wait_until(lambda: bool(j_iso.job.gcode) and not win.runner.busy())
    assert "BrokCAM" in j_iso.job.gcode
    # mover o grupo leva os caminhos junto
    before = np.vstack(j_iso.job.ops[0].paths)
    from brokcam.ui.project import Transform
    win._on_layer_transform(j_iso.source_id, Transform(None, None, (3.0, 1.0), 0.0))
    assert np.allclose(np.vstack(j_iso.job.ops[0].paths), before + [3.0, 1.0])
    # salvar e reabrir: caminhos e geometria voltam iguais
    path = str(tmp_path / "importado.bcam")
    assert win._write_project(path)
    saved = {j.name: np.vstack([np.vstack(op.paths) if op.paths else op.points for op in j.job.ops])
             for j in win.project.jobs}
    win.open_project(path)
    assert wait_until(lambda: len(win.project.jobs) == 3 and not win.runner.busy())
    assert len(win.project.layers) == 3
    for j in win.project.jobs:
        got = np.vstack([np.vstack(op.paths) if op.paths else op.points for op in j.job.ops])
        assert np.allclose(got, saved[j.name], atol=1e-4)


def test_view_only_keeps_old_behavior(win, gcode_files, monkeypatch):
    from PySide6.QtWidgets import QFileDialog
    monkeypatch.setattr(QFileDialog, "getOpenFileName", lambda *a, **k: (gcode_files[1], ""))
    win.a_view_gcode.trigger()
    wait(50)
    assert not win.project.jobs and "G0" in win.gcode_panel.text()


def test_import_error_is_reported(win, tmp_path, monkeypatch):
    from PySide6.QtWidgets import QMessageBox
    bad = tmp_path / "vazio.nc"
    bad.write_text("G0 X1 Y1\n")
    shown = []
    monkeypatch.setattr(QMessageBox, "warning", lambda *a, **k: shown.append(a[2]))
    win.import_gcode_files([str(bad)])
    assert wait_until(lambda: not win.runner.busy() and bool(shown))
    assert "vazio.nc" in shown[0] and not win.project.jobs


def test_imported_params_table_edits_ops_with_undo(win, gcode_files):
    win.import_gcode_files([gcode_files[2]])                       # recorte (multiprofundidade)
    assert wait_until(lambda: len(win.project.jobs) == 1 and not win.runner.busy())
    j = win.project.jobs[0]
    win.project_panel.select("job", j.id)
    wait(30)
    t = win.props.imported_table
    assert t is not None and t.columnCount() == len(j.job.ops) == 1 and t.rowCount() == 6
    assert t.item(1, 0).text() == "-1,7"                          # Z corte lido do arquivo
    assert t.item(5, 0).text() == "10000" and t.item(3, 0).text() == "120"
    t.item(3, 0).setText("300")                                    # avanço XY
    assert wait_until(lambda: j.job.ops[0].params.feed_xy == 300.0)
    assert not j.job.gcode                                         # passa a ser regerado
    t = win.props.imported_table
    t.item(0, 0).setText("2,0")                                    # Ø da fresa
    assert wait_until(lambda: j.job.ops[0].tool.diameter == 2.0)
    win.undo.undo()
    assert j.job.ops[0].tool.diameter == 1.0
    win.undo.undo()
    assert j.job.ops[0].params.feed_xy == 120.0 and j.job.gcode   # texto original de volta
