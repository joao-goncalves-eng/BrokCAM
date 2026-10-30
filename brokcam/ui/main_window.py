"""Janela principal do BrokCAM."""
from __future__ import annotations

import logging
import os
import time
from typing import Any

from PySide6.QtCore import QByteArray, QSize, Qt, QTimer
from PySide6.QtGui import (
    QAction, QActionGroup, QCloseEvent, QDragEnterEvent, QDragLeaveEvent, QDropEvent, QKeySequence,
    QUndoStack,
)
from PySide6.QtWidgets import (
    QDockWidget, QFileDialog, QFrame, QHBoxLayout, QScrollArea, QLabel, QMainWindow, QMessageBox,
    QProgressBar, QStackedWidget, QTabWidget, QToolBar, QToolButton, QWidget,
)

from brokcam.core.model import DrillLayer, GeometryLayer, GerberLayer, Job

from . import backend, theme, units
from .canvas import PROGRAM_KEY, CanvasView
from .align_dialog import AlignDialog
from .commands import (
    AddJobCmd, AddLayersCmd, AffineCmd, ClearJobGcodeCmd, GroupCmd, EditJobOpsCmd, FlipLayersCmd, MoveAllCmd, RemoveJobCmd, RemoveLayerCmd, RoleCmd,
    TransformCmd,
)
from .dock_title import DockTitleBar
from .double_sided import DoubleSidedDialog
from .gcode_editor import GcodePanel
from .help_dialogs import AboutDialog, OptionsManual, ShortcutsDialog, quick_guide
from .icons import icon
from .log_panel import LogPanel
from .machine_config import MachineConfigPanel
from .machine_tools import MachineToolsPanel
from .machine_panel import MachinePanel, StateChip
from .operation_panel import OperationPanel
from .preferences import PreferencesDialog
from .project import JobEntry, LayerEntry, Project, Transform, read_project_file
from .project_panel import ProjectPanel
from .ribbon import Ribbon, SegmentedControl
from .settings import AppSettings
from .tool_library import ToolLibraryDialog, library
from .welcome import WelcomeView, example_files
from .wizard import FILE_FILTER, PcbWizard, WizardFile, WizardResult
from .workers import Task, TaskRunner

#: versão do layout salvo (saveState/restoreState). 3 = painéis da máquina separados.
LAYOUT_VERSION = 3

log = logging.getLogger("brokcam.ui")

GCODE_EXTS = (".nc", ".gcode", ".ngc", ".tap", ".gc", ".cnc")
PROJECT_EXT = ".bcam"
PROJECT_EXTS = (".bcam", ".tcam")      # .tcam: projetos do tempo em que o programa se chamava TraceCAM
ROLE_Z = {"outline": 6.0, "drill": 7.0, "top_copper": 3.0, "bottom_copper": 2.0, "silk": 4.0,
          "mask": 1.0, "paste": 1.5, "unknown": 2.5}


def safe_gcode_filename(name: str, max_len: int = 60) -> str:
    """Nome de arquivo seguro para cartão SD / Candle / GRBL a partir do nome de um caminho.

    Só ASCII (sem "ç", "Ø"…), espaços e símbolos viram ``_``, e a vírgula/ponto DECIMAL vira
    ``-`` — "Ø0,4 mm" vira ``0-4mm`` (e não "04", que pareceria 4 mm). Sem extensão.
    """
    import re
    import unicodedata
    s = str(name or "")
    s = re.sub(r"(?<=\d)[,.](?=\d)", "-", s)                 # 0,4 / 0.4 -> 0-4
    s = re.sub(r"(?<=\d)\s+(mm|in|pol)\b", r"\1", s)          # "0-4 mm" -> "0-4mm"
    s = s.replace("Ø", "").replace("ø", "").replace("—", "_").replace("–", "_")
    s = unicodedata.normalize("NFKD", s).encode("ascii", "ignore").decode("ascii")
    s = re.sub(r"[^A-Za-z0-9\-]+", "_", s)
    s = re.sub(r"_+", "_", s).strip("_-")
    s = s[:max_len].rstrip("_-")
    return s or "programa"


class MainWindow(QMainWindow):
    def __init__(self, settings: AppSettings | None = None, *, controller: Any | None = None) -> None:
        super().__init__()
        self.settings = settings or AppSettings()
        self.project = Project(self)
        self.undo = QUndoStack(self)
        self.runner = TaskRunner(self)
        self.current_task: Task | None = None
        self.last_dir = str(self.settings.get("last_dir", os.path.expanduser("~")))
        self.gcode_source: tuple[str, int] = ("", -1)    # ("job"|"file"|"program", id)
        self._last_line_update = 0.0
        self._pending_line = -1
        self._stream_preview = False
        self._preview_src: tuple[str, int] | None = None     # qual G-code está desenhado no canvas

        self.setWindowTitle("BrokCAM")
        self.setAcceptDrops(True)
        self.setDockNestingEnabled(True)
        self.setDocumentMode(True)
        self.resize(1440, 900)

        self._apply_ui_settings(initial=True)
        self._build_central()
        self._build_docks(controller)
        self._build_actions()
        self._build_menus()
        self._build_toolbar()
        self._build_statusbar()
        self._connect()
        self._restore_window()
        self._workspace = "prepare"
        self._apply_workspace_docks("prepare")
        self._update_title()
        self._update_actions()
        self.welcome.set_recent(self.settings.recent)
        log.info("BrokCAM pronto. Módulos: %s",
                 ", ".join(f"{k} {'ok' if v else 'pendente'}" for k, v in backend.status().items()))

    # =================================================================== construção
    def _build_central(self) -> None:
        self.stack = QStackedWidget()
        self.welcome = WelcomeView()
        self.canvas = CanvasView(opengl=bool(self.settings.ui("opengl")))
        self.canvas.circle_segments = int(self.settings.ui("circle_segments"))
        self.canvas.set_show_grid(bool(self.settings.ui("show_grid")))
        self.canvas.set_show_rulers(bool(self.settings.ui("show_rulers")))
        self.canvas.set_show_rapids(bool(self.settings.ui("show_rapids")))
        self.canvas.set_antialias(bool(self.settings.ui("antialias")))
        self.stack.addWidget(self.welcome)
        self.stack.addWidget(self.canvas)
        self.setCentralWidget(self.stack)

    def _dock(self, title: str, widget: QWidget, name: str, area: Qt.DockWidgetArea) -> QDockWidget:
        d = QDockWidget(title, self)
        d.setObjectName(name)
        d.setWidget(widget)
        d.setFeatures(QDockWidget.DockWidgetFeature.DockWidgetMovable
                      | QDockWidget.DockWidgetFeature.DockWidgetFloatable
                      | QDockWidget.DockWidgetFeature.DockWidgetClosable)
        d.setAccessibleName(f"Painel {title}")
        self.addDockWidget(area, d)
        return d

    @staticmethod
    def _scroll(widget: QWidget) -> QScrollArea:
        """Área de rolagem sem moldura: o tamanho mínimo do conteúdo não força o painel a crescer."""
        sc = QScrollArea()
        sc.setWidgetResizable(True)
        sc.setFrameShape(QFrame.Shape.NoFrame)
        sc.setWidget(widget)
        return sc

    def _build_docks(self, controller: Any | None) -> None:
        self.project_panel = ProjectPanel()
        self.props = OperationPanel(self.settings)
        self.log_panel = LogPanel()
        self.log_panel.install()
        self.gcode_panel = GcodePanel()
        # a máquina é dividida em painéis independentes (como no Candle): cada um pode ser
        # arrastado para qualquer borda, empilhado em abas ou destacado como janela
        self.machine = MachinePanel(controller=controller, confirm_send=bool(self.settings.ui("confirm_send")),
                                    split=True)
        L, R, B = (Qt.DockWidgetArea.LeftDockWidgetArea, Qt.DockWidgetArea.RightDockWidgetArea,
                   Qt.DockWidgetArea.BottomDockWidgetArea)

        self.dock_project = self._dock("Projeto", self.project_panel, "dock_project", L)
        self.dock_props = self._dock("Propriedades", self.props, "dock_props", R)
        self.dock_log = self._dock("Log", self.log_panel, "dock_log", B)
        self.dock_gcode = self._dock("G-code", self.gcode_panel, "dock_gcode", B)
        self.dock_machine = self._dock("Máquina", self._scroll(self.machine), "dock_machine", R)
        self.dock_jog = self._dock("Controle e jog", self._scroll(self.machine.jog_panel), "dock_jog", R)
        self.dock_program = self._dock("Programa", self._scroll(self.machine.program_panel), "dock_program", B)
        self.dock_console = self._dock("Console GRBL", self.machine.console_panel, "dock_console", B)
        self.dock_grbl = self._dock("Ajustes $$", self.machine.settings_panel, "dock_grbl", B)
        self.machine_config = MachineConfigPanel(self.machine.bridge)
        self.dock_machine_cfg = self._dock("Config. máquina", self._scroll(self.machine_config), "dock_machine_cfg", B)
        self.machine_tools = MachineToolsPanel(
            self.machine.bridge, self.settings,
            get_program=lambda: ("\n".join(self.machine.program_lines), getattr(self.machine, "program_name", "programa")),
            get_board_bounds=lambda: self.project.board_bounds("outline"))
        self.machine_tools.programChanged.connect(self._on_program_autolevel)
        self.dock_tools = self._dock("Comandos e sonda", self._scroll(self.machine_tools), "dock_tools", B)
        # painéis que só existem no espaço de trabalho Máquina
        self.machine_docks = (self.dock_machine, self.dock_jog, self.dock_program, self.dock_console,
                              self.dock_grbl, self.dock_tools, self.dock_machine_cfg)
        self.all_docks = (self.dock_project, self.dock_props, self.dock_log, self.dock_gcode) + self.machine_docks
        # barra compacta (só botões quando em abas; título quando sozinho ou destacado)
        for d in (self.dock_log, self.dock_gcode) + self.machine_docks:
            d.setTitleBarWidget(DockTitleBar(d))
        self._bar_sync = QTimer(self)
        self._bar_sync.setSingleShot(True)
        self._bar_sync.setInterval(0)
        self._bar_sync.timeout.connect(self._sync_dock_bars)
        for d in self.all_docks:
            d.dockLocationChanged.connect(lambda _a: self._bar_sync.start())
            d.topLevelChanged.connect(lambda _f: self._bar_sync.start())
            d.visibilityChanged.connect(lambda _v: self._bar_sync.start())
        # arrastar um painel até uma borda mostra onde ele encaixa (animado) e encaixa ao soltar;
        # arrastar a barra de um grupo em abas leva o grupo inteiro
        self.setDockOptions(QMainWindow.DockOption.AnimatedDocks | QMainWindow.DockOption.AllowNestedDocks
                            | QMainWindow.DockOption.AllowTabbedDocks | QMainWindow.DockOption.GroupedDragging)
        self.setTabPosition(Qt.DockWidgetArea.AllDockWidgetAreas, QTabWidget.TabPosition.North)
        # colunas laterais ocupam a altura toda; o rodapé fica entre elas
        self.setCorner(Qt.Corner.BottomLeftCorner, L)
        self.setCorner(Qt.Corner.BottomRightCorner, R)
        self._default_prepare_layout()

    def _sync_dock_bars(self) -> None:
        for d in self.all_docks:
            bar = d.titleBarWidget()
            if isinstance(bar, DockTitleBar):
                bar.sync()

    def _detach_all_docks(self) -> None:
        """Tira todos os painéis da janela antes de montar um layout: o Qt não separa de
        forma confiável um painel de um grupo de abas existente com tabifyDockWidget."""
        for d in self.all_docks:
            d.setFloating(False)
            self.removeDockWidget(d)

    def _default_prepare_layout(self) -> None:
        """Preparar: Projeto à esquerda, Propriedades à direita, Log e G-code no rodapé."""
        L, R, B = (Qt.DockWidgetArea.LeftDockWidgetArea, Qt.DockWidgetArea.RightDockWidgetArea,
                   Qt.DockWidgetArea.BottomDockWidgetArea)
        self._detach_all_docks()
        self.addDockWidget(L, self.dock_project)
        self.addDockWidget(R, self.dock_props)
        self.addDockWidget(B, self.dock_log)
        self.tabifyDockWidget(self.dock_log, self.dock_gcode)
        prev = self.dock_gcode
        for d in self.machine_docks:          # escondidos neste espaço; ficam em abas no rodapé
            self.addDockWidget(B, d)
            self.tabifyDockWidget(prev, d)
            prev = d
        for d in (self.dock_project, self.dock_props, self.dock_log, self.dock_gcode):
            d.show()
        self.dock_log.raise_()
        self.resizeDocks([self.dock_project, self.dock_props], [290, 390], Qt.Orientation.Horizontal)
        self.resizeDocks([self.dock_log], [190], Qt.Orientation.Vertical)

    def _default_machine_layout(self) -> None:
        """Máquina (inspirado no Candle): canvas no centro; à direita o estado/DRO e, abaixo, o
        controle e jog; no rodapé o Programa (envio) ao lado do Console, com Comandos e sonda,
        Config. máquina, Log, G-code e Ajustes $$ em abas. Tudo pode ser rearranjado arrastando
        a barra de um painel até outra borda (ou sobre outro painel, para formar abas)."""
        R, B = Qt.DockWidgetArea.RightDockWidgetArea, Qt.DockWidgetArea.BottomDockWidgetArea
        self._detach_all_docks()
        self.dock_project.hide()
        self.dock_props.hide()
        self.addDockWidget(R, self.dock_machine)
        self.splitDockWidget(self.dock_machine, self.dock_jog, Qt.Orientation.Vertical)
        self.addDockWidget(B, self.dock_program)
        self.splitDockWidget(self.dock_program, self.dock_console, Qt.Orientation.Horizontal)
        prev = self.dock_console
        for d in (self.dock_tools, self.dock_machine_cfg, self.dock_log, self.dock_gcode, self.dock_grbl):
            self.tabifyDockWidget(prev, d)
            prev = d
        for d in self.machine_docks + (self.dock_log, self.dock_gcode):
            d.show()
        self.dock_console.raise_()
        w, h = max(self.width(), 900), max(self.height(), 600)
        self.resizeDocks([self.dock_machine], [390], Qt.Orientation.Horizontal)
        self.resizeDocks([self.dock_machine, self.dock_jog], [int(h * 0.42), int(h * 0.58)], Qt.Orientation.Vertical)
        self.resizeDocks([self.dock_program], [max(270, int(h * 0.32))], Qt.Orientation.Vertical)
        self.resizeDocks([self.dock_program, self.dock_console], [int((w - 390) * 0.45), int((w - 390) * 0.55)],
                         Qt.Orientation.Horizontal)

    def set_docks_locked(self, locked: bool) -> None:
        """Trava (ou destrava) a posição dos painéis: evita arrastar um painel sem querer
        durante a usinagem (Candle: "Lock panels"). Painéis continuam podendo ser fechados."""
        F = QDockWidget.DockWidgetFeature
        for d in self.all_docks:
            feats = F.DockWidgetClosable
            if not locked or d.isFloating():
                feats |= F.DockWidgetMovable | F.DockWidgetFloatable
            d.setFeatures(feats)
        if getattr(self, "a_lock_docks", None) is not None and self.a_lock_docks.isChecked() != locked:
            self.a_lock_docks.setChecked(locked)
        self._sync_dock_bars()

    def _act(self, text: str, slot: Any, icon_name: str | None = None, shortcut: Any = None,
             tip: str = "", checkable: bool = False) -> QAction:
        a = QAction(text, self)
        if icon_name:
            a.setIcon(icon(icon_name))
        if shortcut is not None:
            if isinstance(shortcut, (list, tuple)):
                a.setShortcuts([QKeySequence(s) for s in shortcut])
            else:
                a.setShortcut(QKeySequence(shortcut))
        if tip:
            a.setToolTip(tip)
            a.setStatusTip(tip)
        a.setCheckable(checkable)
        if checkable:
            a.toggled.connect(slot)
        else:
            a.triggered.connect(lambda _=False: slot())
        return a

    def _build_actions(self) -> None:
        A = self._act
        self.a_new = A("&Novo projeto", self.new_project, "new", QKeySequence.StandardKey.New, "Fechar tudo e começar um projeto vazio")
        self.a_open = A("&Abrir…", self.open_dialog, "open", QKeySequence.StandardKey.Open, "Abrir Gerber, Excellon, G-code ou projeto (Ctrl+O)")
        self.a_example = A("Abrir &exemplo", self.open_example, "board", None, "Carregar os arquivos de exemplo de tests/data")
        self.a_save = A("&Salvar projeto", self.save_project, "save", QKeySequence.StandardKey.Save, "Salvar o projeto (.bcam)")
        self.a_save_as = A("Salvar projeto &como…", self.save_project_as, None, QKeySequence("Ctrl+Shift+S"))
        self.a_export = A("&Exportar G-code…", self.export_gcode, "export", QKeySequence("Ctrl+E"), "Salvar o G-code do caminho selecionado ou de todos")
        self.a_wizard = A("&Assistente Gerber → GRBL…", self.open_wizard, "wizard", QKeySequence("Ctrl+Shift+W"), "Importar, configurar, gerar e exportar em 4 passos")
        self.a_prefs = A("&Preferências…", self.open_preferences, "settings", QKeySequence("Ctrl+,"), "Todas as opções do BrokCAM, com explicações")
        self.a_quit = A("Sai&r", self.close, None, QKeySequence.StandardKey.Quit)
        self.a_undo = self.undo.createUndoAction(self, "&Desfazer")
        self.a_undo.setIcon(icon("undo"))
        self.a_undo.setShortcut(QKeySequence.StandardKey.Undo)
        self.a_redo = self.undo.createRedoAction(self, "&Refazer")
        self.a_redo.setIcon(icon("redo"))
        self.a_redo.setShortcuts([QKeySequence.StandardKey.Redo, QKeySequence("Ctrl+Y")])
        self.a_remove = A("Re&mover selecionado", self.remove_selected, "trash", None, "Remover a camada ou caminho selecionado (Delete no painel Projeto)")
        self.a_mirror = A("&Espelhar camada selecionada", lambda: self._layer_action("mirror:x"), "mirror", None, "Espelhar horizontalmente em torno do centro da placa")
        self.a_fit = A("&Ajustar à tela", self.canvas.fit_all, "fit", None, "Enquadrar tudo (1 ou F no canvas)")
        self.a_zoom_in = A("Apro&ximar", lambda: self.canvas.zoom_by(1.25), "zoom-in", QKeySequence.StandardKey.ZoomIn)
        self.a_zoom_out = A("A&fastar", lambda: self.canvas.zoom_by(0.8), "zoom-out", QKeySequence.StandardKey.ZoomOut)
        self.a_measure = A("&Medir distância", self._toggle_measure, "measure", None, "Régua: clique em dois pontos (M no canvas)", checkable=True)
        self.a_grid = A("&Grade", self._set_grid, "grid", QKeySequence("G"), "Mostrar/ocultar a grade", checkable=True)
        self.a_grid.setChecked(self.canvas.show_grid)
        self.a_grid.setShortcutContext(Qt.ShortcutContext.WidgetWithChildrenShortcut)
        self.canvas.addAction(self.a_grid)
        self.a_rulers = A("&Réguas", self._set_rulers, "ruler", None, "Mostrar/ocultar réguas", checkable=True)
        self.a_rulers.setChecked(self.canvas.show_rulers)
        self.a_rapids = A("Deslocamentos &rápidos", self._set_rapids, None, None, "Mostrar movimentos G0 tracejados", checkable=True)
        self.a_rapids.setChecked(self.canvas.show_rapids)
        self.a_theme = A("Alternar &tema claro/escuro", self.toggle_theme, "contrast", QKeySequence("Ctrl+Shift+T"), "Alternar entre tema claro e escuro")
        self.a_hc = A("Alto &contraste", self._set_high_contrast, None, None, "Tema de alto contraste", checkable=True)
        self.a_hc.setChecked(bool(self.settings.ui("high_contrast")))
        self.a_font_up = A("Aumentar fonte", lambda: self._font_step(10), None, QKeySequence("Ctrl+="))
        self.a_font_down = A("Reduzir fonte", lambda: self._font_step(-10), None, QKeySequence("Ctrl+-"))
        self.a_units = A("Unidades em &polegadas", self._set_inches, None, None, "Exibir medidas em polegadas", checkable=True)
        self.a_units.setChecked(units.display_units() == "in")
        self.a_generate = A("&Gerar caminho / G-code", self.props._primary_clicked, "play", QKeySequence("Ctrl+Return"), "Executa a ação principal do painel Propriedades")
        self.a_gen_all_gcode = A("Gerar G-code de &todos os caminhos", self.generate_all_gcode, "gcode", None, "Pós-processa todos os caminhos visíveis num único programa")
        self.a_simulate = A("&Simular caminho", self.simulate_selected, "simulate", QKeySequence("Ctrl+R"), "Animar o caminho de ferramenta selecionado")
        self.a_cancel = A("&Cancelar tarefa", self.cancel_task, "cancel", None, "Cancelar a operação em andamento")
        self.a_guide = A("&Guia rápido", lambda: quick_guide(self).exec(), "help", QKeySequence.StandardKey.HelpContents)
        self.a_manual = A("&Manual de opções", self.open_manual, "book", QKeySequence("Shift+F1"), "Todas as opções explicadas, com busca")
        self.a_keys = A("&Atalhos de teclado", lambda: ShortcutsDialog(self).exec(), "keyboard")
        self.a_about = A("&Sobre o BrokCAM", lambda: AboutDialog(self).exec(), "info")
        self.a_tab_log = A("Log", lambda: self._show_dock(self.dock_log), "log", QKeySequence("Ctrl+1"))
        self.a_tab_gcode = A("G-code", lambda: self._show_dock(self.dock_gcode), "gcode", QKeySequence("Ctrl+2"))
        self.a_tab_machine = A("Máquina", lambda: self._show_dock(self.dock_machine), "machine", QKeySequence("Ctrl+3"))
        self.a_tab_tools = A("Comandos, sonda e &heightmap", lambda: self._show_dock(self.dock_tools), "probe",
                             QKeySequence("Ctrl+5"),
                             "Definir posição, ajuste fino de Z, sonda Z, autolevel (heightmap) e macros")
        self.a_tab_machine_cfg = A("&Configuração da máquina (status e $$)", lambda: self._show_dock(self.dock_machine_cfg),
                                   "settings", QKeySequence("Ctrl+4"),
                                   "Status completo da máquina e edição das configurações do GRBL / MKS DLC32")
        self.a_tab_program = A("Programa (envio)", lambda: self._show_dock(self.dock_program), "play",
                               QKeySequence("Ctrl+6"), "Painel de envio do programa: iniciar, pausar, parar, overrides")
        self.a_tab_console = A("Console GRBL", lambda: self._show_dock(self.dock_console), "terminal",
                               QKeySequence("Ctrl+7"), "Console para enviar comandos ao GRBL")
        self.a_reset_layout = A("Restaurar layout padrão", self.reset_layout, None, None,
                                "Recoloca os painéis do espaço de trabalho atual na posição padrão")
        self.a_lock_docks = A("Travar painéis", lambda: self.set_docks_locked(self.a_lock_docks.isChecked()),
                              None, None, "Impede mover/destacar os painéis sem querer (eles ainda podem ser fechados)")
        self.a_lock_docks.setCheckable(True)
        self.a_library = A("&Biblioteca de ferramentas…", self.open_library, "tool-library", QKeySequence("Ctrl+T"),
                           "Cadastrar ferramentas com seus dados de corte para reutilizar nas operações")
        self.a_align = A("&Posicionar placa no zero…", self.open_align, "move", QKeySequence("Ctrl+Shift+A"),
                         "Mover/girar a placa inteira (camadas e caminhos) para o zero de trabalho")
        self.a_align_quick = A("Placa no zero (canto inferior esquerdo)", self.align_quick, "zero",
                               QKeySequence("Ctrl+0"),
                               "Leva o canto inferior esquerdo da placa para X0 Y0 num clique")
        self.a_edit_paths = A("&Editar caminhos", self.edit_selected_paths, "edit-path", QKeySequence("E"),
                              "Selecionar e remover caminhos ou trechos inúteis do caminho selecionado")
        self.a_edit_paths.setShortcutContext(Qt.ShortcutContext.WidgetWithChildrenShortcut)
        # operações de CAM na faixa de comandos: escolhem a camada certa e abrem o formulário
        self.op_actions: dict[str, QAction] = {}
        for key, text, short, icon_name, tip in (
                ("isolation", "&Isolar trilhas", "Isolar", "isolation",
                 "Isolamento: contornar trilhas e pads para separá-los eletricamente"),
                ("ncc", "&Limpar cobre (NCC)", "Limpar", "ncc",
                 "Remover o cobre que não faz parte do circuito"),
                ("cutout", "&Recortar placa", "Recortar", "cutout",
                 "Recortar o contorno da placa, com pontes"),
                ("drilling", "&Furar", "Furar", "drill", "Furação, agrupada por diâmetro de broca"),
                ("panelize", "&Painelizar", "Painelizar", "panelize", "Repetir a placa em linhas × colunas")):
            a = A(text, lambda k=key: self.choose_operation(k), icon_name, None, tip)
            a.setIconText(short)
            self.op_actions[key] = a
        self.a_gcode = A("Gerar &G-code", self.generate_selected_gcode, "gcode", None,
                         "Gerar o G-code do caminho selecionado (ou de todos, se nenhum estiver selecionado)")
        self.a_gcode.setIconText("G-code")
        self.a_save_all_gcode = A("Salvar G-codes de vários caminhos…", self.save_all_gcode, "save",
                                  QKeySequence("Ctrl+Shift+E"),
                                  "Gera e salva numa pasta um arquivo .nc por caminho (os selecionados; senão os "
                                  "da camada selecionada; senão todos) — ex.: um G-code por broca")
        self.a_send = A("&Enviar à máquina", self.send_current_gcode, "send", None,
                        "Carregar o G-code atual no painel Máquina e mudar para o espaço de trabalho Máquina")
        self.a_send.setIconText("Enviar")
        self.a_conn = A("&Conectar / desconectar", self.machine.toggle_connection, "plug", None,
                        "Conectar à máquina (USB ou WiFi) ou desconectar")
        self.a_conn.setIconText("Conectar")
        self.a_home = A("&Home ($H)", self.machine.home, "home", None, "Ciclo de homing: buscar os fins de curso")
        self.a_home.setIconText("Home")
        self.a_unlock = A("&Destravar ($X)", self.machine.unlock, "unlock", None, "Destravar após alarme")
        self.a_unlock.setIconText("Destravar")
        self.a_double = A("&Dupla face (virar placa)…", self.open_double_sided, "mirror", QKeySequence("Ctrl+D"),
                          "Virar de uma vez cobre inferior, furos e contorno para fresar o lado de baixo")
        self.a_double.setIconText("Dupla face")
        self.a_rot_ccw = A("Girar 90° anti-horário", lambda: self.rotate_board(90.0), "rotate-ccw",
                           QKeySequence("Ctrl+Shift+R"),
                           "Girar a placa inteira (camadas e caminhos) 90° no sentido anti-horário")
        self.a_rot_ccw.setIconText("Girar")
        self.a_rot_cw = A("Girar 90° horário", lambda: self.rotate_board(-90.0), "rotate-cw", None,
                          "Girar a placa inteira 90° no sentido horário")
        self.a_rot_180 = A("Girar 180°", lambda: self.rotate_board(180.0), "rotate-ccw", None,
                           "Girar a placa inteira 180°")
        self.a_rot_free = A("Girar por ângulo…", self.rotate_board_free, "rotate-ccw", None,
                            "Girar a placa inteira por um ângulo qualquer (ex.: para alinhar com uma placa "
                            "virgem presa torta)")
        self.a_group = A("A&grupar selecionadas", self.group_selected, "group", QKeySequence("Ctrl+G"),
                         "Agrupar as camadas selecionadas: elas passam a se mover juntas e não podem ser "
                         "desalinhadas por acidente")
        self.a_group.setIconText("Agrupar")
        self.a_ungroup = A("Desa&grupar", self.ungroup_selected, "ungroup", QKeySequence("Ctrl+Shift+G"),
                           "Desfazer o grupo das camadas selecionadas")
        self.a_ungroup.setIconText("Desagrupar")
        self.a_toolwidth = A("Largura da &ferramenta", self._set_tool_width, "isolation", None,
                             "Desenhar a faixa real removida pela ferramenta (largura = diâmetro efetivo)",
                             checkable=True)
        self.a_toolwidth.setChecked(bool(self.settings.get("show_tool_width", True)))
        self.canvas.set_show_tool_width(self.a_toolwidth.isChecked())
        self.a_rapids.setIcon(icon("feed"))
        self.a_rulers.setIcon(icon("ruler"))
        for a, t in ((self.a_open, "Abrir"), (self.a_save, "Salvar"), (self.a_export, "Exportar"),
                     (self.a_align, "Posicionar"), (self.a_measure, "Medir"), (self.a_library, "Biblioteca"),
                     (self.a_edit_paths, "Editar"), (self.a_simulate, "Simular"), (self.a_wizard, "Assistente"),
                     (self.a_undo, "Desfazer"), (self.a_redo, "Refazer"), (self.a_remove, "Remover"),
                     (self.a_align_quick, "Canto no zero"), (self.a_mirror, "Espelhar")):
            a.setIconText(t)
        self.canvas.addAction(self.a_edit_paths)
        self.project_panel.addAction(self.a_edit_paths)

    def _build_menus(self) -> None:
        mb = self.menuBar()
        m = mb.addMenu("&Arquivo")
        m.addActions([self.a_new, self.a_open])
        self.recent_menu = m.addMenu(icon("restore"), "Abrir &recente")
        self.recent_menu.aboutToShow.connect(self._fill_recent)
        m.addAction(self.a_example)
        m.addSeparator()
        m.addActions([self.a_save, self.a_save_as])
        m.addSeparator()
        m.addActions([self.a_export, self.a_save_all_gcode, self.a_wizard])
        m.addSeparator()
        m.addAction(self.a_quit)

        m = mb.addMenu("&Editar")
        m.addActions([self.a_undo, self.a_redo])
        m.addSeparator()
        m.addActions([self.a_remove, self.a_mirror])
        m.addSeparator()
        m.addActions([self.a_align, self.a_align_quick, self.a_double, self.a_edit_paths])
        rot = m.addMenu(icon("rotate-ccw"), "&Girar placa")
        rot.addActions([self.a_rot_ccw, self.a_rot_cw, self.a_rot_180, self.a_rot_free])
        m.addSeparator()
        m.addActions([self.a_group, self.a_ungroup])
        m.addSeparator()
        m.addAction(self.a_library)
        m.addAction(self.a_prefs)

        m = mb.addMenu("E&xibir")
        m.addActions([self.a_fit, self.a_zoom_in, self.a_zoom_out])
        m.addSeparator()
        m.addActions([self.a_grid, self.a_rulers, self.a_rapids, self.a_toolwidth, self.a_measure])
        m.addSeparator()
        theme_menu = m.addMenu(icon("contrast"), "&Tema")
        grp = QActionGroup(self)
        self.theme_actions: dict[str, QAction] = {}
        for key, lab in (("system", "Seguir o sistema"), ("dark", "Escuro"), ("light", "Claro")):
            a = QAction(lab, self, checkable=True)
            a.setChecked(self.settings.ui("theme") == key)
            a.triggered.connect(lambda _=False, k=key: self.set_theme(k))
            grp.addAction(a)
            theme_menu.addAction(a)
            self.theme_actions[key] = a
        theme_menu.addSeparator()
        theme_menu.addActions([self.a_theme, self.a_hc])
        m.addActions([self.a_font_up, self.a_font_down, self.a_units])
        m.addSeparator()
        panels = m.addMenu("&Painéis")
        for d in self.all_docks:
            panels.addAction(d.toggleViewAction())
        panels.addSeparator()
        panels.addActions([self.a_tab_log, self.a_tab_gcode, self.a_tab_machine, self.a_tab_tools,
                           self.a_tab_machine_cfg, self.a_tab_program, self.a_tab_console])
        panels.addSeparator()
        panels.addActions([self.a_lock_docks, self.a_reset_layout])

        m = mb.addMenu("&Operações")
        m.addActions(list(self.op_actions.values()))
        m.addSeparator()
        m.addActions([self.a_wizard, self.a_generate, self.a_gen_all_gcode, self.a_simulate])
        m.addSeparator()
        m.addActions([self.a_library, self.a_edit_paths])
        m.addSeparator()
        m.addAction(self.a_cancel)

        m = mb.addMenu("&Máquina")
        m.addActions([self.a_conn, self.a_home, self.a_unlock])
        m.addSeparator()
        m.addAction(self.a_send)
        m.addAction(self.a_tab_machine)
        m.addAction(self.a_tab_program)
        m.addAction(self.a_tab_console)
        m.addAction(self.a_tab_tools)
        m.addAction(self.a_tab_machine_cfg)

        m = mb.addMenu("Aj&uda")
        m.addActions([self.a_guide, self.a_manual, self.a_keys])
        m.addSeparator()
        m.addAction(self.a_about)

    def _build_toolbar(self) -> None:
        """Faixa de comandos agrupada (substitui a antiga barra só de ícones)."""
        rb = Ribbon()
        from PySide6.QtWidgets import QMenu

        g = rb.group("Arquivo")
        open_menu = QMenu(self)
        open_menu.addActions([self.a_new, self.a_example])
        open_menu.addMenu(self.recent_menu)
        open_menu.addSeparator()
        open_menu.addAction(self.a_save_as)
        g.add(self.a_open, open_menu)
        g.add(self.a_save)

        g = rb.group("Placa")
        align_menu = QMenu(self)
        align_menu.addActions([self.a_align_quick, self.a_rot_ccw, self.a_double])
        g.add(self.a_align, align_menu)
        rot_menu = QMenu(self)
        rot_menu.addActions([self.a_rot_ccw, self.a_rot_cw, self.a_rot_180])
        rot_menu.addSeparator()
        rot_menu.addAction(self.a_rot_free)
        g.add_small(self.a_rot_ccw, rot_menu)
        g.add_small(self.a_double)

        g = rb.group("Caminhos")
        for key in ("isolation", "ncc", "cutout", "drilling"):
            g.add(self.op_actions[key], accent=key == "isolation")
        g.add_small(self.op_actions["panelize"])
        g.add_small(self.a_library)
        g.add_small(self.a_wizard)

        g = rb.group("Editar")
        g.add(self.a_edit_paths)
        g.add_small(self.a_group)
        g.add_small(self.a_ungroup)
        g.add_small(self.a_remove)

        g = rb.group("Saída")
        g.add(self.a_simulate)
        gcode_menu = QMenu(self)
        gcode_menu.addActions([self.a_gcode, self.a_gen_all_gcode])
        gcode_menu.addSeparator()
        gcode_menu.addActions([self.a_export, self.a_save_all_gcode])
        g.add(self.a_gcode, gcode_menu)
        g.add(self.a_send)

        self.workspace = SegmentedControl([("prepare", "Preparar", "layers"), ("machine", "Máquina", "machine")],
                                          "Espaço de trabalho")
        self.workspace.setToolTip("Preparar: arquivos, operações e caminhos. Máquina: controle da CNC, "
                                  "envio do programa e configurações (Ctrl+Tab alterna).")
        self.workspace.changed.connect(self.set_workspace)
        # acesso rápido (como no Fusion 360): desfazer/refazer sempre à mão
        for a in (self.a_undo, self.a_redo):
            qb = QToolButton()
            qb.setDefaultAction(a)
            qb.setAutoRaise(True)
            qb.setIconSize(QSize(18, 18))
            qb.setAccessibleName(a.text().replace("&", ""))
            rb.add_right(qb)
        rb.add_right(self.workspace)
        for a in (self.a_theme, self.a_prefs, self.a_guide):
            b = QToolButton()
            b.setDefaultAction(a)
            b.setAutoRaise(True)
            b.setIconSize(QSize(18, 18))
            b.setAccessibleName(a.text().replace("&", "").replace("…", ""))
            rb.add_right(b)

        tb = QToolBar("Faixa de comandos")
        tb.setObjectName("RibbonBar")
        tb.setMovable(False)
        tb.setFloatable(False)
        tb.setAccessibleName("Faixa de comandos")
        tb.addWidget(rb)
        tb.toggleViewAction().setEnabled(False)
        self.addToolBar(tb)
        self.toolbar = tb
        self.ribbon = rb
        self.a_ws_toggle = self._act("Alternar espaço de trabalho", lambda: self.set_workspace(
            "machine" if self.workspace.current() == "prepare" else "prepare"), None, QKeySequence("Ctrl+Tab"))
        self.addAction(self.a_ws_toggle)

        # navegação flutuante no canto do canvas
        self.canvas.nav_bar.set_actions([[self.a_fit, self.a_zoom_out, "zoom", self.a_zoom_in], [self.a_measure],
                                         [self.a_toolwidth, self.a_grid, self.a_rulers, self.a_rapids]])

    def _build_statusbar(self) -> None:
        sb = self.statusBar()
        sb.setSizeGripEnabled(False)
        self.task_box = QWidget()
        tl = QHBoxLayout(self.task_box)
        tl.setContentsMargins(0, 0, 0, 0)
        tl.setSpacing(6)
        self.task_label = QLabel()
        self.task_bar = QProgressBar()
        self.task_bar.setRange(0, 1000)
        self.task_bar.setMaximumWidth(160)
        self.task_bar.setMaximumHeight(12)
        self.task_bar.setTextVisible(False)
        self.task_bar.setAccessibleName("Progresso da tarefa")
        self.task_cancel = QToolButton()
        self.task_cancel.setIcon(icon("cancel"))
        self.task_cancel.setToolTip("Cancelar tarefa")
        self.task_cancel.setAccessibleName("Cancelar tarefa")
        self.task_cancel.setAutoRaise(True)
        self.task_cancel.clicked.connect(self.cancel_task)
        tl.addWidget(self.task_label)
        tl.addWidget(self.task_bar)
        tl.addWidget(self.task_cancel)
        self.task_box.hide()
        sb.addWidget(self.task_box)
        self.measure_label = QLabel()
        self.measure_label.setAccessibleName("Medição")
        sb.addPermanentWidget(self.measure_label)
        self.coord_label = QLabel("X —  Y —")
        self.coord_label.setMinimumWidth(190)
        self.coord_label.setStyleSheet(f"font-family: '{theme.mono_font_family()}';")
        self.coord_label.setAccessibleName("Coordenadas do cursor")
        sb.addPermanentWidget(self.coord_label)
        self.units_btn = QToolButton()
        self.units_btn.setAutoRaise(True)
        self.units_btn.setToolTip("Unidade de exibição (clique para alternar mm/pol)")
        self.units_btn.setAccessibleName("Alternar unidade de exibição")
        self.units_btn.clicked.connect(lambda: self.a_units.toggle())
        sb.addPermanentWidget(self.units_btn)
        self.grbl_chip = StateChip()
        self.grbl_chip.setToolTip("Estado da máquina GRBL (clique na aba Máquina para detalhes)")
        sb.addPermanentWidget(self.grbl_chip)
        self._update_units_label()

    def _connect(self) -> None:
        p = self.project
        p.layerAdded.connect(self._on_layer_added)
        p.layerRemoved.connect(self._on_layer_removed)
        p.layerChanged.connect(self._on_layer_changed)
        p.layerGeometryChanged.connect(self._on_layer_geometry)
        p.jobAdded.connect(self._on_job_added)
        p.jobRemoved.connect(self._on_job_removed)
        p.jobChanged.connect(self._on_job_changed)
        p.jobGeometryChanged.connect(self._on_job_geometry)
        p.cleared.connect(self._on_cleared)
        p.modifiedChanged.connect(lambda _m: self._update_title())
        p.pathChanged.connect(lambda _p: self._update_title())

        pp = self.project_panel
        pp.selected.connect(self._on_tree_selected)
        pp.visibilityToggled.connect(self._on_visibility)
        pp.action.connect(self._on_tree_action)
        pp.openRequested.connect(self.open_dialog)
        pp.batch.connect(self._on_batch)
        pp.group_name_of = self.project.group_name
        pp.multiSelected.connect(self._on_multi_selected)

        self.canvas.cursorMoved.connect(self._on_cursor)
        self.canvas.itemClicked.connect(self._on_canvas_click)
        self.canvas.measureChanged.connect(self._on_measure)
        self.canvas.editAction.connect(self._on_edit_action)
        self.canvas.editModeChanged.connect(self._on_edit_mode)

        pr = self.props
        pr.generateRequested.connect(self.generate_path)
        pr.generateOnceRequested.connect(lambda lid, key, vals: self.generate_path(lid, key, vals, remember=False))
        pr.drillHighlight.connect(self.canvas.highlight_drill)
        pr.cancelRequested.connect(self.cancel_task)
        pr.gcodeRequested.connect(self.generate_gcode)
        pr.simulateRequested.connect(self.simulate_job)
        pr.saveGcodeRequested.connect(self.save_job_gcode)
        pr.sendRequested.connect(self.send_job)
        pr.roleChanged.connect(lambda lid, role: self.undo.push(RoleCmd(self.project, lid, role)))
        pr.colorChanged.connect(self.project.set_layer_color)
        pr.transformRequested.connect(self._on_layer_transform)
        pr.wizardRequested.connect(self.open_wizard)
        pr.libraryRequested.connect(lambda uid: self.open_library(uid or None))
        pr.editPathsRequested.connect(self.start_edit_paths)
        pr.alignRequested.connect(self.open_align)
        pr.batchRequested.connect(self._on_batch)
        pr.gcodeAction.connect(self._on_gcode_action)
        pr.openRequested.connect(self.open_dialog)

        w = self.welcome
        w.openRequested.connect(self.open_dialog)
        w.wizardRequested.connect(self.open_wizard)
        w.exampleRequested.connect(self.open_example)
        w.recentRequested.connect(lambda path: self.open_files([path]))

        g = self.gcode_panel
        g.saveRequested.connect(self.save_gcode_text)
        g.sendRequested.connect(self.send_current_gcode)
        g.previewRequested.connect(self.preview_current_gcode)
        g.openRequested.connect(self.open_gcode_dialog)

        m = self.machine
        m.toolPosition.connect(self._on_tool_position)
        m.stateChanged.connect(self.grbl_chip.set_state)
        m.programLine.connect(self._on_program_line)
        m.streamingChanged.connect(self._on_streaming)

        self.runner.progress.connect(self._on_task_progress)
        self.runner.ended.connect(self._on_task_ended)
        self.undo.cleanChanged.connect(lambda _c: self._update_actions())
        theme.on_change(self, lambda _p: self._update_units_label())

    # =================================================================== configurações
    def _apply_ui_settings(self, initial: bool = False) -> None:
        s = self.settings
        units.set_display_units(str(s.ui("units")))
        theme.manager().configure(mode=str(s.ui("theme")), high_contrast=bool(s.ui("high_contrast")),
                                  font_scale=int(s.ui("font_scale")) / 100.0)
        if not initial:
            self.canvas.circle_segments = int(s.ui("circle_segments"))
            self.canvas.set_opengl(bool(s.ui("opengl")))
            self.canvas.set_show_grid(bool(s.ui("show_grid")))
            self.canvas.set_show_rulers(bool(s.ui("show_rulers")))
            self.canvas.set_show_rapids(bool(s.ui("show_rapids")))
            self.canvas.set_antialias(bool(s.ui("antialias")))
            self.machine.confirm_send = bool(s.ui("confirm_send"))
            for key, a in getattr(self, "theme_actions", {}).items():
                a.setChecked(key == s.ui("theme"))
            self.a_hc.setChecked(bool(s.ui("high_contrast")))
            self.a_units.blockSignals(True)
            self.a_units.setChecked(units.display_units() == "in")
            self.a_units.blockSignals(False)
            for a, key in ((self.a_grid, "show_grid"), (self.a_rulers, "show_rulers"), (self.a_rapids, "show_rapids")):
                a.blockSignals(True)
                a.setChecked(bool(s.ui(key)))
                a.blockSignals(False)
            self._refresh_units_dependent()

    def _refresh_units_dependent(self) -> None:
        self._update_units_label()
        for e in self.project.layers:
            self.project_panel.update_layer(e)
        for j in self.project.jobs:
            self.project_panel.update_job(j)
        self.props.refresh()
        self.canvas.viewport().update()

    def _update_units_label(self) -> None:
        if hasattr(self, "units_btn"):
            self.units_btn.setText(units.unit_label())

    def set_theme(self, mode: str) -> None:
        self.settings.set_ui("theme", mode)
        self.settings.save()
        theme.manager().configure(mode=mode)

    def toggle_theme(self) -> None:
        cur = theme.palette().dark
        self.settings.set_ui("high_contrast", False)
        self.a_hc.setChecked(False)
        mode = "light" if cur else "dark"
        self.set_theme(mode)
        if mode in self.theme_actions:
            self.theme_actions[mode].setChecked(True)

    def _set_high_contrast(self, on: bool) -> None:
        self.settings.set_ui("high_contrast", on)
        self.settings.save()
        theme.manager().configure(high_contrast=on)

    def _font_step(self, delta: int) -> None:
        v = max(100, min(200, int(self.settings.ui("font_scale")) + delta))
        self.settings.set_ui("font_scale", v)
        self.settings.save()
        theme.manager().configure(font_scale=v / 100.0)
        self.statusBar().showMessage(f"Escala da fonte: {v}%", 2500)

    def _set_inches(self, on: bool) -> None:
        self.settings.set_ui("units", "in" if on else "mm")
        units.set_display_units("in" if on else "mm")
        self._refresh_units_dependent()

    def _set_grid(self, on: bool) -> None:
        self.settings.set_ui("show_grid", on)
        self.canvas.set_show_grid(on)

    def _set_rulers(self, on: bool) -> None:
        self.settings.set_ui("show_rulers", on)
        self.canvas.set_show_rulers(on)

    def _set_rapids(self, on: bool) -> None:
        self.settings.set_ui("show_rapids", on)
        self.canvas.set_show_rapids(on)

    def _toggle_measure(self, on: bool) -> None:
        self.canvas.set_mode("measure" if on else "select")
        if on:
            self.canvas.setFocus()

    def open_manual(self) -> None:
        dlg = getattr(self, "_manual", None)
        if dlg is None:
            dlg = OptionsManual(self)
            self._manual = dlg
        dlg.show()
        dlg.raise_()
        dlg.activateWindow()

    def open_wizard(self) -> None:
        initial = [WizardFile(e.layer.source_path or e.name, e.role, layer=e.layer, layer_id=e.id)
                   for e in self.project.layers]
        wiz = PcbWizard(self.settings, self.runner, self, initial, self.last_dir)
        accepted = wiz.exec() == PcbWizard.DialogCode.Accepted
        wiz.deleteLater()
        if not accepted or wiz.result is None:
            self.last_dir = wiz.last_dir or self.last_dir
            return
        self.last_dir = wiz.last_dir or self.last_dir
        self._apply_wizard_result(wiz.result)
        if wiz.send_after:
            self.send_current_gcode()

    def _apply_wizard_result(self, res: WizardResult) -> None:
        self.undo.beginMacro("Assistente Gerber → GRBL")
        try:
            entries: list[LayerEntry] = []
            new: list[LayerEntry] = []
            for lay, role, t, lid in res.layers:
                e = self.project.layer(lid) if lid is not None else None
                if e is None:
                    e = LayerEntry(lay, role, transform=t)
                    new.append(e)
                else:
                    if e.role != role:
                        self.undo.push(RoleCmd(self.project, e.id, role))
                    if e.transform.to_json() != t.to_json():
                        self.undo.push(TransformCmd(self.project, e.id, t))
                entries.append(e)
            if new:
                self.undo.push(AddLayersCmd(self.project, new))
                if len(new) > 1:
                    name = self.project.suggest_group_name()
                    self.undo.push(GroupCmd(self.project, [e.id for e in new], self.project.new_group_id(), name))
            rapid = float(self.settings.ui("rapid_rate"))
            for idx, job in res.jobs:
                je = JobEntry(job, entries[idx].id)
                je.estimate_s = backend.estimate_job_time_s(job, rapid)
                self.undo.push(AddJobCmd(self.project, je))
        finally:
            self.undo.endMacro()
        for line in res.log:
            log.info("Assistente: %s", line)
        if res.program:
            self.gcode_source = ("program", -1)
            self._set_gcode(res.program, "Programa completo", res.estimate_s)
        QTimer.singleShot(0, self.canvas.fit_all)

    def open_preferences(self, start_key: str | None = None) -> None:
        dlg = PreferencesDialog(self.settings, self, start_key)
        dlg.applied.connect(lambda: self._apply_ui_settings())
        dlg.exec()
        dlg.deleteLater()
        self.props.refresh()

    # =================================================================== janela
    def _restore_window(self) -> None:
        win = self.settings.get("window", {}) or {}
        self._layout_restored = False
        try:
            if win.get("geometry"):
                self.restoreGeometry(QByteArray.fromBase64(win["geometry"].encode()))
            if win.get("state"):
                # layouts de versões antigas (painéis da máquina juntos no rodapé) são ignorados
                self._layout_restored = self.restoreState(QByteArray.fromBase64(win["state"].encode()),
                                                          LAYOUT_VERSION)
            self._ws_states = {}
            if win.get("ws_machine"):
                self._ws_states["machine"] = QByteArray.fromBase64(win["ws_machine"].encode())
            if win.get("locked"):
                QTimer.singleShot(0, lambda: self.set_docks_locked(True))
        except Exception:  # noqa: BLE001
            log.warning("Não foi possível restaurar o layout da janela")


    def reset_layout(self) -> None:
        """Recoloca os painéis do espaço de trabalho atual na posição padrão."""
        ws = getattr(self, "_workspace", "prepare")
        if ws == "machine":
            self._default_machine_layout()
        else:
            self._default_prepare_layout()
        if hasattr(self, "_ws_states"):
            self._ws_states.pop(ws, None)
        self._apply_workspace_docks(ws)
        self._sync_dock_bars()

    def _show_dock(self, dock: QDockWidget) -> None:
        if dock in getattr(self, "machine_docks", ()) and getattr(self, "_workspace", "prepare") != "machine":
            self.set_workspace("machine")      # painéis de máquina só existem no espaço Máquina
        dock.show()
        dock.raise_()
        if (not dock.isFloating() and self.dockWidgetArea(dock) == Qt.DockWidgetArea.BottomDockWidgetArea
                and dock.height() < 200):
            self.resizeDocks([dock], [240], Qt.Orientation.Vertical)
        dock.widget().setFocus()

    def _update_title(self) -> None:
        name = os.path.basename(self.project.path) if self.project.path else "Sem título"
        mod = " •" if self.project.modified and not self.project.is_empty() else ""
        self.setWindowTitle(f"{name}{mod} — BrokCAM")

    def _update_actions(self) -> None:
        has = not self.project.is_empty()
        kind, _id = self.project_panel.current()
        for a in (self.a_save, self.a_save_as):
            a.setEnabled(has)
        self.a_remove.setEnabled(bool(kind))
        self.a_mirror.setEnabled(kind == "layer")
        self.a_simulate.setEnabled(kind == "job")
        kinds = {e.kind for e in self.project.layers}
        ops = backend.operations()
        for key, a in self.op_actions.items():
            op = ops.get(key)
            a.setEnabled(op is not None and bool(kinds & set(op.accepts)))
        self.a_gcode.setEnabled(bool(self.project.jobs))
        self.a_edit_paths.setEnabled(kind == "job" or self.canvas.edit_key is not None)
        self.a_align.setEnabled(bool(self.project.layers or self.project.jobs))
        self.a_double.setEnabled(bool(self.project.layers))
        for a in (self.a_rot_ccw, self.a_rot_cw, self.a_rot_180, self.a_rot_free):
            a.setEnabled(bool(self.project.layers))
        sel_layers = [i for k, i in self.project_panel.selected_items() if k == "layer"]
        self.a_group.setEnabled(len(sel_layers) >= 2)
        self.a_ungroup.setEnabled(any((e := self.project.layer(i)) is not None and e.group is not None
                                      for i in sel_layers))
        self.a_align_quick.setEnabled(bool(self.project.layers or self.project.jobs))
        self.a_export.setEnabled(any(j.job.gcode for j in self.project.jobs) or bool(self.project.jobs))
        self.a_gen_all_gcode.setEnabled(bool(self.project.jobs))
        self.a_save_all_gcode.setEnabled(bool(self.project.jobs))
        self.a_cancel.setEnabled(self.runner.busy())
        self.a_example.setEnabled(bool(example_files()))
        self.stack.setCurrentWidget(self.welcome if self.project.is_empty() and self.canvas.gcode_item is None
                                    else self.canvas)

    def closeEvent(self, ev: QCloseEvent) -> None:  # noqa: N802
        if self.machine.streaming:
            if QMessageBox.question(self, "Programa em execução",
                                    "Um programa está sendo enviado à máquina. Fechar interrompe o envio. Fechar mesmo assim?") \
                    != QMessageBox.StandardButton.Yes:
                ev.ignore()
                return
        if not self._maybe_save():
            ev.ignore()
            return
        self.runner.cancel_all()
        self.runner.wait(5000)
        self.machine.shutdown()
        self.settings.set("window", self._window_layout_settings())
        self.settings.set("last_dir", self.last_dir)
        self.settings.save()
        self.log_panel.uninstall()
        ev.accept()

    def _window_layout_settings(self) -> dict[str, Any]:
        """Geometria + layout de cada espaço de trabalho (o Máquina é lembrado à parte)."""
        def b64(state: QByteArray) -> str:
            return bytes(state.toBase64()).decode()
        if not hasattr(self, "_ws_states"):
            self._ws_states = {}
        ws = getattr(self, "_workspace", "prepare")
        self._ws_states[ws] = self.saveState(LAYOUT_VERSION)
        out: dict[str, Any] = {"geometry": b64(self.saveGeometry()),
                               "state": b64(self._ws_states.get("prepare", self._ws_states[ws])),
                               "locked": bool(self.a_lock_docks.isChecked())}
        if "machine" in self._ws_states:
            out["ws_machine"] = b64(self._ws_states["machine"])
        return out

    def _maybe_save(self) -> bool:
        if not self.project.modified or self.project.is_empty():
            return True
        box = QMessageBox(QMessageBox.Icon.Question, "Salvar alterações?",
                          "O projeto tem alterações não salvas. Deseja salvá-las?",
                          QMessageBox.StandardButton.Save | QMessageBox.StandardButton.Discard
                          | QMessageBox.StandardButton.Cancel, self)
        box.button(QMessageBox.StandardButton.Save).setText("Salvar")
        box.button(QMessageBox.StandardButton.Discard).setText("Não salvar")
        box.button(QMessageBox.StandardButton.Cancel).setText("Cancelar")
        r = box.exec()
        if r == QMessageBox.StandardButton.Save:
            return self.save_project()
        return r == QMessageBox.StandardButton.Discard

    # ------------------------------------------------------------------ arrastar e soltar
    def dragEnterEvent(self, ev: QDragEnterEvent) -> None:  # noqa: N802
        if ev.mimeData().hasUrls():
            ev.acceptProposedAction()
            self.welcome.set_drag_active(True)

    def dragLeaveEvent(self, ev: QDragLeaveEvent) -> None:  # noqa: N802
        self.welcome.set_drag_active(False)

    def dropEvent(self, ev: QDropEvent) -> None:  # noqa: N802
        self.welcome.set_drag_active(False)
        paths = [u.toLocalFile() for u in ev.mimeData().urls() if u.isLocalFile()]
        files: list[str] = []
        for p in paths:
            if os.path.isdir(p):
                files += [os.path.join(p, f) for f in sorted(os.listdir(p)) if os.path.isfile(os.path.join(p, f))]
            else:
                files.append(p)
        if files:
            ev.acceptProposedAction()
            self.open_files(files)

    # =================================================================== arquivos
    def new_project(self) -> None:
        if not self._maybe_save():
            return
        self.project.clear()
        self.undo.clear()
        self.gcode_panel.set_text("")

    def open_dialog(self) -> None:
        flt = (f"Todos os suportados (*{PROJECT_EXT} *.tcam *.gbr *.ger *.gtl *.gbl *.gko *.gm1 *.gml *.gto *.gbo *.gts *.gbs "
               f"*.gtp *.gbp *.drl *.xln *.exc *.txt *.drd *.cmp *.sol *.dim *.art *.pho *.nc *.gcode *.ngc *.tap);;"
               f"Projeto BrokCAM (*{PROJECT_EXT} *.tcam);;" + FILE_FILTER.replace(";;Todos os arquivos (*)", "")
               + ";;G-code (*.nc *.gcode *.ngc *.tap);;Todos os arquivos (*)")
        paths, _ = QFileDialog.getOpenFileNames(self, "Abrir", self.last_dir, flt)
        if paths:
            self.open_files(paths)

    def open_gcode_dialog(self) -> None:
        path, _ = QFileDialog.getOpenFileName(self, "Abrir G-code", self.last_dir,
                                              "G-code (*.nc *.gcode *.ngc *.tap *.gc *.cnc);;Todos os arquivos (*)")
        if path:
            self.open_files([path])

    def open_example(self) -> None:
        files = example_files()
        if not files:
            QMessageBox.information(self, "Exemplo", "Nenhum arquivo de exemplo encontrado em tests/data.")
            return
        self.open_files(files)

    def _fill_recent(self) -> None:
        self.recent_menu.clear()
        rec = [p for p in self.settings.recent if os.path.exists(p)]
        for p in rec:
            a = self.recent_menu.addAction(os.path.basename(p))
            a.setToolTip(p)
            a.triggered.connect(lambda _=False, path=p: self.open_files([path]))
        if not rec:
            a = self.recent_menu.addAction("(nenhum)")
            a.setEnabled(False)
        self.recent_menu.addSeparator()
        self.recent_menu.addAction("Limpar lista", lambda: (self.settings.clear_recent(), self.settings.save(),
                                                          self.welcome.set_recent([])))

    def open_files(self, paths: list[str]) -> None:
        if not paths:
            return
        self.last_dir = os.path.dirname(os.path.abspath(paths[0]))
        projects = [p for p in paths if p.lower().endswith(PROJECT_EXTS)]
        gcodes = [p for p in paths if p.lower().endswith(GCODE_EXTS)]
        layers = [p for p in paths if p not in projects and p not in gcodes]
        if projects:
            self.open_project(projects[0])
            return
        for g in gcodes[:1]:
            self.load_gcode_file(g)
        if layers:
            self.load_layers(layers)

    def load_layers(self, paths: list[str]) -> None:
        if not backend.status()["parsers"]:
            QMessageBox.warning(self, "Leitores indisponíveis",
                                "O módulo de leitura de Gerber/Excellon ainda não está disponível.")
            return

        def work(progress: Any, cancel: Any) -> list[tuple[str, Any]]:
            out: list[tuple[str, Any]] = []
            for i, p in enumerate(paths):
                if cancel():
                    break
                progress(i / len(paths), f"Lendo {os.path.basename(p)}…")
                try:
                    out.append((p, backend.load_file(p)))
                except Exception as exc:  # noqa: BLE001 - reportado ao usuário
                    out.append((p, exc))
            progress(1.0, "")
            return out

        self._submit(f"Abrindo {len(paths)} arquivo(s)", work, self._on_layers_loaded)

    def _on_layers_loaded(self, results: list[tuple[str, Any]]) -> None:
        entries: list[LayerEntry] = []
        errors: list[str] = []
        for path, res in results:
            if isinstance(res, Exception):
                errors.append(f"{os.path.basename(path)}: {res}")
                log.error("Falha ao abrir %s: %s", path, res)
                continue
            role = backend.guess_role(path)
            if isinstance(res, DrillLayer) and role not in ("drill",):
                role = "drill"
            entries.append(LayerEntry(res, role))
            for w in getattr(res, "warnings", []) or []:
                log.warning("%s: %s", os.path.basename(path), w)
            log.info("Aberto %s (%s)", os.path.basename(path), role)
            self.settings.add_recent(path)
        was_empty = self.project.is_empty()
        if entries:
            self.undo.beginMacro(f"Abrir {len(entries)} arquivo(s)")
            self.undo.push(AddLayersCmd(self.project, entries))
            if len(entries) > 1 and bool(self.settings.get("auto_group", True)):
                name = self.project.suggest_group_name()
                self.undo.push(GroupCmd(self.project, [e.id for e in entries], self.project.new_group_id(), name,
                                        f"Agrupar como {name}"))
                self.statusBar().showMessage(
                    f"{len(entries)} arquivos agrupados como “{name}”: eles se movem juntos. "
                    "Use Desagrupar (Ctrl+Shift+G) se quiser movê-los separadamente.", 8000)
            self.undo.endMacro()
            self.settings.save()
            self.welcome.set_recent(self.settings.recent)
            if was_empty:
                QTimer.singleShot(0, self.canvas.fit_all)
            self.project_panel.select("layer", entries[0].id)
        if errors:
            QMessageBox.warning(self, "Alguns arquivos não foram abertos", "\n".join(errors[:12]))

    def load_gcode_file(self, path: str) -> None:
        try:
            with open(path, encoding="utf-8", errors="replace") as fh:
                text = fh.read()
        except OSError as exc:
            QMessageBox.warning(self, "G-code", f"Não foi possível ler {path}:\n{exc}")
            return
        self.settings.add_recent(path)
        self.settings.save()
        self.gcode_source = ("file", -1)
        self._set_gcode(text, os.path.basename(path))
        self.preview_current_gcode()

    # ------------------------------------------------------------------ projeto
    def save_project(self) -> bool:
        if not self.project.path:
            return self.save_project_as()
        return self._write_project(self.project.path)

    def save_project_as(self) -> bool:
        start = self.project.path or os.path.join(self.last_dir, "placa" + PROJECT_EXT)
        path, _ = QFileDialog.getSaveFileName(self, "Salvar projeto", start, f"Projeto BrokCAM (*{PROJECT_EXT})")
        if not path:
            return False
        if not path.lower().endswith(PROJECT_EXT):
            path += PROJECT_EXT
        return self._write_project(path)

    def _write_project(self, path: str) -> bool:
        grp = backend.grbl_post_group()
        if grp is not None:
            self.project.post_settings = self.settings.group_values(grp)
        try:
            self.project.save(path)
        except OSError as exc:
            QMessageBox.warning(self, "Salvar projeto", f"Não foi possível salvar:\n{exc}")
            return False
        self.settings.add_recent(path)
        self.settings.save()
        self.undo.setClean()
        self.statusBar().showMessage(f"Projeto salvo em {path}", 4000)
        log.info("Projeto salvo: %s", path)
        return True

    def open_project(self, path: str) -> None:
        if not self._maybe_save():
            return
        try:
            data = read_project_file(path)
        except Exception as exc:  # noqa: BLE001
            QMessageBox.warning(self, "Abrir projeto", f"Não foi possível abrir {path}:\n{exc}")
            return
        layers_spec = data.get("layers", [])
        jobs_spec = data.get("jobs", [])

        def work(progress: Any, cancel: Any) -> dict[str, Any]:
            loaded: list[Any] = []
            n = max(1, len(layers_spec) + len(jobs_spec))
            for i, ls in enumerate(layers_spec):
                if cancel():
                    break
                progress(i / n, f"Lendo {ls.get('name') or os.path.basename(ls.get('path', ''))}…")
                try:
                    if ls.get("inline"):
                        from brokcam.core.serialize import layer_from_json
                        loaded.append(layer_from_json(ls["inline"]))
                        continue
                    loaded.append(backend.load_file(ls["path"]))
                except Exception as exc:  # noqa: BLE001
                    loaded.append(exc)
            from .project import apply_transform
            ops = backend.operations()
            jobs: list[tuple[int, Any]] = []
            for k, js in enumerate(jobs_spec):
                if cancel():
                    break
                idx = int(js.get("source", -1))
                op = ops.get(js.get("operation", ""))
                if js.get("ops") and 0 <= idx < len(loaded) and not isinstance(loaded[idx], Exception):
                    # caminhos editados à mão: restaura exatamente como foram salvos
                    from brokcam.core.serialize import ops_from_json
                    job = Job(name=js.get("name") or "Caminho editado", source_id=loaded[idx].id,
                              ops=ops_from_json(js["ops"]), operation=js.get("operation", ""),
                              settings=dict(js.get("settings") or {}))
                    jobs.append((idx, job))
                    continue
                if op is None or not (0 <= idx < len(loaded)) or isinstance(loaded[idx], Exception):
                    jobs.append((idx, RuntimeError(f"Não foi possível regenerar {js.get('name', '?')}")))
                    continue
                progress((len(layers_spec) + k) / n, f"Regenerando {js.get('name', op.title)}…")
                t = Transform.from_json(layers_spec[idx].get("transform"))
                try:
                    job = backend.run_operation(op, apply_transform(loaded[idx], t), js.get("settings", {}),
                                                cancel=cancel)
                    if isinstance(job, list):
                        # cada job separado guarda o próprio filtro; uma lista aqui só viria de um
                        # arquivo editado à mão — mantém o primeiro para não duplicar itens
                        job = job[0] if job else RuntimeError(f"Nada a regenerar em {js.get('name', '?')}")
                    if isinstance(job, Job):
                        job.name = js.get("name") or job.name
                    jobs.append((idx, job))
                except Exception as exc:  # noqa: BLE001
                    jobs.append((idx, exc))
            return {"layers": loaded, "jobs": jobs}

        def done(res: dict[str, Any]) -> None:
            self.project.blockSignals(False)
            self.project.clear()
            self.undo.clear()
            errors: list[str] = []
            entries: list[LayerEntry | None] = []
            for ls, lay in zip(layers_spec, res["layers"]):
                if isinstance(lay, Exception):
                    errors.append(f"{ls.get('path')}: {lay}")
                    entries.append(None)
                    continue
                e = LayerEntry(lay, ls.get("role", "unknown"), ls.get("color", ""), bool(ls.get("visible", True)),
                               Transform.from_json(ls.get("transform")))
                if ls.get("group") is not None:
                    e.group = int(ls["group"])
                self.project.add_layer(e)
                entries.append(e)
            for (idx, job), js in zip(res["jobs"], jobs_spec):
                if isinstance(job, Exception):
                    errors.append(str(job))
                    continue
                if isinstance(job, Job) and 0 <= idx < len(entries) and entries[idx] is not None:
                    je = JobEntry(job, entries[idx].id, bool(js.get("visible", True)))  # type: ignore[union-attr]
                    je.estimate_s = backend.estimate_job_time_s(job, float(self.settings.ui("rapid_rate")))
                    self.project.add_job(je)
            groups = {int(k): v for k, v in (data.get("groups") or {}).items()}
            if groups:
                self.project.set_layer_groups({}, groups)
                for e in self.project.layers:
                    self.project.layerChanged.emit(e)
            grp = backend.grbl_post_group()
            if grp is not None and data.get("post"):
                self.settings.set_group_values(grp, grp.validate({**grp.defaults(), **data["post"]}))
            self.project.set_path(path)
            self.project.set_modified(False)
            self.settings.add_recent(path)
            self.settings.save()
            self.welcome.set_recent(self.settings.recent)
            QTimer.singleShot(0, self.canvas.fit_all)
            log.info("Projeto aberto: %s", path)
            if errors:
                QMessageBox.warning(self, "Projeto aberto com avisos", "\n".join(errors[:12]))

        self._submit("Abrindo projeto", work, done)

    # =================================================================== tarefas
    def _submit(self, title: str, fn: Any, on_done: Any, show_in_props: bool = False) -> Task:
        task = self.runner.submit(title, fn, on_done=on_done,
                                  on_error=lambda msg, tb: self._task_error(title, msg),
                                  on_cancel=lambda: self.statusBar().showMessage(f"{title}: cancelado", 4000))
        self.current_task = task
        self.task_label.setText(title)
        self.task_bar.setValue(0)
        self.task_box.show()
        if show_in_props:
            self.props.set_busy(True, 0.0, title)
        self._update_actions()
        return task

    def _task_error(self, title: str, msg: str) -> None:
        log.error("%s falhou: %s", title, msg)
        QMessageBox.warning(self, title, f"A operação falhou:\n\n{msg}\n\nDetalhes técnicos no painel Log.")

    def _on_task_progress(self, task: Task, frac: float, msg: str) -> None:
        if task is not self.current_task:
            return
        self.task_bar.setValue(int(frac * 1000))
        if msg:
            self.task_label.setText(msg)
        if self.props.busy:
            self.props.set_busy(True, frac, msg)

    def _on_task_ended(self, task: Task) -> None:
        if not self.runner.busy():
            self.task_box.hide()
            self.props.set_busy(False)
            self.current_task = None
        self._update_actions()

    def cancel_task(self) -> None:
        self.runner.cancel_all()

    # =================================================================== CAM
    def generate_path(self, layer_id: int, op_key: str, values: dict[str, Any], remember: bool = True) -> None:
        """Gera caminho(s) de ``op_key`` para a camada. ``remember=False`` não grava ``values``
        como padrão do formulário (ex.: "Gerar só este diâmetro", que é pontual)."""
        e = self.project.layer(layer_id)
        op = backend.operations().get(op_key)
        if e is None or op is None:
            return
        warns = self.props.form.warnings() if self.props.form is not None else {}
        if warns:
            msg = "\n".join(f"• {op.group.spec(k).label}: {w}" for k, w in warns.items())
            if QMessageBox.question(self, "Verifique os parâmetros",
                                    f"Alguns valores parecem incorretos:\n\n{msg}\n\nGerar mesmo assim?") \
                    != QMessageBox.StandardButton.Yes:
                return
        if remember:
            self.settings.set_group_values(op.group, values)
            self.settings.save()
        layer = e.effective

        def work(progress: Any, cancel: Any) -> Any:
            return backend.run_operation(op, layer, values, progress=progress, cancel=cancel)

        def done(result: Any) -> None:
            if isinstance(result, list):
                # furação com um trabalho por broca: todos entram juntos (Ctrl+Z desfaz tudo)
                jobs = [r for r in result if isinstance(r, Job)]
                if not jobs:
                    QMessageBox.information(self, op.title, "Nenhum furo para gerar com as opções atuais.")
                    return
                rapid = float(self.settings.ui("rapid_rate"))
                entries = []
                taken = {j.name for j in self.project.jobs}
                self.undo.beginMacro("Furação por broca" if op.key == "drilling" else f"{op.title} ({len(jobs)})")
                try:
                    for job in jobs:
                        job.name = self._unique_job_name(job.name, taken)
                        taken.add(job.name)
                        je = JobEntry(job, e.id)
                        je.estimate_s = backend.estimate_job_time_s(job, rapid)
                        self.undo.push(AddJobCmd(self.project, je))
                        entries.append(je)
                finally:
                    self.undo.endMacro()
                log.info("%s: %d trabalhos separados gerados para %s", op.title, len(entries), e.name)
                self.statusBar().showMessage(
                    f"{len(entries)} trabalhos de {op.title.lower()} criados — um G-code por broca "
                    f"(Ctrl+Z desfaz todos).", 6000)
                self.project_panel.select("job", entries[0].id)
                return
            if isinstance(result, Job):
                if not result.name or result.name == op.key:
                    result.name = f"{op.title} — {e.name}"
                result.name = self._unique_job_name(result.name)
                lt = library().get(values.get("library_tool"))
                if lt is not None:
                    result.settings["library_tool"] = lt.uid
                if lt is not None and op.key != "drilling":
                    # nomeia a ferramenta como na biblioteca (aparece na mensagem de troca do G-code)
                    for pop in result.ops:
                        if pop.kind == "mill" and abs(pop.tool.number - lt.number) == 0:
                            pop.tool.name = lt.name
                je = JobEntry(result, e.id)
                je.estimate_s = backend.estimate_job_time_s(result, float(self.settings.ui("rapid_rate")))
                self.undo.push(AddJobCmd(self.project, je))
                st = result.stats()
                log.info("%s gerado: %.1f mm de corte, %d furos", result.name, st["cut_length_mm"], st["holes"])
                self.project_panel.select("job", je.id)
            elif isinstance(result, (GerberLayer, DrillLayer, GeometryLayer)):
                ne = LayerEntry(result, e.role)
                self.undo.push(AddLayersCmd(self.project, [ne]))
                self.project_panel.select("layer", ne.id)
                QTimer.singleShot(0, self.canvas.fit_all)
            else:
                log.warning("A operação %s não retornou resultado utilizável", op.key)

        self._submit(f"{op.title} em {e.name}", work, done, show_in_props=True)

    def _unique_job_name(self, name: str, taken: set[str] | None = None) -> str:
        """``name`` ou ``name (2)``, ``name (3)``… se já houver um caminho com esse nome."""
        taken = taken if taken is not None else {j.name for j in self.project.jobs}
        if name not in taken:
            return name
        n = 2
        while f"{name} ({n})" in taken:
            n += 1
        return f"{name} ({n})"

    def _post_values(self) -> dict[str, Any]:
        grp = backend.grbl_post_group()
        return self.settings.group_values(grp) if grp is not None else {}

    def generate_gcode(self, job_id: int, post: dict[str, Any] | None = None) -> None:
        j = self.project.job(job_id)
        if j is None:
            return
        grp = backend.grbl_post_group()
        if grp is None:
            QMessageBox.information(self, "G-code", "O pós-processador GRBL ainda não está disponível.")
            return
        post = post if post is not None else self._post_values()
        self.settings.set_group_values(grp, post)
        self.settings.save()
        rapid = float(self.settings.ui("rapid_rate"))
        job = j.job

        def work(progress: Any, cancel: Any) -> tuple[str, float]:
            progress(0.1, "Gerando G-code…")
            text = backend.generate_gcode(job, post)
            progress(0.7, "Estimando tempo…")
            pv = backend.parse_gcode(text, rapid)
            return text, pv.estimate_time_s

        def done(res: tuple[str, float]) -> None:
            text, est = res
            j.job.gcode = text
            if est:
                j.estimate_s = est
            self.project.jobChanged.emit(j)
            self.gcode_source = ("job", j.id)
            self._set_gcode(text, j.name, est)
            self._show_dock(self.dock_gcode)
            log.info("G-code gerado para %s: %d linhas", j.name, text.count("\n") + 1)
            if self.project_panel.current() == ("job", j.id):
                self.props.show_job(j)

        self._submit(f"G-code de {j.name}", work, done, show_in_props=True)

    def generate_all_gcode(self) -> None:
        jobs = [j for j in self.project.jobs if j.visible] or list(self.project.jobs)
        if not jobs:
            return
        if backend.grbl_post_group() is None:
            QMessageBox.information(self, "G-code", "O pós-processador GRBL ainda não está disponível.")
            return
        post = self._post_values()
        rapid = float(self.settings.ui("rapid_rate"))
        order = {"isolation": 0, "ncc": 1, "drilling": 2, "cutout": 3}
        ordered = sorted(jobs, key=lambda j: order.get(j.job.operation, 9))

        def work(progress: Any, cancel: Any) -> tuple[str, float]:
            progress(0.1, "Gerando programa…")
            text = backend.generate_program([j.job for j in ordered], post)
            pv = backend.parse_gcode(text, rapid)
            return text, pv.estimate_time_s

        def done(res: tuple[str, float]) -> None:
            text, est = res
            self.gcode_source = ("program", -1)
            self._set_gcode(text, "Programa completo", est)
            self._show_dock(self.dock_gcode)
            log.info("Programa completo gerado (%d caminhos)", len(ordered))

        self._submit("Gerando programa completo", work, done)

    def _set_gcode(self, text: str, name: str, estimate_s: float = 0.0) -> None:
        self.gcode_name = name
        self.gcode_panel.set_text(text, estimate_s)
        if not text.strip() and self._preview_src == ("program", 0):
            self._hide_preview()
        self._update_actions()
        self._sync_gcode_tree()

    def preview_current_gcode(self) -> None:
        text = self.gcode_panel.text()
        if not text.strip():
            return
        pv = backend.parse_gcode(text, float(self.settings.ui("rapid_rate")))
        self.canvas.set_gcode_preview(pv)
        kind, id_ = self.gcode_source
        self._preview_src = ("gcode", id_) if kind == "job" else ("program", 0)
        self._sync_gcode_tree()
        self.stack.setCurrentWidget(self.canvas)
        if pv.estimate_time_s and not self.gcode_panel.estimate_s:
            self.gcode_panel.estimate_s = pv.estimate_time_s
            self.gcode_panel._update_info()
        if self.project.is_empty():
            QTimer.singleShot(0, self.canvas.fit_all)

    def save_gcode_text(self, text: str | None = None, name: str | None = None) -> None:
        text = text if text is not None else self.gcode_panel.text()
        if not text.strip():
            return
        safe = safe_gcode_filename(name or getattr(self, "gcode_name", "programa"))
        path, _ = QFileDialog.getSaveFileName(self, "Salvar G-code", os.path.join(self.last_dir, safe + ".nc"),
                                              "G-code (*.nc *.gcode *.ngc *.tap);;Todos os arquivos (*)")
        if not path:
            return
        try:
            with open(path, "w", encoding="utf-8", newline="\n") as fh:
                fh.write(text if text.endswith("\n") else text + "\n")
        except OSError as exc:
            QMessageBox.warning(self, "Salvar G-code", f"Não foi possível salvar:\n{exc}")
            return
        self.last_dir = os.path.dirname(path)
        self.statusBar().showMessage(f"G-code salvo em {path}", 4000)
        log.info("G-code salvo: %s", path)

    def _jobs_to_save(self) -> list[JobEntry]:
        """Caminhos alvo de "Salvar G-codes…": os selecionados na árvore; senão os da camada
        selecionada; senão todos."""
        sel = [j for k, i in self.project_panel.selected_items() if k == "job"
               and (j := self.project.job(i)) is not None]
        if sel:
            return sel
        kind, id_ = self.project_panel.current()
        if kind == "layer" and self.project.jobs_for(id_):
            return self.project.jobs_for(id_)
        return list(self.project.jobs)

    def save_all_gcode(self, job_ids: list[int] | None = None) -> None:
        """Gera (se preciso) e grava o G-code de vários caminhos — um arquivo por caminho —
        numa pasta escolhida, com nomes seguros (:func:`safe_gcode_filename`). Nunca
        sobrescreve sem perguntar."""
        jobs = ([j for i in job_ids if (j := self.project.job(i)) is not None] if job_ids is not None
                else self._jobs_to_save())
        if not jobs:
            QMessageBox.information(self, "Salvar G-codes", "Não há caminhos para salvar. Gere um caminho primeiro.")
            return
        grp = backend.grbl_post_group()
        if grp is None:
            QMessageBox.information(self, "G-code", "O pós-processador GRBL ainda não está disponível.")
            return
        folder = QFileDialog.getExistingDirectory(
            self, f"Pasta para salvar {len(jobs)} G-code{'s' if len(jobs) != 1 else ''}", self.last_dir)
        if not folder:
            return
        names: list[str] = []
        for j in jobs:
            base = safe_gcode_filename(j.name)
            n, cand = 2, base
            while cand.lower() in {x.lower() for x in names}:
                cand = f"{base}_{n}"
                n += 1
            names.append(cand)
        paths = [os.path.join(folder, n + ".nc") for n in names]
        existing = [p for p in paths if os.path.exists(p)]
        if existing:
            lst = "\n".join("• " + os.path.basename(p) for p in existing[:8]) + ("\n…" if len(existing) > 8 else "")
            ans = QMessageBox.question(
                self, "Arquivos já existem",
                f"{len(existing)} arquivo(s) já existe(m) nesta pasta:\n\n{lst}\n\n"
                "Sim = substituir esses arquivos.\nNão = manter os existentes e salvar os novos com outro "
                "nome (_2, _3…).\nCancelar = não salvar nada.",
                QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No | QMessageBox.StandardButton.Cancel,
                QMessageBox.StandardButton.No)
            if ans == QMessageBox.StandardButton.Cancel:
                return
            if ans == QMessageBox.StandardButton.No:
                for k, p in enumerate(paths):
                    if p in existing:
                        stem, n = os.path.splitext(p)[0], 2
                        while os.path.exists(f"{stem}_{n}.nc") or f"{stem}_{n}.nc" in paths:
                            n += 1
                        paths[k] = f"{stem}_{n}.nc"
        post = self._post_values()
        rapid = float(self.settings.ui("rapid_rate"))
        todo = [(j, j.job) for j in jobs]

        def work(progress: Any, cancel: Any) -> list[tuple[str, float]]:
            out: list[tuple[str, float]] = []
            for k, (j, job) in enumerate(todo):
                if cancel():
                    break
                progress(k / max(len(todo), 1), f"G-code de {j.name}…")
                if job.gcode:
                    out.append((job.gcode, 0.0))
                else:
                    text = backend.generate_gcode(job, post)
                    out.append((text, backend.parse_gcode(text, rapid).estimate_time_s))
            return out

        def done(res: list[tuple[str, float]]) -> None:
            saved: list[str] = []
            for (j, _job), (text, est), path in zip(todo, res, paths):
                if not j.job.gcode:
                    j.job.gcode = text
                    if est:
                        j.estimate_s = est
                    self.project.jobChanged.emit(j)
                try:
                    with open(path, "w", encoding="utf-8", newline="\n") as fh:
                        fh.write(text if text.endswith("\n") else text + "\n")
                except OSError as exc:
                    QMessageBox.warning(self, "Salvar G-codes", f"Não foi possível salvar {path}:\n{exc}")
                    break
                saved.append(path)
            self.last_dir = folder
            self._sync_gcode_tree()
            if saved:
                log.info("%d G-code(s) salvos em %s: %s", len(saved), folder,
                         ", ".join(os.path.basename(p) for p in saved))
                self.statusBar().showMessage(f"{len(saved)} G-code(s) salvos em {folder}", 6000)

        self._submit(f"Salvando {len(jobs)} G-code(s)", work, done)

    def save_job_gcode(self, job_id: int) -> None:
        j = self.project.job(job_id)
        if j is not None and j.job.gcode:
            self.save_gcode_text(j.job.gcode, j.name)

    def export_gcode(self) -> None:
        kind, id_ = self.project_panel.current()
        if kind == "job":
            j = self.project.job(id_)
            if j is not None and j.job.gcode:
                self.save_gcode_text(j.job.gcode, j.name)
                return
            if j is not None:
                self.generate_gcode(j.id)
                return
        if self.gcode_panel.text().strip():
            self.save_gcode_text()
        elif self.project.jobs:
            self.generate_all_gcode()

    def send_job(self, job_id: int) -> None:
        self.set_workspace("machine")
        j = self.project.job(job_id)
        if j is None or not j.job.gcode:
            return
        self.gcode_source = ("job", j.id)
        self._set_gcode(j.job.gcode, j.name, j.estimate_s)
        self.send_current_gcode()

    def send_current_gcode(self) -> None:
        self.set_workspace("machine")
        text = self.gcode_panel.text()
        if not text.strip():
            QMessageBox.information(self, "Enviar", "Não há G-code carregado. Gere ou abra um programa primeiro.")
            return
        name = getattr(self, "gcode_name", "programa")
        self.machine.load_program(text, name, self.gcode_panel.estimate_s)
        pv = backend.parse_gcode(text, float(self.settings.ui("rapid_rate")))
        self.canvas.set_gcode_preview(pv)
        kind, id_ = self.gcode_source
        self._preview_src = ("gcode", id_) if kind == "job" else ("program", 0)
        self._sync_gcode_tree()
        self._stream_preview = True
        self._show_dock(self.dock_program)
        log.info("Programa “%s” carregado na aba Máquina", name)

    # ------------------------------------------------------------------ simulação
    def simulate_selected(self) -> None:
        kind, id_ = self.project_panel.current()
        if kind == "job":
            self.simulate_job(id_)

    def simulate_job(self, job_id: int) -> None:
        j = self.project.job(job_id)
        if j is None:
            return
        self.stack.setCurrentWidget(self.canvas)
        if not self.canvas.sim_start([j.job], float(self.settings.ui("rapid_rate"))):
            self.statusBar().showMessage("Nada para simular neste caminho.", 3000)
        else:
            self.canvas.setFocus()

    # =================================================================== reações do projeto
    def _on_layer_added(self, e: LayerEntry) -> None:
        it = self.canvas.add_layer(e.id, e.effective, e.color, ROLE_Z.get(e.role, 2.0))
        it.setVisible(e.visible)
        self.project_panel.add_layer(e)
        self._update_actions()

    def _on_layer_removed(self, layer_id: int) -> None:
        self.canvas.remove_item(layer_id)
        self.project_panel.remove("layer", layer_id)
        if isinstance(self.props.entry, LayerEntry) and self.props.entry.id == layer_id:
            self.props.show_empty()
        self._update_actions()

    def _on_layer_changed(self, e: LayerEntry) -> None:
        self.canvas.set_item_visible(e.id, e.visible)
        self.canvas.set_item_color(e.id, e.color)
        it = self.canvas.items_by_key.get(e.id)
        if it is not None:
            it.setZValue(ROLE_Z.get(e.role, 2.0))
        self.project_panel.update_layer(e)
        if self.props.entry is e and not self.props.role.hasFocus():
            QTimer.singleShot(0, lambda: self.props.show_layer(e) if self.props.entry is e else None)

    def _on_layer_geometry(self, e: LayerEntry) -> None:
        self.canvas.update_layer(e.id, e.effective)
        self.project_panel.update_layer(e)
        if self.props.entry is e:
            self.props.show_layer(e)
        stale = self.project.jobs_for(e.id)
        if stale:
            self.statusBar().showMessage(
                f"A camada mudou: {len(stale)} caminho(s) gerado(s) antes da mudança podem estar desatualizados.", 6000)

    def _on_job_added(self, j: JobEntry) -> None:
        it = self.canvas.add_job(j.id, j.job)
        it.setVisible(j.visible)
        src = self.project.layer(j.source_id)
        self.project_panel.add_job(j, src.name if src else "")
        self._sync_gcode_tree()
        self._update_actions()

    def _on_job_removed(self, job_id: int) -> None:
        if self.canvas.edit_key == job_id:
            self.canvas.edit_stop()
        self.canvas.remove_item(job_id)
        if self._preview_src == ("gcode", job_id):
            self._hide_preview()
        if self.gcode_source == ("job", job_id) and not self.machine.streaming:
            self.gcode_source = ("", -1)
            self._set_gcode("", "", 0.0)
        self._sync_gcode_tree()
        self.project_panel.remove("job", job_id)
        if isinstance(self.props.entry, JobEntry) and self.props.entry.id == job_id:
            self.props.show_empty()
        self._update_actions()

    def _on_job_changed(self, j: JobEntry) -> None:
        self.canvas.set_item_visible(j.id, j.visible)
        self.project_panel.update_job(j)
        if not j.job.gcode:
            if self._preview_src == ("gcode", j.id):
                self._hide_preview()
            if self.gcode_source == ("job", j.id) and not self.machine.streaming:
                self.gcode_source = ("", -1)
                self._set_gcode("", "", 0.0)
        self._sync_gcode_tree()
        self._update_actions()

    def _on_cleared(self) -> None:
        self.canvas.edit_stop()
        self.canvas.clear_items()
        self._preview_src = None
        if not self.machine.streaming:
            self.gcode_source = ("", -1)
            self._set_gcode("", "", 0.0)
        self.project_panel.clear()
        self.props.show_empty()
        self._update_actions()

    # =================================================================== reações da interface
    def _on_tree_selected(self, kind: str, id_: int) -> None:
        if kind == "layer":
            e = self.project.layer(id_)
            if e is not None:
                self.props.show_layer(e)
        elif kind in ("gcode", "program"):
            self._show_gcode_entry(kind, id_)
        elif kind == "job":
            j = self.project.job(id_)
            if j is not None:
                self.props.show_job(j)
                if j.job.gcode and self.gcode_source != ("job", j.id) and not self.machine.streaming:
                    self.gcode_source = ("job", j.id)
                    self._set_gcode(j.job.gcode, j.name, j.estimate_s)
        else:
            self.props.show_empty()
        self.canvas.set_selected(id_ if kind else None)
        self._update_actions()

    def _on_canvas_click(self, key: int) -> None:
        if key == PROGRAM_KEY and self._preview_src is not None:
            self.project_panel.select(*self._preview_src)
            return
        if key < 0:
            self.project_panel.select("", -1)
            return
        kind = "layer" if self.project.layer(key) is not None else "job" if self.project.job(key) else ""
        if kind == "layer":
            from PySide6.QtWidgets import QApplication
            members = self.project.group_members(key)
            alt = bool(QApplication.keyboardModifiers() & Qt.KeyboardModifier.AltModifier)
            if len(members) > 1 and not alt:
                # como nos editores gráficos: clicar seleciona o grupo; Alt+clique, só a camada
                self.project_panel.select_many([("layer", i) for i in members], current=("layer", key))
                return
        if kind:
            self.project_panel.select(kind, key)

    def _on_visibility(self, kind: str, id_: int, on: bool) -> None:
        if kind in ("gcode", "program"):
            self._toggle_gcode_preview(kind, id_, on)
            return
        if kind == "layer":
            self.project.set_layer_visible(id_, on)
        else:
            self.project.set_job_visible(id_, on)

    def _on_tree_action(self, action: str, kind: str, id_: int) -> None:
        if action == "remove":
            self.remove_item(kind, id_)
        elif action == "zoom":
            it = self.canvas.items_by_key.get(id_)
            if it is not None:
                self.canvas.fit_rect(it.sceneBoundingRect())
        elif action.startswith("color:"):
            self.project.set_layer_color(id_, action.split(":", 1)[1])
        elif action.startswith("role:"):
            self.undo.push(RoleCmd(self.project, id_, action.split(":", 1)[1]))
        elif action.startswith("mirror:"):
            self._layer_action(action, id_)
        elif action == "gcode":
            self.generate_gcode(id_)
        elif action == "simulate":
            self.simulate_job(id_)
        elif action == "edit":
            self.start_edit_paths(id_)
        elif action == "flip":
            self.open_double_sided([id_])
        elif action == "select_group":
            members = self.project.group_members(id_)
            self.project_panel.select_many([("layer", i) for i in members], current=("layer", id_))
        elif action == "ungroup":
            self.project_panel.select("layer", id_)
            self.ungroup_selected()
        elif action in ("gcode_save", "gcode_send"):
            self._on_gcode_action(action.split("_", 1)[1], kind, id_)

    def _layer_action(self, action: str, layer_id: int | None = None) -> None:
        if layer_id is None:
            kind, layer_id = self.project_panel.current()
            if kind != "layer":
                return
        e = self.project.layer(layer_id)
        if e is None:
            return
        if self._blocked_by_group(e, "espelhar"):
            return
        axis = action.split(":", 1)[1]
        import dataclasses
        t = dataclasses.replace(e.transform, mirror=None if axis == "none" else axis,
                                center=self.project.board_center())
        self.undo.push(TransformCmd(self.project, layer_id, t))

    def remove_selected(self) -> None:
        kind, id_ = self.project_panel.current()
        if kind:
            self.remove_item(kind, id_)

    def remove_item(self, kind: str, id_: int) -> None:
        if kind in ("gcode", "program"):
            self._remove_gcode(kind, id_)
            return
        if kind == "layer":
            self.undo.push(RemoveLayerCmd(self.project, id_))
        elif kind == "job":
            self.undo.push(RemoveJobCmd(self.project, id_))
        self.statusBar().showMessage("Removido. Use Ctrl+Z para desfazer.", 4000)

    # =================================================================== faixa de comandos
    def showEvent(self, ev: Any) -> None:  # noqa: N802
        super().showEvent(ev)
        if not getattr(self, "_first_show_done", False):
            self._first_show_done = True
            if not self._layout_restored:
                # rodapé baixo: o canvas é o protagonista
                QTimer.singleShot(0, lambda: self.resizeDocks([self.dock_log], [190], Qt.Orientation.Vertical))

    def resizeEvent(self, ev: Any) -> None:  # noqa: N802
        super().resizeEvent(ev)
        # a própria faixa decide se compacta, pela largura que os rótulos realmente ocupam

    def choose_operation(self, key: str) -> None:
        """Escolhe a camada adequada e abre o formulário da operação no inspetor."""
        op = backend.operations().get(key)
        if op is None:
            return
        kind, id_ = self.project_panel.current()
        target = self.project.layer(id_) if kind == "layer" else None
        if target is None and kind == "job":
            j = self.project.job(id_)
            target = self.project.layer(j.source_id) if j else None
        pref = {"isolation": ("top_copper", "bottom_copper"), "ncc": ("top_copper", "bottom_copper"),
                "cutout": ("outline",), "drilling": ("drill",), "panelize": ()}.get(key, ())
        cands = [e for e in self.project.layers if e.kind in op.accepts]
        keep = target is not None and target.kind in op.accepts and (not pref or target.role in pref)
        if not keep:
            # camada de papel adequado (ex.: recorte → contorno); senão, a atual se compatível
            best = next((e for r in pref for e in cands if e.role == r), None)
            if best is not None:
                target = best
            elif target is None or target.kind not in op.accepts:
                target = cands[0] if cands else None
        if target is None:
            self.statusBar().showMessage(f"Nenhuma camada compatível com {op.title}. Abra os arquivos da placa.", 5000)
            return
        if self.workspace.current() != "prepare":
            self.set_workspace("prepare")
        self.props.op_key = key
        self.project_panel.select("layer", target.id)
        self.props.select_operation(key)
        self._show_dock(self.dock_props)

    def generate_selected_gcode(self) -> None:
        kind, id_ = self.project_panel.current()
        if kind == "job":
            self.generate_gcode(id_)
        else:
            self.generate_all_gcode()

    def set_workspace(self, key: str) -> None:
        """Preparar (camadas + inspetor) ou Máquina (controle da CNC em destaque)."""
        cur = getattr(self, "_workspace", "prepare")
        if key == cur:
            self.workspace.set_current(key)
            return
        if not hasattr(self, "_ws_states"):
            self._ws_states: dict[str, QByteArray] = {}
        self._ws_states[cur] = self.saveState(LAYOUT_VERSION)
        self._workspace = key
        self.workspace.set_current(key)
        if key == "machine":
            self.canvas.edit_stop()
        saved = self._ws_states.get(key)
        if saved is None or not self.restoreState(saved, LAYOUT_VERSION):
            if key == "machine":
                self._default_machine_layout()
            else:
                self._default_prepare_layout()
        self._apply_workspace_docks(key)
        if key == "machine":
            self.dock_project.hide()
            self.dock_props.hide()
        self._sync_dock_bars()
        QTimer.singleShot(0, self.canvas.fit_all)

    def _apply_workspace_docks(self, key: str) -> None:
        """Os painéis da máquina (estado, jog, programa, console, $$, comandos e config.)
        aparecem só no espaço Máquina (também quando destacados como janelas)."""
        for d in self.machine_docks:
            d.setVisible(key == "machine")
            d.toggleViewAction().setEnabled(key == "machine")
        if key != "machine":
            self.dock_log.show()
            self.dock_gcode.show()

    # =================================================================== girar / agrupar
    def _board_bounds_after(self, A: Any, t: Any) -> tuple[float, float, float, float] | None:
        """Limites de todas as camadas após ``p' = A·p + t`` (calculados sobre a geometria real)."""
        import numpy as np
        import shapely
        pts: list[Any] = []
        for e in self.project.layers:
            lay = e.effective
            if isinstance(lay, DrillLayer):
                for tool in lay.tools.values():
                    pts.append(np.asarray(tool.points, dtype=float).reshape(-1, 2))
                    pts.append(np.asarray(tool.slots, dtype=float).reshape(-1, 2))
            else:
                g = lay.copper if isinstance(lay, GerberLayer) else getattr(lay, "geometry", None)
                if g is not None and not g.is_empty:
                    pts.append(shapely.get_coordinates(g))
        pts = [p for p in pts if len(p)]
        if not pts:
            return None
        a = np.vstack(pts) @ np.asarray(A).T + np.asarray(t)
        return float(a[:, 0].min()), float(a[:, 1].min()), float(a[:, 0].max()), float(a[:, 1].max())

    def rotate_board(self, deg: float) -> None:
        """Gira a placa inteira (camadas + caminhos) e mantém o canto inferior esquerdo no lugar."""
        from .project import rotation_about
        if not self.project.layers or not deg:
            return
        if self.machine.streaming:
            QMessageBox.warning(self, "Girar placa", "Não é possível girar a placa durante o envio de um programa.")
            return
        b0 = self._board_bounds_after([[1, 0], [0, 1]], [0, 0])
        if b0 is None:
            return
        A, t = rotation_about(deg, (b0[0] + b0[2]) / 2, (b0[1] + b0[3]) / 2)
        b1 = self._board_bounds_after(A, t)
        t = t + [b0[0] - b1[0], b0[1] - b1[1]]          # mesmo canto inferior esquerdo de antes
        self.canvas.edit_stop()
        had_gcode = any(j.job.gcode for j in self.project.jobs)
        self.undo.push(AffineCmd(self.project, [e.id for e in self.project.layers], A, t,
                                 f"Girar placa {deg:g}°"))
        QTimer.singleShot(0, self.canvas.fit_all)
        msg = f"Placa girada {deg:g}° (canto inferior esquerdo mantido)"
        if had_gcode:
            msg += " — gere o G-code novamente"
        self.statusBar().showMessage(msg + ". Ctrl+Z desfaz.", 6000)
        log.info(msg)

    def rotate_board_free(self) -> None:
        from PySide6.QtWidgets import QInputDialog
        deg, ok = QInputDialog.getDouble(
            self, "Girar placa", "Ângulo em graus (positivo = anti-horário, negativo = horário):",
            float(self.settings.get("last_rotation", 90.0)), -360.0, 360.0, 3)
        if ok and deg:
            self.settings.set("last_rotation", deg)
            self.rotate_board(deg)

    def group_selected(self) -> None:
        ids = [i for k, i in self.project_panel.selected_items() if k == "layer"]
        if len(ids) < 2:
            self.statusBar().showMessage("Selecione duas ou mais camadas (Ctrl+clique) para agrupar.", 4000)
            return
        name = self.project.suggest_group_name()
        self.undo.push(GroupCmd(self.project, ids, self.project.new_group_id(), name, f"Agrupar como {name}"))
        self.statusBar().showMessage(f"{len(ids)} camadas agrupadas como “{name}”: agora se movem juntas.", 5000)
        self._on_multi_selected(self.project_panel.selected_items())

    def ungroup_selected(self) -> None:
        ids = [i for k, i in self.project_panel.selected_items() if k == "layer"]
        kind, cur = self.project_panel.current()
        if not ids and kind == "layer":
            ids = [cur]
        gids = {e.group for i in ids if (e := self.project.layer(i)) is not None and e.group is not None}
        members = [e.id for e in self.project.layers if e.group in gids]
        if not members:
            return
        self.undo.push(GroupCmd(self.project, members, None))
        self.statusBar().showMessage("Grupo desfeito: as camadas podem ser movidas separadamente. "
                                     "Cuidado para não desalinhar furos e cobre.", 6000)
        sel = self.project_panel.selected_items()
        if len(sel) > 1:
            self._on_multi_selected(sel)

    def _blocked_by_group(self, e: LayerEntry, what: str) -> bool:
        """Impede espelhar sozinha uma camada que pertence a um grupo (desalinharia os furos)."""
        if e.group is None or len(self.project.group_members(e.id)) < 2:
            return False
        QMessageBox.information(
            self, "Camada agrupada",
            f"“{e.name}” faz parte do grupo “{self.project.group_name(e.group)}”. Para não desalinhar furos e "
            f"cobre, não é possível {what} só esta camada.\n\n"
            "• Para fresar o lado de baixo, use Dupla face (Ctrl+D): ela vira o lado de baixo inteiro junto.\n"
            "• Se quiser mesmo mexer só nesta camada, desagrupe antes (Ctrl+Shift+G).")
        return True

    def _on_layer_transform(self, layer_id: int, t: Transform) -> None:
        """Deslocar/espelhar pela seção Posição: em grupo, o grupo inteiro se move junto."""
        import numpy as np
        e = self.project.layer(layer_id)
        if e is None:
            return
        members = self.project.group_members(layer_id)
        if len(members) < 2:
            self.undo.push(TransformCmd(self.project, layer_id, t))
            return
        A_old, t_old = e.transform.matrix()
        A_new, t_new = t.matrix()
        if np.linalg.det(A_old) * np.linalg.det(A_new) < 0:
            self._blocked_by_group(e, "espelhar")
            self.props.refresh()
            return
        D = A_new @ np.linalg.inv(A_old)
        d = t_new - D @ t_old
        self.undo.push(AffineCmd(self.project, members, D, d,
                                 f"Mover grupo {self.project.group_name(e.group)}"))
        self.statusBar().showMessage(
            f"Movido o grupo “{self.project.group_name(e.group)}” inteiro ({len(members)} camadas), "
            "para manter o alinhamento.", 5000)

    def _on_program_autolevel(self, text: str, name: str) -> None:
        """O heightmap corrigiu (ou restaurou) o programa carregado na máquina."""
        rapid = float(self.settings.ui("rapid_rate"))
        pv = backend.parse_gcode(text, rapid)
        self.machine.load_program(text, name, pv.estimate_time_s)
        self.gcode_source = ("program", -1)
        self._set_gcode(text, name, pv.estimate_time_s)
        self.canvas.set_gcode_preview(pv)
        self._preview_src = ("program", 0)
        self._stream_preview = True
        self._sync_gcode_tree()
        log.info("Programa atualizado pelo heightmap: %s", name)

    # =================================================================== categoria G-code
    def _sync_gcode_tree(self) -> None:
        """Mantém a categoria G-code da árvore igual aos programas existentes."""
        pp = self.project_panel
        want: set[tuple[str, int]] = set()
        for j in self.project.jobs:
            if j.job.gcode:
                want.add(("gcode", j.id))
                n = len(j.job.gcode.splitlines())
                sub = f"{n:,} linhas".replace(",", ".") + (f" · ≈ {units.fmt_time(j.estimate_s)}" if j.estimate_s else "")
                pp.upsert_gcode("gcode", j.id, f"G-code — {j.name}", sub, self._preview_src == ("gcode", j.id))
        text = self.gcode_panel.text() if hasattr(self, "gcode_panel") else ""
        if self.gcode_source[0] in ("program", "file") and text.strip():
            want.add(("program", 0))
            n = len(text.splitlines())
            origin = "arquivo aberto" if self.gcode_source[0] == "file" else "programa completo"
            pp.upsert_gcode("program", 0, getattr(self, "gcode_name", "Programa") or "Programa",
                            f"{origin} · {n:,} linhas".replace(",", "."), self._preview_src == ("program", 0))
        for key in [k for k in pp.items if k[0] in ("gcode", "program") and k not in want]:
            pp.remove(*key)

    def _gcode_text(self, kind: str, id_: int) -> tuple[str, str, float, str]:
        """(texto, nome, tempo estimado, origem) de uma entrada da categoria G-code."""
        if kind == "gcode":
            j = self.project.job(id_)
            if j is None:
                return "", "", 0.0, ""
            src = self.project.layer(j.source_id)
            return j.job.gcode, f"G-code — {j.name}", j.estimate_s, f"gerado de {j.name}" + (
                f" ({src.name})" if src else "")
        origin = "arquivo aberto" if self.gcode_source[0] == "file" else "programa completo (todos os caminhos)"
        return self.gcode_panel.text(), getattr(self, "gcode_name", "Programa"), self.gcode_panel.estimate_s, origin

    def _load_entry_in_editor(self, kind: str, id_: int) -> None:
        if self.machine.streaming:
            return
        if kind == "gcode":
            j = self.project.job(id_)
            if j is not None and self.gcode_source != ("job", id_):
                self.gcode_source = ("job", id_)
                self.gcode_name = j.name
                self.gcode_panel.set_text(j.job.gcode, j.estimate_s)

    def _show_gcode_entry(self, kind: str, id_: int) -> None:
        self._load_entry_in_editor(kind, id_)
        text, name, est, origin = self._gcode_text(kind, id_)
        if not text:
            self.props.show_empty()
            return
        self.props.show_gcode(kind, id_, name, text, est, origin, self._preview_src == (kind, id_))
        self.canvas.set_selected(None)

    def _toggle_gcode_preview(self, kind: str, id_: int, on: bool) -> None:
        if not on:
            if self._preview_src == (kind, id_):
                self._hide_preview()
            return
        self._load_entry_in_editor(kind, id_)
        text, _n, _e, _o = self._gcode_text(kind, id_)
        if not text.strip():
            return
        pv = backend.parse_gcode(text, float(self.settings.ui("rapid_rate")))
        self.canvas.set_gcode_preview(pv)
        self._preview_src = (kind, id_)
        self._sync_gcode_tree()
        self.stack.setCurrentWidget(self.canvas)

    def _hide_preview(self) -> None:
        if self.machine.streaming and self._stream_preview:
            return
        self.canvas.set_gcode_preview(None)
        self._preview_src = None
        self._stream_preview = False
        for key in [k for k in self.project_panel.items if k[0] in ("gcode", "program")]:
            self.project_panel.set_gcode_visible(*key, False)
        self._update_actions()

    def _remove_gcode(self, kind: str, id_: int) -> None:
        if self.machine.streaming and (self.gcode_source == ("job", id_) if kind == "gcode"
                                       else self.gcode_source[0] in ("program", "file")):
            QMessageBox.information(self, "Remover G-code", "Este programa está sendo enviado à máquina.")
            return
        if self._preview_src == (kind, id_):
            self._hide_preview()
        if kind == "gcode":
            if self.project.job(id_) is not None:
                self.undo.push(ClearJobGcodeCmd(self.project, id_))
        else:
            self.gcode_source = ("", -1)
            self._set_gcode("", "", 0.0)
        self._sync_gcode_tree()
        if self.project_panel.current() == (kind, id_) or self.project_panel.current() == ("", -1):
            self.props.show_empty()
        self.statusBar().showMessage("G-code removido. O caminho de ferramenta continua em Geometria.", 4000)

    def _on_gcode_action(self, action: str, kind: str, id_: int) -> None:
        text, name, est, _o = self._gcode_text(kind, id_)
        if action == "preview":
            self._toggle_gcode_preview(kind, id_, self._preview_src != (kind, id_))
            self._show_gcode_entry(kind, id_)
        elif action == "save" and text:
            self.save_gcode_text(text, name)
        elif action == "send" and text:
            self._load_entry_in_editor(kind, id_)
            self.send_current_gcode()
        elif action == "remove":
            self._remove_gcode(kind, id_)

    # =================================================================== dupla face / seleção múltipla
    def _set_tool_width(self, on: bool) -> None:
        self.canvas.set_show_tool_width(on)
        self.settings.set("show_tool_width", on)

    def open_double_sided(self, preselect: list[int] | None = None) -> None:
        if not self.project.layers:
            return
        if self.machine.streaming:
            QMessageBox.warning(self, "Dupla face", "Não é possível virar a placa durante o envio de um programa.")
            return
        dlg = DoubleSidedDialog(self.project, self, preselect)
        dlg.setAttribute(Qt.WidgetAttribute.WA_DeleteOnClose)
        if not dlg.exec():
            return
        self.canvas.edit_stop()
        self.undo.beginMacro("Dupla face: virar placa")
        if dlg.layer_ids:
            self.undo.push(FlipLayersCmd(self.project, dlg.layer_ids, dlg.axis, dlg.center))
        if dlg.pins is not None:
            self.undo.push(AddLayersCmd(self.project, [LayerEntry(dlg.pins, "drill")]))
        self.undo.endMacro()
        n_jobs = sum(1 for j in self.project.jobs if j.source_id in set(dlg.layer_ids))
        msg = f"{len(dlg.layer_ids)} camada(s) virada(s) em torno de {'X' if dlg.axis == 'x' else 'Y'} = " \
              f"{units.fmt_len(dlg.center, 2)}"
        if n_jobs:
            msg += f"; {n_jobs} caminho(s) também — gere o G-code novamente"
        if dlg.pins is not None:
            msg += "; furos de alinhamento criados"
        self.statusBar().showMessage(msg + ". Ctrl+Z desfaz.", 8000)
        log.info(msg)

    def _on_multi_selected(self, items: list) -> None:
        layers = [e for k, i in items if k == "layer" and (e := self.project.layer(i)) is not None]
        jobs = [j for k, i in items if k == "job" and (j := self.project.job(i)) is not None]
        gids = {e.group for e in layers}
        group = None
        if layers and len(gids) == 1 and None not in gids:
            gid = next(iter(gids))
            if {e.id for e in layers} == {e.id for e in self.project.layers if e.group == gid}:
                group = (gid, self.project.group_name(gid))
        self.props.show_multi(layers, jobs, group)
        self.canvas.set_selected({i for _k, i in items})
        self._update_actions()

    def _on_batch(self, action: str, items: list) -> None:
        if action == "rename_group":
            pass
        else:
            items = [(k, i) for k, i in items if k in ("gcode", "program")
                     or (self.project.layer(i) if k == "layer" else self.project.job(i))]
        if not items:
            return
        if action in ("show", "hide"):
            for k, i in items:
                (self.project.set_layer_visible if k == "layer" else self.project.set_job_visible)(i, action == "show")
        elif action == "solo":
            keep = set(items)
            for e in self.project.layers:
                self.project.set_layer_visible(e.id, ("layer", e.id) in keep)
            for j in self.project.jobs:
                self.project.set_job_visible(j.id, ("job", j.id) in keep)
        elif action == "zoom":
            from PySide6.QtCore import QRectF
            r = QRectF()
            for _k, i in items:
                it = self.canvas.items_by_key.get(i)
                if it is not None:
                    r = r | it.sceneBoundingRect()
            if not r.isEmpty():
                self.canvas.fit_rect(r)
        elif action == "flip":
            self.open_double_sided([i for k, i in items if k == "layer"])
        elif action == "save_gcodes":
            self.save_all_gcode([i for k, i in items if k == "job"])
        elif action == "group":
            self.group_selected()
        elif action == "ungroup":
            self.ungroup_selected()
        elif action == "rename_group":
            from PySide6.QtWidgets import QInputDialog
            gid = items[0][1]
            name, ok = QInputDialog.getText(self, "Renomear grupo", "Nome do grupo:",
                                            text=self.project.group_name(gid))
            if ok and name.strip():
                self.project.rename_group(gid, name.strip())
                self._on_multi_selected(self.project_panel.selected_items())
        elif action == "remove":
            if QMessageBox.question(self, "Remover", f"Remover {len(items)} itens? (Ctrl+Z desfaz)") \
                    != QMessageBox.StandardButton.Yes:
                return
            self.undo.beginMacro(f"Remover {len(items)} itens")
            for k, i in [x for x in items if x[0] in ("gcode", "program")]:
                self._remove_gcode(k, i)
            for k, i in [x for x in items if x[0] == "job"] + [x for x in items if x[0] == "layer"]:
                if k == "job" and self.project.job(i) is not None:
                    self.undo.push(RemoveJobCmd(self.project, i))
                elif k == "layer" and self.project.layer(i) is not None:
                    self.undo.push(RemoveLayerCmd(self.project, i))
            self.undo.endMacro()

    # =================================================================== biblioteca de ferramentas
    def open_library(self, select_uid: str | None = None) -> None:
        dlg = ToolLibraryDialog(self, select_uid)
        dlg.setAttribute(Qt.WidgetAttribute.WA_DeleteOnClose)
        dlg.exec()

    # =================================================================== posicionar placa
    def open_align(self) -> None:
        if self.project.is_empty():
            return
        dlg = AlignDialog(self.project, self)
        dlg.setAttribute(Qt.WidgetAttribute.WA_DeleteOnClose)
        if dlg.exec():
            self._move_board(dlg.rotation, dlg.dx, dlg.dy)

    def align_quick(self) -> None:
        from .project import alignment_delta
        b = self.project.board_bounds("outline")
        if b is None:
            return
        dx, dy, _nb = alignment_delta(b, 0.0, "bl", (0.0, 0.0))
        if abs(dx) < 1e-9 and abs(dy) < 1e-9:
            self.statusBar().showMessage("A placa já está com o canto inferior esquerdo em X0 Y0.", 4000)
            return
        self._move_board(0.0, dx, dy)

    def _move_board(self, rotation: float, dx: float, dy: float) -> None:
        if self.machine.streaming:
            QMessageBox.warning(self, "Posicionar placa", "Não é possível mover a placa durante o envio de um programa.")
            return
        self.canvas.edit_stop()
        had_gcode = any(j.job.gcode for j in self.project.jobs)
        self.undo.push(MoveAllCmd(self.project, rotation, dx, dy))
        QTimer.singleShot(0, self.canvas.fit_all)
        msg = f"Placa movida (ΔX {units.fmt_len(dx, 2, False)}, ΔY {units.fmt_len(dy, 2)}"
        msg += f", giro {rotation:g}°)" if rotation else ")"
        if had_gcode:
            msg += " — gere o G-code novamente."
        self.statusBar().showMessage(msg + " Ctrl+Z desfaz.", 8000)
        log.info(msg)

    # =================================================================== edição de caminhos
    def edit_selected_paths(self) -> None:
        if self.canvas.edit_key is not None:
            self.canvas.edit_stop()
            return
        kind, id_ = self.project_panel.current()
        if kind == "job":
            self.start_edit_paths(id_)

    def start_edit_paths(self, job_id: int) -> None:
        j = self.project.job(job_id)
        if j is None:
            return
        if self.machine.streaming:
            QMessageBox.information(self, "Editar caminhos", "Aguarde o fim do envio do programa para editar.")
            return
        if not j.visible:
            self.project.set_job_visible(job_id, True)
        self.stack.setCurrentWidget(self.canvas)
        self.canvas.edit_start(job_id, j.name, j.job.ops)
        it = self.canvas.items_by_key.get(job_id)
        if it is not None and not self.canvas.viewport().rect().isEmpty():
            self.canvas.fit_rect(it.sceneBoundingRect())

    def _on_edit_action(self, job_id: int, action: str, payload: Any) -> None:
        from brokcam.cam import edit as cam_edit
        j = self.project.job(job_id)
        if j is None:
            return
        ops = j.job.ops
        if action == "delete":
            new, text = cam_edit.delete(ops, payload), f"Excluir {len(payload)} item(ns) do caminho"
        elif action == "keep":
            new, text = cam_edit.keep_only(ops, payload), "Manter só a seleção"
        elif action == "reverse":
            new, text = cam_edit.reverse(ops, payload), "Inverter sentido de caminhos"
        elif action == "trim":
            new, text = cam_edit.trim_rect(ops, *payload), "Apagar trecho de caminhos"
        elif action == "reoptimize":
            new, text = cam_edit.reoptimize(ops), "Reotimizar ordem dos cortes"
        else:
            return
        before, after = cam_edit.count(ops), cam_edit.count(new)
        if action == "trim" and before == after and \
                sum(len(p) for op in ops for p in op.paths) == sum(len(p) for op in new for p in op.paths):
            self.statusBar().showMessage("Nada para apagar dentro da área.", 3000)
            return
        if not new:
            if QMessageBox.question(self, "Editar caminhos",
                                    "Esta edição remove todos os cortes deste caminho. Continuar?") \
                    != QMessageBox.StandardButton.Yes:
                return
        self.undo.push(EditJobOpsCmd(self.project, job_id, new, text))
        self.statusBar().showMessage(
            f"{text}: {after['paths']} caminhos e {after['points']} furos restantes. Ctrl+Z desfaz.", 5000)

    def _on_edit_mode(self, on: bool) -> None:
        if on:
            self.statusBar().showMessage(
                "Edição: clique/arraste para selecionar · Delete remove · T apaga trecho por área · "
                "R inverte sentido · Esc/Enter conclui", 0)
        else:
            self.statusBar().clearMessage()
        self._update_actions()

    def _on_job_geometry(self, j: JobEntry) -> None:
        it = self.canvas.items_by_key.get(j.id)
        if it is not None and hasattr(it, "set_job"):
            it.set_job(j.job)
        j.estimate_s = backend.estimate_job_time_s(j.job, float(self.settings.ui("rapid_rate")))
        if self.canvas.edit_key == j.id:
            self.canvas.edit_set_ops(j.job.ops)
        self.project_panel.update_job(j)
        if self.gcode_source == ("job", j.id) and not j.job.gcode and not self.machine.streaming:
            self._set_gcode("", j.name, 0.0)
        if self._preview_src == ("gcode", j.id) and not j.job.gcode:
            self._hide_preview()
        self._sync_gcode_tree()
        if isinstance(self.props.entry, JobEntry) and self.props.entry.id == j.id:
            self.props.show_job(j)
        self._update_actions()

    def _on_cursor(self, x: float, y: float) -> None:
        self.coord_label.setText(f"X {units.fmt_len(x, with_unit=False):>10}  Y {units.fmt_len(y, with_unit=False):>10}")

    def _on_measure(self, m: dict | None) -> None:
        if not m or "dist" not in m:
            self.measure_label.setText("Clique no primeiro ponto…" if self.canvas.mode == "measure" and not m else
                                       "Clique no segundo ponto…" if m else "")
            return
        self.measure_label.setText(
            f"Distância {units.fmt_len(m['dist'])}  ·  ΔX {units.fmt_len(m['dx'], with_unit=False)}  "
            f"ΔY {units.fmt_len(m['dy'], with_unit=False)}  ·  {m['angle']:.1f}°".replace(".", ",", 1))
        if self.canvas.mode != "measure":
            self.a_measure.setChecked(False)

    def _on_tool_position(self, pos: Any) -> None:
        if pos is None:
            self.canvas.set_tool_position(None)
        else:
            self.canvas.set_tool_position(pos[0], pos[1], live=True)

    def _on_program_line(self, line: int) -> None:
        self._pending_line = line
        now = time.monotonic()
        if now - self._last_line_update < 0.1:
            QTimer.singleShot(110, self._flush_program_line)
            return
        self._flush_program_line()

    def _flush_program_line(self) -> None:
        line = self._pending_line
        if line < 0:
            return
        self._last_line_update = time.monotonic()
        self.gcode_panel.editor.set_exec_line(line, follow=self.dock_gcode.isVisible())
        if self.canvas.gcode_item is not None:
            self.canvas.gcode_item.set_done_line(line)

    def _on_streaming(self, on: bool) -> None:
        self.gcode_panel.editor.setReadOnly(on)
        if not on:
            self.statusBar().showMessage("Envio do programa encerrado.", 5000)


