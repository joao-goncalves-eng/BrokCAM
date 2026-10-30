"""Testes da interface (rodam sem tela: QT_QPA_PLATFORM=offscreen)."""
from __future__ import annotations

import os
import sys
import tempfile
import threading
import time
from pathlib import Path

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
if sys.platform == "win32":
    os.environ.setdefault("QT_QPA_FONTDIR", r"C:\Windows\Fonts")
os.environ.setdefault("BROKCAM_CONFIG_DIR", tempfile.mkdtemp(prefix="brokcam-test-"))

import numpy as np  # noqa: E402
import pytest  # noqa: E402
import shapely  # noqa: E402

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from PySide6.QtCore import QEventLoop, QPointF, Qt, QTimer  # noqa: E402

from brokcam.core.params import ParamGroup, ParamSpec  # noqa: E402

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


def wait_until(cond, timeout: float = 30.0) -> bool:  # type: ignore[no-untyped-def]
    t0 = time.time()
    while time.time() - t0 < timeout:
        if cond():
            return True
        wait(20)
    return cond()


FAKE_GROUP = ParamGroup(
    key="teste_ui", title="Grupo de teste", description="Grupo fictício para testar o formulário.",
    params=(
        ParamSpec("cut_z", "Profundidade", "float", -0.1,
                  "Profundidade do corte abaixo da superfície. Valores positivos não cortam.",
                  unit="mm", minimum=-5, maximum=5, step=0.01, decimals=3),
        ParamSpec("passes", "Passadas", "int", 2, "Número de passadas ao redor das trilhas.",
                  minimum=1, maximum=20),
        ParamSpec("climb", "Fresamento concordante", "bool", True, "Usa fresamento concordante (climb)."),
        ParamSpec("strategy", "Estratégia", "choice", "b", "Estratégia de preenchimento.",
                  choices=(("a", "Linhas"), ("b", "Contorno"), ("c", "Semente"))),
        ParamSpec("note", "Observação", "str", "abc", "Texto livre.", advanced=True),
        ParamSpec("start_gcode", "G-code inicial", "str", "G21\nG90", "Executado no início.", advanced=True),
    ),
)


# --------------------------------------------------------------------------- janela

def test_main_window_opens(qapp):
    from brokcam.ui.main_window import MainWindow
    from brokcam.ui.settings import AppSettings
    win = MainWindow(AppSettings())
    win.resize(1400, 860)
    win.show()
    wait(50)
    assert win.isVisible()
    assert win.stack.currentWidget() is win.welcome          # projeto vazio -> tela inicial
    assert win.dock_project.isVisible() and win.dock_props.isVisible()
    titles = [a.text().replace("&", "") for a in win.menuBar().actions()]
    assert titles == ["Arquivo", "Editar", "Exibir", "Operações", "Máquina", "Ajuda"]
    # todos os botões da barra de ferramentas têm nome acessível
    for a in win.toolbar.actions():
        w = win.toolbar.widgetForAction(a)
        if w is not None and a.text():
            assert w.accessibleName()
    win.project.set_modified(False)
    win.close()
    win.deleteLater()
    wait(10)


def test_open_example_and_generate(qapp):
    from brokcam.ui import backend
    if not backend.status()["parsers"] or not backend.status()["cam"]:
        pytest.skip("parsers/CAM indisponíveis")
    from brokcam.ui.main_window import MainWindow
    from brokcam.ui.settings import AppSettings
    win = MainWindow(AppSettings())
    win.show()
    win.open_example()
    assert wait_until(lambda: len(win.project.layers) > 0 and not win.runner.busy(), 60)
    assert win.stack.currentWidget() is win.canvas
    top = next(e for e in win.project.layers if e.role == "top_copper")
    win.project_panel.select("layer", top.id)
    wait(20)
    assert win.props.form is not None
    win.props._primary_clicked()
    assert wait_until(lambda: len(win.project.jobs) == 1 and not win.runner.busy(), 120)
    job = win.project.jobs[0]
    assert job.source_id == top.id and job.job.stats()["cut_length_mm"] > 0
    # desfazer remove o caminho; refazer devolve
    win.undo.undo()
    assert not win.project.jobs
    win.undo.redo()
    assert len(win.project.jobs) == 1
    if backend.status()["gcode"]:
        win.generate_gcode(job.id)
        assert wait_until(lambda: bool(job.job.gcode) and not win.runner.busy(), 60)
        assert "G" in win.gcode_panel.text()
    win.project.set_modified(False)
    win.close()
    win.deleteLater()
    wait(10)


# --------------------------------------------------------------------------- formulário

def test_param_form_fake_group(qapp, tmp_path, monkeypatch):
    monkeypatch.setenv("BROKCAM_CONFIG_DIR", str(tmp_path))
    from PySide6.QtWidgets import QCheckBox, QComboBox, QDoubleSpinBox, QLineEdit, QPlainTextEdit, QSpinBox
    from brokcam.ui.param_form import ParamForm
    from brokcam.ui import settings as st
    form = ParamForm(FAKE_GROUP)
    form.resize(420, 600)
    form.show()
    wait(10)
    assert form.values() == FAKE_GROUP.defaults()
    assert isinstance(form.editor("cut_z"), QDoubleSpinBox)
    assert isinstance(form.editor("passes"), QSpinBox)
    assert isinstance(form.editor("climb"), QCheckBox)
    assert isinstance(form.editor("strategy"), QComboBox)
    assert isinstance(form.editor("note"), QLineEdit)
    assert isinstance(form.editor("start_gcode"), QPlainTextEdit)
    ed = form.editor("cut_z")
    assert ed.suffix() == " mm" and ed.decimals() == 3
    assert ed.minimum() == -5 and ed.maximum() == 5
    for spec in FAKE_GROUP.params:
        w = form.editor(spec.key)
        assert w.toolTip().startswith(spec.tooltip)
        assert w.accessibleName().startswith(spec.label)
        assert w.accessibleDescription() == spec.help
    # avançado oculto por padrão
    assert not form.editor("note").isVisible()
    form.set_show_advanced(True)
    assert form.editor("note").isVisible()
    # alteração, sinal e botão "restaurar padrão"
    got: list[dict] = []
    form.valuesChanged.connect(got.append)
    ed.setValue(-0.25)
    assert got and abs(got[-1]["cut_z"] + 0.25) < 1e-9
    row = form.rows["cut_z"]
    assert row.reset_btn.isVisible() and ed.property("modified") is True
    row.reset()
    assert abs(form.values()["cut_z"] + 0.1) < 1e-9
    # validação visual: Z positivo é sinalizado
    ed.setValue(0.5)
    assert ed.property("invalid") is True
    assert "cut_z" in form.warnings()
    # ajuda contextual mostra a explicação completa do campo focado
    form._show_help(form.rows["passes"])
    assert "passadas" in form.help_panel.body.text()
    # presets
    form.set_values({**FAKE_GROUP.defaults(), "passes": 7})
    st.save_preset(FAKE_GROUP.key, "meu preset", form.values())
    assert "meu preset" in st.list_presets(FAKE_GROUP.key)
    assert st.load_preset(FAKE_GROUP.key, "meu preset")["passes"] == 7
    form.close()


def test_param_form_inches(qapp):
    from brokcam.ui.param_form import ParamForm
    form = ParamForm(FAKE_GROUP, display_units="in", show_presets=False)
    ed = form.editor("cut_z")
    assert ed.suffix() == " pol"
    ed.setValue(-0.01)                      # polegadas
    assert abs(form.values()["cut_z"] + 0.254) < 1e-6   # volta em mm


# --------------------------------------------------------------------------- desempenho

def test_qpath_20000_polygons_fast(qapp):
    from brokcam.ui import qpaths
    rng = np.random.default_rng(1)
    pts = rng.random((20000, 2)) * 300
    polys = shapely.buffer(shapely.points(pts), 0.25, quad_segs=4)
    geom = shapely.multipolygons(polys)
    t0 = time.perf_counter()
    path = qpaths.geometry_to_path(geom)
    dt = time.perf_counter() - t0
    assert path.elementCount() == int(shapely.get_num_coordinates(polys).sum())
    assert dt < 1.5, f"conversão lenta: {dt:.2f}s"
    # furos (anel interno) ficam vazios pela regra par-ímpar
    donut = shapely.box(0, 0, 10, 10).difference(shapely.box(3, 3, 6, 6))
    p = qpaths.geometry_to_path(donut)
    assert p.contains(QPointF(1, 1)) and not p.contains(QPointF(4, 4))


def test_segments_and_polylines(qapp):
    from brokcam.ui import qpaths
    segs = np.array([[[0, 0, 0], [1, 0, 0]], [[1, 0, 0], [1, 1, 0]], [[5, 5, 0], [6, 6, 0]]], dtype=float)
    p = qpaths.segments_to_path(segs)
    assert p.elementCount() == 5
    p2 = qpaths.polylines_to_path([np.array([[0, 0], [1, 1], [2, 0]]), np.array([[5, 5], [6, 6]])])
    assert p2.elementCount() == 5


# --------------------------------------------------------------------------- ponte de sinais

class FakeController:
    """Controlador mínimo: callbacks chamados a partir de outra thread."""

    def __init__(self) -> None:
        self.on_status = self.on_line = self.on_progress = None
        self.on_alarm = self.on_error = self.on_state_change = self.on_connection = None
        self.on_stream_done = self.on_message = None
        self.calls: list[tuple] = []

    def list_ports(self):  # type: ignore[no-untyped-def]
        return [("COM9", "porta de teste")]

    def connect(self, port: str, baud: int = 115200) -> None:
        self.calls.append(("connect", port, baud))
        threading.Thread(target=lambda: self.on_connection(True)).start()

    def jog(self, dx: float, dy: float, dz: float, feed: float) -> None:
        self.calls.append(("jog", dx, dy, dz, feed))

    def disconnect(self) -> None:
        self.calls.append(("disconnect",))


def test_grbl_bridge_signals(qapp):
    from dataclasses import dataclass
    from PySide6.QtCore import QThread
    from brokcam.ui.machine_panel import GrblBridge

    @dataclass
    class Status:
        state: str
        wpos: tuple
        mpos: tuple

    ctrl = FakeController()
    bridge = GrblBridge()
    bridge.attach(ctrl)
    got: dict[str, object] = {}
    threads: list[object] = []

    def rec(name):  # type: ignore[no-untyped-def]
        def f(*a):  # type: ignore[no-untyped-def]
            got[name] = a
            threads.append(QThread.currentThread())
        return f

    bridge.status.connect(rec("status"))
    bridge.line.connect(rec("line"))
    bridge.progress.connect(rec("progress"))
    bridge.alarm.connect(rec("alarm"))
    bridge.error.connect(rec("error"))
    bridge.state.connect(rec("state"))
    bridge.connection.connect(rec("connection"))

    def worker() -> None:
        ctrl.on_status(Status("Idle", (1.0, 2.0, 3.0), (4.0, 5.0, 6.0)))
        ctrl.on_line("rx", "ok")
        ctrl.on_progress(10, 8, 100)
        ctrl.on_alarm(1, "fim de curso")
        ctrl.on_error(22, "avanço", 5)
        ctrl.on_state_change("Idle", "Run")
        ctrl.on_connection(True)

    t = threading.Thread(target=worker)
    t.start()
    t.join()
    assert wait_until(lambda: len(got) == 7, 5)
    assert got["status"][0]["wpos"] == (1.0, 2.0, 3.0)
    assert got["progress"] == (10, 8, 100)
    assert got["error"] == (22, "avanço", 5)
    assert got["state"] == ("Run",)
    main = qapp.thread()
    assert all(th is main for th in threads)   # todos os slots rodaram na thread da GUI


def test_machine_panel_with_fake_controller(qapp):
    from brokcam.ui.machine_panel import MachinePanel
    ctrl = FakeController()
    panel = MachinePanel(controller=ctrl)
    panel.show()
    assert "COM9" in [panel.port.itemText(i) for i in range(panel.port.count())]
    panel.port.setCurrentText("COM9")
    panel.toggle_connection()
    assert wait_until(lambda: panel.connected, 5)
    ctrl.on_status({"state": "Idle", "MPos": "10,20,-1", "WCO": "1,2,0"})
    assert wait_until(lambda: panel.dro_w[0].text() != "—", 5)
    assert panel.dro_w[0].text().startswith("9,000")
    assert "Ocioso" in panel.chip.text()
    panel._jog(1, 0, 0)
    assert wait_until(lambda: any(c[0] == "jog" for c in ctrl.calls), 5)
    assert next(c for c in ctrl.calls if c[0] == "jog")[1] == 1.0     # passo padrão 1 mm
    ctrl.on_error(20, "", None)
    assert wait_until(lambda: panel.banner.isVisible(), 5)
    panel.load_program("G21\n(comentário)\nG0 X1\nG1 X2 F100\n", "teste")
    assert panel.n_commands == 3 and panel.start_btn.isEnabled()
    panel.close()


# --------------------------------------------------------------------------- tema / projeto

def test_theme_contrast_aa(qapp):
    from brokcam.ui.theme import DARK, HIGH_CONTRAST, LIGHT, contrast_ratio
    for pal in (DARK, LIGHT, HIGH_CONTRAST):
        for bg in (pal.window, pal.surface, pal.surface_alt):
            assert contrast_ratio(pal.text, bg) >= 4.5, (pal.name, bg)
            assert contrast_ratio(pal.text_muted, bg) >= 4.5, (pal.name, "muted", bg)
        assert contrast_ratio(pal.accent_text, pal.accent) >= 4.5, pal.name
        assert contrast_ratio(pal.selection_text, pal.selection) >= 4.5, pal.name


def test_icons_render(qapp):
    from brokcam.ui.icons import available, icon
    names = available()
    assert len(names) > 50
    for n in names:
        pm = icon(n).pixmap(24, 24)
        assert not pm.isNull()


def test_project_roundtrip(qapp, tmp_path):
    from brokcam.core.model import GeometryLayer
    from brokcam.ui.project import LayerEntry, Project, Transform, apply_transform, read_project_file
    src = tmp_path / "x.gbr"
    src.write_text("dummy")
    lay = GeometryLayer("contorno", shapely.box(0, 0, 10, 5).boundary, str(src))
    proj = Project()
    e = proj.add_layer(LayerEntry(lay, "outline"))
    proj.set_layer_transform(e.id, Transform("x", (5.0, 2.5), (1.0, 0.0)))
    out = tmp_path / "p.bcam"
    proj.save(str(out))
    data = read_project_file(str(out))
    assert data["layers"][0]["role"] == "outline"
    t = Transform.from_json(data["layers"][0]["transform"])
    assert t.mirror == "x" and t.offset == (1.0, 0.0)
    b = apply_transform(lay, t).bounds
    assert b == pytest.approx((1.0, 0.0, 11.0, 5.0))


def test_canvas_fit_and_pick(qapp):
    from brokcam.core.model import GerberLayer
    from brokcam.ui.canvas import CanvasView
    view = CanvasView()
    view.resize(800, 600)
    view.show()
    lay = GerberLayer("t", "", shapely.box(10, 10, 30, 20))
    view.add_layer(lay.id, lay, "#e08a3c")
    view.fit_all()
    wait(10)
    c = view.mapFromScene(QPointF(20, 15))
    assert view.pick(QPointF(c)) == lay.id
    assert view.pick(QPointF(5, 5)) == -1
    # Y para cima: um ponto acima na cena aparece mais acima na tela
    assert view.mapFromScene(QPointF(20, 19)).y() < view.mapFromScene(QPointF(20, 11)).y()
    img = view.grab()
    assert not img.isNull()
    view.set_mode("measure")
    assert view.mode == "measure"
    view.close()


def test_preferences_and_manual(qapp):
    from brokcam.ui.help_dialogs import OptionsManual, collect_groups, manual_html
    from brokcam.ui.preferences import PreferencesDialog
    from brokcam.ui.settings import AppSettings
    groups = collect_groups()
    html = manual_html(groups)
    for g in groups:
        assert g.title.replace("&", "&amp;") in html or g.title in html
    dlg = PreferencesDialog(AppSettings())
    assert dlg.cats.count() >= 1 and "ui" in dlg.forms
    dlg.apply()
    man = OptionsManual()
    man._filter("profundidade")
    man.close()
    dlg.close()
    man.deleteLater()
    dlg.deleteLater()
    wait(10)


def test_wizard_pipeline(qapp):
    """O trabalho em segundo plano do assistente: ler, espelhar, gerar e pós-processar."""
    from brokcam.ui import backend
    if not all(backend.status()[k] for k in ("parsers", "cam", "gcode")):
        pytest.skip("módulos de domínio indisponíveis")
    from brokcam.ui.welcome import example_files
    from brokcam.ui.wizard import WizardFile, _make_job
    files = [WizardFile(p, backend.guess_role(p)) for p in example_files()]
    assert any(f.role == "top_copper" for f in files)
    fn = _make_job(files, True, ["isolation", "drilling", "cutout"], {}, {}, 1000.0)
    msgs: list[str] = []
    res = fn(lambda f, m: msgs.append(m), lambda: False)
    ops = {j.operation for _i, j in res.jobs}
    assert {"isolation", "drilling", "cutout"} <= ops
    order = [j.operation for _i, j in res.jobs]
    assert order.index("isolation") < order.index("cutout")        # recorte sempre por último
    assert res.program and all(j.gcode for _i, j in res.jobs)
    mirrored = [t for _l, role, t, _id in res.layers if role == "bottom_copper"]
    assert all(t.mirror == "x" for t in mirrored)
    assert res.estimate_s > 0


def test_wizard_dialog_pages(qapp):
    from brokcam.ui.settings import AppSettings
    from brokcam.ui.welcome import example_files
    from brokcam.ui.wizard import PcbWizard, WizardFile
    from brokcam.ui.workers import TaskRunner
    from brokcam.ui import backend
    wiz = PcbWizard(AppSettings(), TaskRunner(), initial=[WizardFile(p, backend.guess_role(p)) for p in example_files()])
    wiz.show()
    assert wiz.import_page.table.rowCount() == len(example_files())
    assert wiz.import_page.isComplete()
    wiz.next()
    assert wiz.currentPage() is wiz.config_page
    wiz.reject()
    wiz.deleteLater()
    wait(10)
