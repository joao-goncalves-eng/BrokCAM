"""Interface: furação com um trabalho por broca, tabela de diâmetros e cores por diâmetro."""
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


def _excellon(path: Path) -> Path:
    rng = np.random.default_rng(1)
    dias = {1: 0.6, 2: 0.8, 3: 0.9, 4: 1.0}
    lines = ["M48", "METRIC,TZ"] + [f"T{n}C{d:.3f}" for n, d in dias.items()] + ["%", "G90", "G05"]
    for n in dias:
        lines.append(f"T{n}")
        for x, y in rng.random((5 + n, 2)) * [40, 25]:
            lines.append(f"X{x:.3f}Y{y:.3f}")
    lines.append("M30")
    path.write_text("\n".join(lines) + "\n")
    return path


@pytest.fixture()
def win(qapp, tmp_path):
    from brokcam.ui.main_window import MainWindow
    from brokcam.ui.settings import AppSettings
    w = MainWindow(AppSettings(tmp_path / "settings.json"))
    w.resize(1400, 860)
    w.show()
    w.open_files([str(_excellon(tmp_path / "multi.drl"))])
    assert wait_until(lambda: len(w.project.layers) == 1 and not w.runner.busy())
    yield w
    w.project.set_modified(False)
    w.machine.shutdown()
    w.close()
    w.deleteLater()
    wait(10)


def test_layer_inspector_shows_diameter_table_and_colors(win):
    from brokcam.ui import theme
    from brokcam.ui.canvas import LayerItem
    e = win.project.layers[0]
    win.project_panel.select("layer", e.id)
    wait(30)
    t = win.props.drill_table
    assert t is not None and t.isVisible() and t.rowCount() == 4
    assert [t.item(r, 1).text() for r in range(4)] == ["0,6", "0,8", "0,9", "1"]
    assert [t.item(r, 2).text() for r in range(4)] == ["T1", "T2", "T3", "T4"]
    assert [t.item(r, 3).text() for r in range(4)] == ["6", "7", "8", "9"]
    it = win.canvas.items_by_key[e.id]
    assert isinstance(it, LayerItem) and [g.rank for g in it.drill_groups] == [0, 1, 2, 3]
    assert [g.count for g in it.drill_groups] == [6, 7, 8, 9]
    cols = {theme.drill_color(g.rank) for g in it.drill_groups}
    assert len(cols) == 4
    t.selectRow(2)
    wait(20)
    assert it.highlight_rank == 2


def test_per_bit_generates_n_jobs_and_ctrl_z_removes_all(win, tmp_path):
    e = win.project.layers[0]
    win.project_panel.select("layer", e.id)
    wait(20)
    win.props._select_op("drilling")
    vals = win.props.form.values()
    vals["split_jobs"] = "per_bit"
    win.props.form.set_values(vals)
    win.props._primary_clicked()
    assert wait_until(lambda: len(win.project.jobs) == 4 and not win.runner.busy())
    names = [j.name for j in win.project.jobs]
    assert names[0] == "Furação Ø0,6 mm (T1 · 6 furos)" and names[-1] == "Furação Ø1 mm (T4 · 9 furos)"
    tree_jobs = [k for k in win.project_panel.selected_items() if k[0] == "job"]
    assert tree_jobs == [("job", win.project.jobs[0].id)]
    # inspetor do job: tabela de brocas com 1 linha
    assert win.props.drill_table is not None and win.props.drill_table.rowCount() == 1
    # salvar e reabrir: cada job é regenerado igual (bit_filter salvo nas settings)
    path = str(tmp_path / "p.bcam")
    assert win._write_project(path)
    before = [(j.name, j.job.stats()["holes"]) for j in win.project.jobs]
    win.open_project(path)
    assert wait_until(lambda: len(win.project.jobs) == 4 and not win.runner.busy())
    assert [(j.name, j.job.stats()["holes"]) for j in win.project.jobs] == before
    # gerar de novo e desfazer: os 4 saem de uma vez
    e = win.project.layers[0]
    win.project_panel.select("layer", e.id)
    wait(20)
    win.props._select_op("drilling")
    win.props._primary_clicked()
    assert wait_until(lambda: len(win.project.jobs) == 8 and not win.runner.busy())
    assert win.undo.undoText() == "Furação por broca"
    win.undo.undo()
    assert len(win.project.jobs) == 4
    win.undo.redo()
    assert len(win.project.jobs) == 8
    # G-code de um job separado: sem pausa de troca
    j = win.project.jobs[-1]
    win.generate_gcode(j.id)
    assert wait_until(lambda: bool(j.job.gcode) and not win.runner.busy())
    assert "M0" not in j.job.gcode.split()


def test_generate_only_this_diameter_button(win):
    from PySide6.QtWidgets import QPushButton
    e = win.project.layers[0]
    win.project_panel.select("layer", e.id)
    wait(20)
    t = win.props.drill_table
    t.selectRow(1)
    wait(20)
    btn = next(b for b in win.props.findChildren(QPushButton) if b.text() == "Gerar só este diâmetro")
    assert btn.isEnabled()
    btn.click()
    assert wait_until(lambda: len(win.project.jobs) == 1 and not win.runner.busy())
    j = win.project.jobs[0]
    assert j.name == "Furação Ø0,8 mm (T2 · 7 furos)" and j.job.stats()["holes"] == 7
    # pontual: não altera o padrão salvo do formulário
    from brokcam.cam.drilling import DRILLING
    assert win.settings.group_values(DRILLING).get("tool_select", "") == ""


def _layer_drill_ui(win):
    e = win.project.layers[0]
    win.project_panel.select("layer", e.id)
    wait(20)
    win.props._select_op("drilling")
    wait(20)
    return e


def test_mapped_diameter_shows_bit_and_generates_it(win):
    """Com 'Brocas disponíveis', a tabela mostra a broca que cada Ø vai usar e o botão diz isso."""
    _layer_drill_ui(win)
    t = win.props.drill_table
    assert t.isColumnHidden(4)                          # associação exata: nada a avisar
    vals = win.props.form.values()
    vals.update({"mapping": "library", "available_drills": "0.8, 1.0", "library_rule": "larger"})
    win.props.form.set_values(vals)
    win.props.form._emit_changed()
    wait(20)
    assert not t.isColumnHidden(4)
    assert t.item(0, 4).text() == "0,8 ⚠" and t.item(1, 4).text() == "0,8"
    t.selectRow(0)
    wait(20)
    btn = win.props.drill_only_btn
    assert btn.text() == "Gerar Ø0,6 (com broca 0,8)" and btn.isEnabled()
    btn.click()
    assert wait_until(lambda: len(win.project.jobs) == 1 and not win.runner.busy())
    j = win.project.jobs[0]
    assert j.job.ops[0].tool.diameter == 0.8 and j.job.stats()["holes"] == 6
    assert j.name == "Furação broca Ø0,8 mm (furos Ø0,6 · T1 · 6 furos)"


def test_per_bit_button_and_unique_names(win):
    _layer_drill_ui(win)
    allb = win.props.drill_all_btn
    assert allb is not None and allb.isEnabled() and allb.property("primary")
    allb.click()
    assert wait_until(lambda: len(win.project.jobs) == 4 and not win.runner.busy())
    assert win.undo.undoText() == "Furação por broca"
    from brokcam.cam.drilling import DRILLING
    assert win.settings.group_values(DRILLING).get("split_jobs", "single") == "single"   # pontual
    # de novo o mesmo diâmetro: nome com sufixo, não duplicado
    _layer_drill_ui(win)
    win.props.drill_table.selectRow(1)
    wait(20)
    win.props.drill_only_btn.click()
    assert wait_until(lambda: len(win.project.jobs) == 5 and not win.runner.busy())
    assert win.project.jobs[-1].name == "Furação Ø0,8 mm (T2 · 7 furos) (2)"
    # inspetor do job de furação: sem "comprimento de corte", com nº de brocas
    from PySide6.QtWidgets import QLabel
    win.project_panel.select("job", win.project.jobs[0].id)
    wait(20)
    labels = [lb.text() for lb in win.props.findChildren(QLabel) if lb.objectName() == "StatLabel"]
    assert "comprimento de corte" not in labels and "furos" in labels and "broca" in labels


def test_safe_gcode_filename():
    from brokcam.ui.main_window import safe_gcode_filename
    assert safe_gcode_filename("Furação Ø0,4 mm (T1 · 6 furos)") == "Furacao_0-4mm_T1_6_furos"
    assert safe_gcode_filename("placa.drl — furação (4 brocas · 40 furos)") == "placa_drl_furacao_4_brocas_40_furos"
    assert safe_gcode_filename("Isolamento — top.gbr") == "Isolamento_top_gbr"
    assert safe_gcode_filename("Ø ç") == "c" and safe_gcode_filename("—") == "programa"
    assert safe_gcode_filename("x" * 200).__len__() <= 60
    assert all(ord(c) < 128 for c in safe_gcode_filename("Furos fresados com fresa Ø1,25 mm"))


def test_save_all_gcode_to_folder(win, tmp_path, monkeypatch):
    from PySide6.QtWidgets import QFileDialog, QMessageBox
    _layer_drill_ui(win)
    win.props.drill_all_btn.click()
    assert wait_until(lambda: len(win.project.jobs) == 4 and not win.runner.busy())
    out = tmp_path / "sd"
    out.mkdir()
    (out / "Furacao_0-6mm_T1_6_furos.nc").write_text("antigo")
    monkeypatch.setattr(QFileDialog, "getExistingDirectory", lambda *a, **k: str(out))
    asked = []

    def ask(*a, **k):  # type: ignore[no-untyped-def]
        asked.append(a[2] if len(a) > 2 else "")
        return QMessageBox.StandardButton.No          # manter o existente, salvar com outro nome
    monkeypatch.setattr(QMessageBox, "question", ask)
    win.project_panel.select_many([("job", j.id) for j in win.project.jobs])
    wait(20)
    win.a_save_all_gcode.trigger()
    assert wait_until(lambda: not win.runner.busy() and len(list(out.glob("*.nc"))) == 5)
    names = sorted(p.name for p in out.glob("*.nc"))
    assert "Furacao_0-6mm_T1_6_furos_2.nc" in names and "Furacao_1mm_T4_9_furos.nc" in names
    assert (out / "Furacao_0-6mm_T1_6_furos.nc").read_text() == "antigo"   # não sobrescreveu
    assert asked and all(n.isascii() for n in names)
    txt = (out / "Furacao_0-8mm_T2_7_furos.nc").read_text()
    assert "G0" in txt and "M0" not in txt.split()
    assert all(j.job.gcode for j in win.project.jobs)


def test_layer_offset_without_group_moves_jobs(win, tmp_path):
    """Deslocar uma camada sem grupo (TransformCmd) leva junto os caminhos, como ao reabrir."""
    e = _layer_drill_ui(win)
    win.props._primary_clicked()
    assert wait_until(lambda: len(win.project.jobs) == 1 and not win.runner.busy())
    j = win.project.jobs[0]
    before = np.vstack([op.points for op in j.job.ops])
    from brokcam.ui.project import Transform
    win._on_layer_transform(e.id, Transform(None, None, (5.0, -2.0), 0.0))
    after = np.vstack([op.points for op in j.job.ops])
    assert np.allclose(after, before + [5.0, -2.0])
    win.undo.undo()
    assert np.allclose(np.vstack([op.points for op in j.job.ops]), before)
    win.undo.redo()
    path = str(tmp_path / "m.bcam")
    assert win._write_project(path)
    win.open_project(path)
    assert wait_until(lambda: len(win.project.jobs) == 1 and not win.runner.busy())
    reopened = np.vstack([op.points for op in win.project.jobs[0].job.ops])
    from collections import Counter
    key = lambda a: Counter(map(tuple, np.round(a, 6).tolist()))  # noqa: E731
    assert key(reopened) == key(after)
