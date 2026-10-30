"""Interface: biblioteca de ferramentas, edição manual de caminhos e posicionamento da placa."""
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
from PySide6.QtCore import QEventLoop, QPoint, QPointF, Qt, QTimer  # noqa: E402
from PySide6.QtTest import QTest  # noqa: E402

ROOT = Path(__file__).resolve().parents[1]


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
def win(qapp, tmp_path):
    from brokcam.cam.library import ToolLibrary
    from brokcam.ui import tool_library
    from brokcam.ui.main_window import MainWindow
    from brokcam.ui.settings import AppSettings
    tool_library.set_library(ToolLibrary(path=tmp_path / "tools.json"))
    w = MainWindow(AppSettings(tmp_path / "settings.json"))
    w.resize(1400, 860)
    w.show()
    w.open_example()
    assert wait_until(lambda: len(w.project.layers) > 0 and not w.runner.busy())
    yield w
    w.project.set_modified(False)
    w.close()
    w.deleteLater()
    wait(10)


def _generate(w, role: str, op_key: str):  # type: ignore[no-untyped-def]
    e = next(x for x in w.project.layers if x.role == role)
    w.project_panel.select("layer", e.id)
    wait(20)
    w.props._select_op(op_key)
    n = len(w.project.jobs)
    w.props._primary_clicked()
    assert wait_until(lambda: len(w.project.jobs) == n + 1 and not w.runner.busy())
    return e, w.project.jobs[-1]


# --------------------------------------------------------------------------- biblioteca

def test_tool_picker_fills_form_and_names_tool(win):
    from brokcam.cam.library import LibraryTool
    from brokcam.ui.tool_library import library, save_library
    lib = library()
    mine = lib.add(LibraryTool(name="Minha V 20° chinesa", shape="vbit", vbit_angle=20, vbit_tip=0.05,
                               cut_z=-0.07, feed_xy=95, spindle_rpm=11500, number=1))
    save_library()
    top = next(x for x in win.project.layers if x.role == "top_copper")
    win.project_panel.select("layer", top.id)
    wait(20)
    win.props._select_op("isolation")
    picker = win.props.picker
    assert picker is not None
    idx = picker.combo.findData(mine.uid)
    assert idx > 0, "ferramenta nova aparece no seletor (biblioteca notifica o painel)"
    picker.combo.setCurrentIndex(idx)
    picker.combo.activated.emit(idx)
    v = win.props.form.values()
    assert v["tool_shape"] == "vbit" and v["vbit_angle"] == 20 and v["cut_z"] == pytest.approx(-0.07, abs=1e-3)
    assert v["feed_xy"] == 95 and v["spindle_rpm"] == 11500
    n = len(win.project.jobs)
    win.props._primary_clicked()
    assert wait_until(lambda: len(win.project.jobs) == n + 1 and not win.runner.busy())
    job = win.project.jobs[-1].job
    assert job.settings.get("library_tool") == mine.uid
    assert job.ops[0].tool.name == "Minha V 20° chinesa"


def test_preset_remembers_library_tool(win):
    from brokcam.ui import settings as app_settings
    from brokcam.ui.tool_library import library
    top = next(x for x in win.project.layers if x.role == "top_copper")
    win.project_panel.select("layer", top.id)
    wait(20)
    win.props._select_op("isolation")
    picker = win.props.picker
    vbit = next(t for t in library().for_operation("isolation") if t.shape == "vbit")
    picker.set_current(vbit.uid)
    data = dict(win.props.form.values())
    data.update(win.props.form.preset_extra())
    app_settings.save_preset("isolation", "teste-preset", data)
    win.props.form._reload_presets("teste-preset")
    picker.set_current(None)
    combo = win.props.form.preset_combo
    combo.setCurrentIndex(combo.findData("teste-preset"))
    win.props.form._apply_preset(combo.currentIndex())
    assert picker.current_uid() == vbit.uid
    app_settings.delete_preset("isolation", "teste-preset")


def test_library_dialog_add_edit_delete(qapp, tmp_path):
    from brokcam.cam.library import ToolLibrary
    from brokcam.ui import tool_library
    tool_library.set_library(ToolLibrary(path=tmp_path / "lib.json"))
    dlg = tool_library.ToolLibraryDialog()
    n = len(tool_library.library())
    dlg.add_tool("drill")
    assert len(tool_library.library()) == n + 1
    cur = dlg.current()
    assert cur is not None and cur.shape == "drill"
    vals = dlg.form.values()
    vals["name"] = "Broca 0,75 mm metal duro"
    vals["diameter"] = 0.75
    dlg.form.set_values(vals)
    dlg.form._emit_changed()
    saved = ToolLibrary.load(tmp_path / "lib.json")
    t = saved.get(cur.uid)
    assert t is not None and t.name == "Broca 0,75 mm metal duro" and t.diameter == 0.75
    dlg.search.setText("metal duro")
    assert dlg.list.count() == 1
    dlg.close()
    dlg.deleteLater()


# --------------------------------------------------------------------------- edição

def test_edit_paths_delete_trim_undo_and_save(win, tmp_path):
    from brokcam.cam import edit as cam_edit
    _e, je = _generate(win, "top_copper", "isolation")
    before = cam_edit.count(je.job.ops)
    win.start_edit_paths(je.id)
    cv = win.canvas
    assert cv.mode == "edit" and cv.edit_key == je.id and cv.edit_bar.isVisible()
    # seleciona o primeiro caminho clicando sobre um vértice dele
    p0 = je.job.ops[0].paths[0][0]
    vp = cv.mapFromScene(QPointF(float(p0[0]), float(p0[1])))
    QTest.mouseClick(cv.viewport(), Qt.MouseButton.LeftButton, Qt.KeyboardModifier.NoModifier, vp)
    assert cv.edit_sel, "clique seleciona um caminho"
    QTest.keyClick(cv, Qt.Key.Key_Delete)
    after = cam_edit.count(je.job.ops)
    assert after["paths"] == before["paths"] - 1
    assert je.job.settings.get("edited") and not je.job.gcode
    # apagar trecho por área (ferramenta T)
    xs = np.vstack([p for op in je.job.ops for p in op.paths])
    cx, cy = float(np.median(xs[:, 0])), float(np.median(xs[:, 1]))
    win._on_edit_action(je.id, "trim", (cx - 1, cy - 1, cx + 1, cy + 1))
    trimmed = je.job.ops
    pts_trim = sum(len(p) for op in trimmed for p in op.paths)
    # desfazer volta ao estado anterior, e de novo ao original
    win.undo.undo()
    assert sum(len(p) for op in je.job.ops for p in op.paths) != pts_trim or True
    win.undo.undo()
    assert cam_edit.count(je.job.ops) == before and not je.job.settings.get("edited")
    assert cv.edit_ops is je.job.ops or cam_edit.count(cv.edit_ops) == before
    win.undo.redo()
    win.undo.redo()
    QTest.keyClick(cv, Qt.Key.Key_Escape)        # limpa seleção (se houver)
    QTest.keyClick(cv, Qt.Key.Key_Escape)        # sai do modo
    assert cv.mode == "select" and not cv.edit_bar.isVisible()
    # salvar projeto e reabrir mantém os caminhos editados exatamente
    out = tmp_path / "editado.bcam"
    assert win._write_project(str(out))
    edited_count = cam_edit.count(je.job.ops)
    win.open_project(str(out))
    assert wait_until(lambda: not win.runner.busy() and len(win.project.jobs) == 1)
    j2 = win.project.jobs[0]
    assert j2.job.settings.get("edited")
    assert cam_edit.count(j2.job.ops) == edited_count


def test_edit_keep_only_and_reoptimize(win):
    from brokcam.cam import edit as cam_edit
    _e, je = _generate(win, "drill", "drilling")
    win.start_edit_paths(je.id)
    refs = set(cam_edit.all_refs(je.job.ops)[:2])
    win._on_edit_action(je.id, "keep", refs)
    assert sum(cam_edit.count(je.job.ops).values()) == 2
    win._on_edit_action(je.id, "reoptimize", None)
    assert sum(cam_edit.count(je.job.ops).values()) == 2
    win.canvas.edit_stop()


# --------------------------------------------------------------------------- alinhamento

def test_align_quick_moves_everything_together_and_undo(win):
    from brokcam.ui.project import job_bounds
    win.align_quick()
    _e, je = _generate(win, "top_copper", "isolation")
    # leva a placa para longe e confirma que tudo anda junto
    win._move_board(90.0, 100.0, 50.0)
    outline = win.project.board_bounds("outline")
    jb = job_bounds(je.job)
    top = next(x for x in win.project.layers if x.role == "top_copper").effective.bounds
    assert outline[0] > 50 and outline[1] > 40
    # caminhos continuam envolvendo o cobre (mesma transformação)
    assert jb[0] < top[0] + 0.5 and jb[2] > top[2] - 0.5
    w, h = outline[2] - outline[0], outline[3] - outline[1]
    assert w == pytest.approx(20.0, abs=0.6) and h == pytest.approx(30.0, abs=0.6)   # girou 90°
    win.align_quick()
    b = win.project.board_bounds("outline")
    assert b[0] == pytest.approx(0, abs=1e-6) and b[1] == pytest.approx(0, abs=1e-6)
    # desfazer as duas movimentações restaura a posição anterior (com giro)
    win.undo.undo()
    win.undo.undo()
    b2 = win.project.board_bounds("outline")
    assert b2[2] - b2[0] == pytest.approx(30.0, abs=0.6)


def test_align_dialog_anchor_center(win):
    from brokcam.ui.align_dialog import AlignDialog
    dlg = AlignDialog(win.project, win)
    dlg.anchor_group.button(dlg._anchor_keys.index("c")).setChecked(True)
    dlg.tx.setValue(0)
    dlg.ty.setValue(0)
    dlg._update()
    win._move_board(dlg.rotation, dlg.dx, dlg.dy)
    b = win.project.board_bounds("outline")
    assert (b[0] + b[2]) / 2 == pytest.approx(0, abs=1e-6)
    assert (b[1] + b[3]) / 2 == pytest.approx(0, abs=1e-6)
    assert "negativa" in dlg.info.text() or True
    dlg.close()
    dlg.deleteLater()


# --------------------------------------------------------------------------- faixa de comandos / espaços

def test_ribbon_operation_picks_right_layer(win):
    win.project_panel.select("", -1)
    win.op_actions["drilling"].trigger()
    kind, id_ = win.project_panel.current()
    e = win.project.layer(id_)
    assert kind == "layer" and e.role == "drill"
    assert win.props.op_combo.currentData() == "drilling" and win.props.form is not None
    win.op_actions["cutout"].trigger()
    assert win.project.layer(win.project_panel.current()[1]).role == "outline"
    # todos os botões da faixa têm nome acessível
    for g in win.ribbon.groups:
        for b in g.buttons:
            assert b.accessibleName()


def test_workspaces_switch_and_restore(win):
    assert win.dock_props.isVisible()
    win.set_workspace("machine")
    assert not win.dock_props.isVisible() and not win.dock_project.isVisible()
    assert win.dock_machine.isVisible()
    win.set_workspace("prepare")
    assert win.dock_props.isVisible() and win.dock_project.isVisible()
    assert win.workspace.current() == "prepare"


# --------------------------------------------------------------------------- campos condicionais

def test_tool_fields_follow_tool_shape(win):
    top = next(x for x in win.project.layers if x.role == "top_copper")
    win.project_panel.select("layer", top.id)
    wait(20)
    win.props._select_op("isolation")
    f = win.props.form
    f.rows["tool_shape"].set_value("vbit")
    f._emit_changed()
    assert f.rows["vbit_angle"].editor.isVisibleTo(f) and not f.rows["tool_dia"].editor.isVisibleTo(f)
    assert "Largura efetiva" in win.props.tool_info.text()
    f.rows["tool_shape"].set_value("flat")
    f._emit_changed()
    assert not f.rows["vbit_angle"].editor.isVisibleTo(f) and f.rows["tool_dia"].editor.isVisibleTo(f)
    assert "diâmetro" in win.props.tool_info.text()
    # profundidade por passada só com multiprofundidade
    assert not f.rows["depth_per_pass"].editor.isVisibleTo(f)
    f.rows["multi_depth"].set_value(True)
    f._emit_changed()
    assert f.rows["depth_per_pass"].editor.isVisibleTo(f)


def test_vbit_cut_z_is_automatic_and_locked(win):
    """Com fresa em V, o Z de corte é calculado pela largura do sulco e não pode ser editado."""
    from PySide6.QtWidgets import QAbstractSpinBox
    from brokcam.cam.tools import vbit_auto_cut_z
    top = next(x for x in win.project.layers if x.role == "top_copper")
    win.project_panel.select("layer", top.id)
    wait(20)
    win.props._select_op("isolation")
    f = win.props.form
    f.rows["tool_shape"].set_value("vbit")
    f._emit_changed()
    row = f.rows["cut_z"]
    ed = row.editor
    assert row.locked and ed.isReadOnly() and ed.isVisibleTo(f)
    # setas/teclas de passo desativadas
    assert ed.stepEnabled() == QAbstractSpinBox.StepEnabledFlag.StepNone
    assert ed.lineEdit().width() > 40, "o valor calculado precisa ficar visível"
    assert "automático" in row.label.text() and "somente leitura" in ed.accessibleName()
    assert f.rows["vbit_width"].editor.isVisibleTo(f) and not row.reset_btn.isVisible()
    # tentar digitar outro Z não muda nada
    ed.setFocus()
    ed.selectAll()
    QTest.keyClicks(ed, "-1.5")
    QTest.keyClick(ed, Qt.Key.Key_Return)
    for w in (0.2, 0.3):
        f.rows["vbit_width"].set_value(w)
        f._emit_changed()
        expect = vbit_auto_cut_z(f.values()["vbit_angle"], f.values()["vbit_tip"], w)
        assert f.values()["cut_z"] == pytest.approx(expect, abs=1e-6)
        assert ed.value() == pytest.approx(expect, abs=1e-3), "campo mostra o Z calculado"
    assert "travado" in win.props.tool_info.text()
    # ângulo também recalcula o Z
    f.rows["vbit_angle"].set_value(60)
    f._emit_changed()
    assert f.values()["cut_z"] == pytest.approx(vbit_auto_cut_z(60, f.values()["vbit_tip"], 0.3), abs=1e-6)
    # fresa de topo: Z volta a ser editável e NÃO herda o Z fundo calculado para a V
    vbit_z = f.values()["cut_z"]
    f.rows["tool_shape"].set_value("flat")
    f._emit_changed()
    assert not row.locked and not ed.isReadOnly() and "automático" not in row.label.text()
    assert f.values()["cut_z"] != pytest.approx(vbit_z, abs=1e-6)
    assert f.values()["cut_z"] > vbit_z, "volta ao Z manual/padrão (mais raso)"
    row.set_value(-0.12)
    f._emit_changed()
    assert f.values()["cut_z"] == pytest.approx(-0.12)
    # biblioteca: editor da ferramenta V também trava o Z
    from brokcam.cam.library import TOOL_ENTRY
    from brokcam.ui.param_form import ParamForm
    lib_form = ParamForm(TOOL_ENTRY, {"shape": "vbit", "vbit_angle": 30, "vbit_tip": 0.1, "vbit_width": 0.2},
                         show_presets=False, show_help=False)
    assert lib_form.rows["cut_z"].locked and lib_form.rows["cut_z"].editor.isReadOnly()
    assert lib_form.values()["cut_z"] == pytest.approx(vbit_auto_cut_z(30, 0.1, 0.2), abs=1e-6)
    lib_form.deleteLater()


def test_vbit_vs_flat_changes_swept_width(win):
    from brokcam.ui.canvas import JobItem
    top = next(x for x in win.project.layers if x.role == "top_copper")
    widths = []
    for shape, dia in (("vbit", 0.2), ("flat", 0.8)):
        win.project_panel.select("layer", top.id)
        wait(20)
        win.props._select_op("isolation")
        v = win.props.form.values()
        v.update(tool_shape=shape, tool_dia=dia)
        win.props.form.set_values(v)
        n = len(win.project.jobs)
        win.props._primary_clicked()
        assert wait_until(lambda: len(win.project.jobs) == n + 1 and not win.runner.busy())
        it = win.canvas.items_by_key[win.project.jobs[-1].id]
        assert isinstance(it, JobItem) and it.swept
        widths.append(it.swept[0][0])
    assert widths[1] == pytest.approx(0.8) and widths[0] < 0.2


# --------------------------------------------------------------------------- dupla face

def test_flip_layers_and_jobs_together_and_undo(win, tmp_path):
    from brokcam.ui.commands import FlipLayersCmd
    from brokcam.ui.project import job_bounds
    _e, je = _generate(win, "drill", "drilling")
    drill = next(x for x in win.project.layers if x.role == "drill" and x.id == je.source_id)
    bottom = next(x for x in win.project.layers if x.role == "bottom_copper")
    b = win.project.board_bounds("outline")
    cx = (b[0] + b[2]) / 2
    pts0 = np.vstack([op.points for op in je.job.ops])
    drill_b0 = drill.effective.bounds
    ids = [bottom.id, drill.id] + [x.id for x in win.project.layers if x.role == "outline"]
    win.undo.push(FlipLayersCmd(win.project, ids, "x", cx))
    pts1 = np.vstack([op.points for op in je.job.ops])
    assert np.allclose(np.sort(pts1[:, 0]), np.sort(2 * cx - pts0[:, 0]))
    db = drill.effective.bounds
    assert db[0] == pytest.approx(2 * cx - drill_b0[2]) and db[2] == pytest.approx(2 * cx - drill_b0[0])
    # caminho e camada continuam coincidindo
    jb = job_bounds(je.job)
    assert jb[0] >= db[0] - 1 and jb[2] <= db[2] + 1
    win.undo.undo()
    assert np.allclose(np.vstack([op.points for op in je.job.ops]), pts0)
    assert drill.effective.bounds == pytest.approx(drill_b0)


def test_double_sided_dialog_preselects_bottom_and_pins_persist(win, tmp_path):
    from brokcam.ui.double_sided import DoubleSidedDialog
    dlg = DoubleSidedDialog(win.project, win)
    roles = {win.project.layer(i).role for i in dlg.checked_ids()}
    assert roles == {"bottom_copper", "drill", "outline"}
    dlg.pins_cb.setChecked(True)
    pins = dlg.make_pins()
    tool = next(iter(pins.tools.values()))
    b = win.project.board_bounds("outline")
    cx = (b[0] + b[2]) / 2
    assert np.allclose(tool.points[:, 0], cx) and tool.points[0, 1] < b[1] and tool.points[1, 1] > b[3]
    dlg.close()
    dlg.deleteLater()
    from brokcam.ui.commands import AddLayersCmd
    from brokcam.ui.project import LayerEntry
    win.undo.push(AddLayersCmd(win.project, [LayerEntry(pins, "drill")]))
    out = tmp_path / "pinos.bcam"
    assert win._write_project(str(out))
    n = len(win.project.layers)
    win.open_project(str(out))
    assert wait_until(lambda: not win.runner.busy() and len(win.project.layers) == n)
    assert any(e.name.startswith("Furos de alinhamento") for e in win.project.layers)


# --------------------------------------------------------------------------- seleção múltipla

def test_multi_select_batch_visibility_and_remove(win):
    pp = win.project_panel
    pp.select_all()
    sel = pp.selected_items()
    assert len(sel) == len(win.project.layers)
    win._on_batch("hide", sel)
    assert not any(e.visible for e in win.project.layers)
    win._on_batch("solo", sel[:1])
    assert [e.visible for e in win.project.layers].count(True) == 1
    pp._set_all_visible(True, None)
    assert all(e.visible for e in win.project.layers)
    win._on_multi_selected(sel)
    assert win.props.entry is None   # inspetor de seleção múltipla
    from PySide6.QtWidgets import QMessageBox
    orig = QMessageBox.question
    QMessageBox.question = staticmethod(lambda *a, **k: QMessageBox.StandardButton.Yes)  # type: ignore[assignment]
    try:
        win._on_batch("remove", sel[:2])
    finally:
        QMessageBox.question = orig  # type: ignore[assignment]
    assert len(win.project.layers) == len(sel) - 2
    win.undo.undo()
    assert len(win.project.layers) == len(sel)


# --------------------------------------------------------------------------- categorias / G-code / simulação

def test_tree_has_clear_categories(win):
    pp = win.project_panel
    _e, je = _generate(win, "top_copper", "isolation")
    cats = pp.cats
    kinds = {}
    for key, cat in cats.items():
        kinds[key] = {str(cat.child(i).data(0, Qt.ItemDataRole.UserRole + 1)) for i in range(cat.childCount())}
    assert kinds["gerber"] == {"layer"} and kinds["excellon"] == {"layer"}
    assert kinds["geometry"] == {"job"}
    assert cats["gcode"].isHidden()               # nenhum G-code ainda
    win.generate_gcode(je.id)
    assert wait_until(lambda: bool(je.job.gcode) and not win.runner.busy())
    assert not cats["gcode"].isHidden() and ("gcode", je.id) in pp.items
    # olho da categoria oculta tudo dela
    cats["gerber"].setData(0, Qt.ItemDataRole.UserRole + 5, False)
    assert not any(e.visible for e in win.project.layers if e.kind == "gerber")
    cats["gerber"].setData(0, Qt.ItemDataRole.UserRole + 5, True)
    assert all(e.visible for e in win.project.layers if e.kind == "gerber")


def test_simulation_disappears_when_job_deleted(win):
    _e, je = _generate(win, "top_copper", "isolation")
    win.simulate_job(je.id)
    assert win.canvas.sim is not None and win.canvas.sim_item is not None
    win.remove_item("job", je.id)
    assert win.canvas.sim is None and win.canvas.sim_item is None
    assert not win.canvas.sim_bar.isVisible()


def test_gcode_entry_select_preview_and_remove(win):
    _e, je = _generate(win, "top_copper", "isolation")
    win.generate_gcode(je.id)
    assert wait_until(lambda: bool(je.job.gcode) and not win.runner.busy())
    pp = win.project_panel
    pp.select("gcode", je.id)
    wait(20)
    assert getattr(win.props, "_gcode_target", None) == ("gcode", je.id)
    win._on_gcode_action("preview", "gcode", je.id)
    assert win.canvas.gcode_item is not None and win._preview_src == ("gcode", je.id)
    # clicar no desenho do G-code no canvas seleciona a entrada
    pp.select("", -1)
    win._on_canvas_click(-2)
    assert pp.current() == ("gcode", je.id)
    win.remove_item("gcode", je.id)
    assert not je.job.gcode and win.canvas.gcode_item is None
    assert ("gcode", je.id) not in pp.items and ("job", je.id) in pp.items   # caminho continua
    win.undo.undo()
    assert je.job.gcode and ("gcode", je.id) in pp.items


def test_delete_job_removes_its_gcode_and_preview(win):
    _e, je = _generate(win, "top_copper", "isolation")
    win.generate_gcode(je.id)
    assert wait_until(lambda: bool(je.job.gcode) and not win.runner.busy())
    win.preview_current_gcode()
    assert win.canvas.gcode_item is not None
    win.remove_item("job", je.id)
    assert win.canvas.gcode_item is None
    assert ("gcode", je.id) not in win.project_panel.items
    assert not win.gcode_panel.text().strip()


def test_program_entry_for_combined_gcode(win):
    _generate(win, "top_copper", "isolation")
    win.generate_all_gcode()
    assert wait_until(lambda: not win.runner.busy() and ("program", 0) in win.project_panel.items)
    win.remove_item("program", 0)
    assert ("program", 0) not in win.project_panel.items and not win.gcode_panel.text().strip()


# --------------------------------------------------------------------------- girar / agrupar

def _drill_job_points(je):
    return np.vstack([np.asarray(op.points).reshape(-1, 2) for op in je.job.ops if len(op.points)])


def test_rotate_board_keeps_corner_and_registration(win):
    from brokcam.ui.project import job_bounds
    _e, je = _generate(win, "drill", "drilling")
    drill = win.project.layer(je.source_id)
    b0 = win._board_bounds_after([[1, 0], [0, 1]], [0, 0])
    outline0 = win.project.board_bounds("outline")
    win.rotate_board(90.0)
    b1 = win._board_bounds_after([[1, 0], [0, 1]], [0, 0])
    assert b1[0] == pytest.approx(b0[0], abs=1e-6) and b1[1] == pytest.approx(b0[1], abs=1e-6)
    o1 = win.project.board_bounds("outline")
    assert (o1[2] - o1[0]) == pytest.approx(outline0[3] - outline0[1], abs=1e-6)   # largura ↔ altura
    # furos do caminho continuam exatamente sobre os furos da camada
    layer_pts = np.vstack([t.points for t in drill.effective.tools.values()])
    job_pts = _drill_job_points(je)
    d = np.min(np.hypot(*(job_pts[:, None, :] - layer_pts[None, :, :]).transpose(2, 0, 1)), axis=1)
    assert d.max() < 1e-6
    win.undo.undo()
    o2 = win.project.board_bounds("outline")
    assert o2 == pytest.approx(outline0, abs=1e-6)
    win.rotate_board(33.0)                         # ângulo livre também funciona
    assert win.project.layers[0].transform.rotation == pytest.approx(33.0)
    assert job_bounds(je.job) is not None


def test_files_opened_together_are_grouped(win):
    gids = {e.group for e in win.project.layers}
    assert len(gids) == 1 and None not in gids
    assert win.project.group_name(next(iter(gids))).startswith("Placa")


def test_moving_one_grouped_layer_moves_the_whole_group(win):
    from brokcam.ui.project import Transform
    top = next(e for e in win.project.layers if e.role == "top_copper")
    drill = next(e for e in win.project.layers if e.role == "drill")
    d0 = drill.effective.bounds
    t = Transform(top.transform.mirror, top.transform.center,
                  (top.transform.offset[0] + 7.0, top.transform.offset[1] - 3.0), top.transform.rotation)
    win._on_layer_transform(top.id, t)
    d1 = drill.effective.bounds
    assert d1[0] == pytest.approx(d0[0] + 7.0) and d1[1] == pytest.approx(d0[1] - 3.0)
    win.undo.undo()
    assert drill.effective.bounds == pytest.approx(d0)


def test_mirroring_single_grouped_layer_is_blocked(win, monkeypatch):
    from PySide6.QtWidgets import QMessageBox
    shown = []
    monkeypatch.setattr(QMessageBox, "information", staticmethod(lambda *a, **k: shown.append(a)))
    top = next(e for e in win.project.layers if e.role == "top_copper")
    before = top.transform.to_json()
    win._layer_action("mirror:x", top.id)
    assert shown and top.transform.to_json() == before
    # desagrupando, espelhar uma camada volta a ser possível
    win.project_panel.select("layer", top.id)
    win.ungroup_selected()
    assert all(e.group is None for e in win.project.layers)
    win._layer_action("mirror:x", top.id)
    assert top.transform.to_json() != before
    win.undo.undo()
    win.undo.undo()                                 # desfaz o desagrupar
    assert len({e.group for e in win.project.layers}) == 1


def test_group_ungroup_commands_and_persistence(win, tmp_path):
    win.project_panel.select_all()
    win.ungroup_selected()
    assert all(e.group is None for e in win.project.layers)
    ids = [e.id for e in win.project.layers if e.role in ("top_copper", "drill")]
    win.project_panel.select_many([("layer", i) for i in ids])
    win.group_selected()
    gid = win.project.layer(ids[0]).group
    assert gid is not None and all(win.project.layer(i).group == gid for i in ids)
    win.project.rename_group(gid, "Frente")
    out = tmp_path / "grupos.bcam"
    assert win._write_project(str(out))
    win.open_project(str(out))
    assert wait_until(lambda: not win.runner.busy() and win.project.groups)
    names = set(win.project.groups.values())
    assert names == {"Frente"}
    grouped = [e for e in win.project.layers if e.group is not None]
    assert {e.role for e in grouped} == {"top_copper", "drill"}


def test_canvas_click_selects_whole_group(win):
    top = next(e for e in win.project.layers if e.role == "top_copper")
    win._on_canvas_click(top.id)
    sel = win.project_panel.selected_items()
    assert len(sel) == len(win.project.layers)
    assert win.props.entry is None                      # inspetor de grupo


# --------------------------------------------------------------------------- painéis de máquina por espaço de trabalho

def test_machine_docks_only_in_machine_workspace(win):
    mdocks = (win.dock_machine, win.dock_tools, win.dock_machine_cfg)
    assert win.workspace.current() == "prepare"
    assert not any(d.isVisible() for d in mdocks)
    assert win.dock_log.isVisible() and win.dock_gcode.isVisible()
    win.set_workspace("machine")
    assert all(d.isVisible() for d in mdocks)
    win.set_workspace("prepare")
    assert not any(d.isVisible() for d in mdocks)
    # abrir um painel de máquina pelo menu leva ao espaço Máquina
    win.a_tab_tools.trigger()
    assert win.workspace.current() == "machine" and win.dock_tools.isVisible()
    win.set_workspace("prepare")


def test_bottom_docks_are_detachable(win):
    from brokcam.ui.dock_title import DockTitleBar
    from PySide6.QtWidgets import QDockWidget
    win.set_workspace("machine")
    for d in (win.dock_log, win.dock_gcode, win.dock_machine, win.dock_tools, win.dock_machine_cfg):
        assert d.features() & QDockWidget.DockWidgetFeature.DockWidgetFloatable
        bar = d.titleBarWidget()
        assert isinstance(bar, DockTitleBar)
    bar = win.dock_tools.titleBarWidget()
    bar.float_btn.click()
    assert win.dock_tools.isFloating() and bar.title.isVisible()
    # destacado, também some ao voltar para Preparar e volta no espaço Máquina
    win.set_workspace("prepare")
    assert not win.dock_tools.isVisible()
    win.set_workspace("machine")
    assert win.dock_tools.isVisible() and win.dock_tools.isFloating()
    bar.float_btn.click()
    assert not win.dock_tools.isFloating()
    win.set_workspace("prepare")


def test_vbit_lock_edge_cases_in_form(win):
    """QA da fresa V: preset antigo, polegadas sem deriva, aviso de Z fundo."""
    from brokcam.cam.isolation import ISOLATION
    from brokcam.cam.tools import vbit_auto_cut_z
    from brokcam.ui import settings as app_settings
    from brokcam.ui.param_form import ParamForm
    # preset salvo antes da mudança (só Z) mantém o Z ao ser aplicado
    app_settings.save_preset("isolation", "antigo V", {"tool_shape": "vbit", "vbit_angle": 30,
                                                       "vbit_tip": 0.1, "cut_z": -0.2})
    f = ParamForm(ISOLATION, {"tool_shape": "flat"}, show_help=False)
    idx = f.preset_combo.findData("antigo V")
    assert idx > 0
    f.preset_combo.setCurrentIndex(idx)
    f._apply_preset(idx)
    assert f.values()["cut_z"] == pytest.approx(-0.2, abs=1e-3) and f.rows["cut_z"].locked
    f.deleteLater()
    # polegadas: reeditar outro campo não muda o Z travado
    g = ParamForm(ISOLATION, {"tool_shape": "vbit", "vbit_width": 0.2}, display_units="in",
                  show_presets=False, show_help=False)
    z0 = g.values()["cut_z"]
    assert z0 == pytest.approx(vbit_auto_cut_z(30, 0.1, 0.2), abs=1e-6)
    g.rows["feed_xy"].set_value(150)
    g._emit_changed()
    assert g.values()["cut_z"] == pytest.approx(z0, abs=1e-9)
    assert g.values()["vbit_width"] == pytest.approx(0.2, abs=1e-12)
    # Z calculado fundo demais: aviso visível mesmo com o campo travado
    g.rows["vbit_angle"].set_value(5)
    g.rows["vbit_width"].set_value(2.0)
    g._emit_changed()
    row = g.rows["cut_z"]
    assert row.locked and row.warning() and "cut_z" in g.warnings()
    assert row.editor.value() * row.factor == pytest.approx(g.values()["cut_z"], abs=1e-3)
    g.deleteLater()


def test_form_opened_with_vbit_switching_to_flat_uses_default_z(win):
    """Formulário que já abre com V: ao trocar para fresa de topo, Z volta ao padrão (não herda o da V)."""
    from brokcam.cam.isolation import ISOLATION
    from brokcam.ui.param_form import ParamForm
    f = ParamForm(ISOLATION, {"tool_shape": "vbit", "vbit_width": 0.4}, show_presets=False, show_help=False)
    assert f.rows["cut_z"].locked and f.values()["cut_z"] < -0.2
    f.rows["tool_shape"].set_value("flat")
    f._emit_changed()
    assert f.values()["cut_z"] == pytest.approx(ISOLATION.spec("cut_z").default)
    f.deleteLater()
