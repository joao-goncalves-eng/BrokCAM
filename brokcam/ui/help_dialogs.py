"""Ajuda: manual de opções (todos os ParamGroups), guia rápido, atalhos e Sobre."""
from __future__ import annotations

import html

from PySide6.QtCore import Qt, QUrl
from PySide6.QtGui import QDesktopServices, QTextDocument
from PySide6.QtWidgets import (
    QDialog, QDialogButtonBox, QHBoxLayout, QHeaderView, QLabel, QLineEdit, QSplitter,
    QTableWidget, QTableWidgetItem, QTextBrowser, QTreeWidget, QTreeWidgetItem, QVBoxLayout, QWidget,
)

from brokcam import __version__
from brokcam.core.params import ParamGroup, ParamSpec, all_groups

from . import backend, theme
from .icons import pixmap
from .param_form import _fmt_value, spec_range_text

KIND_PT = {"float": "número decimal", "int": "número inteiro", "bool": "liga/desliga",
           "choice": "lista de opções", "str": "texto"}


def collect_groups() -> list[ParamGroup]:
    """Todos os grupos conhecidos (força o registro dos módulos de CAM e pós)."""
    backend.operations()
    backend.grbl_post_group()
    groups = list(all_groups())
    from .machine_panel import PROBE_GROUP
    if PROBE_GROUP.key not in {g.key for g in groups}:
        groups.append(PROBE_GROUP)
    return groups


def _css() -> str:
    p = theme.palette()
    return f"""
    body {{ color: {p.text}; background: {p.surface}; }}
    h1 {{ font-size: 18pt; margin: 0 0 4px 0; }}
    h2 {{ font-size: 14pt; margin: 22px 0 4px 0; color: {p.text}; }}
    h3 {{ font-size: 11pt; margin: 14px 0 2px 0; color: {p.accent}; }}
    p {{ margin: 2px 0 6px 0; line-height: 140%; }}
    .muted {{ color: {p.text_muted}; }}
    .meta {{ color: {p.text_muted}; font-size: 9pt; }}
    .adv {{ color: {p.warning}; font-size: 8.5pt; font-weight: 600; }}
    kbd {{ background: {p.surface_alt}; border: 1px solid {p.border_strong}; padding: 1px 5px; }}
    td {{ padding: 4px 10px 4px 0; vertical-align: top; }}
    a {{ color: {p.accent}; text-decoration: none; }}
    """


def _anchor(group: ParamGroup, spec: ParamSpec | None = None) -> str:
    return f"g-{group.key}" + (f"-{spec.key}" if spec else "")


def manual_html(groups: list[ParamGroup]) -> str:
    out = [f"<html><head><style>{_css()}</style></head><body>",
           "<h1>Manual de opções</h1>",
           "<p class='muted'>Todas as opções do BrokCAM, com unidade, valor padrão, faixa permitida "
           "e explicação completa. As mesmas explicações aparecem no painel de ajuda ao focar um campo.</p>",
           "<p>" + " · ".join(f"<a href='#{_anchor(g)}'>{html.escape(g.title)}</a>" for g in groups) + "</p>"]
    for g in groups:
        out.append(f"<a name='{_anchor(g)}'></a><h2>{html.escape(g.title)}</h2>")
        out.append(f"<p class='muted'>{html.escape(g.description)}</p>")
        for s in g.params:
            unit = f" <span class='muted'>({html.escape(s.unit)})</span>" if s.unit else ""
            adv = " <span class='adv'>AVANÇADO</span>" if s.advanced else ""
            out.append(f"<a name='{_anchor(g, s)}'></a><h3>{html.escape(s.label)}{unit}{adv}</h3>")
            meta = [f"Tipo: {KIND_PT.get(s.kind, s.kind)}", f"Padrão: {html.escape(_fmt_value(s, s.default))}"]
            rng = spec_range_text(s)
            if rng:
                meta.append(f"Faixa: {html.escape(rng)}")
            if s.kind == "choice" and s.choices:
                meta.append("Opções: " + ", ".join(html.escape(lab) for _v, lab in s.choices))
            meta.append(f"Chave: <code>{html.escape(g.key)}.{html.escape(s.key)}</code>")
            out.append(f"<p class='meta'>{' · '.join(meta)}</p>")
            for para in (s.help or "").split("\n\n"):
                if para.strip():
                    out.append(f"<p>{html.escape(para.strip())}</p>")
    out.append("</body></html>")
    return "".join(out)


class OptionsManual(QDialog):
    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setWindowTitle("Manual de opções — BrokCAM")
        self.resize(1100, 760)
        self.groups = collect_groups()
        lay = QVBoxLayout(self)
        lay.setContentsMargins(12, 12, 12, 12)
        lay.setSpacing(8)
        self.search = QLineEdit()
        self.search.setPlaceholderText("Pesquisar opções (nome, explicação, unidade)…")
        self.search.setClearButtonEnabled(True)
        self.search.setAccessibleName("Pesquisar no manual")
        self.search.textChanged.connect(self._filter)
        self.search.returnPressed.connect(self._find_in_text)
        lay.addWidget(self.search)
        split = QSplitter()
        self.tree = QTreeWidget()
        self.tree.setHeaderHidden(True)
        self.tree.setAccessibleName("Índice de opções")
        self.tree.itemActivated.connect(self._go)
        self.tree.itemClicked.connect(self._go)
        self.browser = QTextBrowser()
        self.browser.setOpenLinks(False)
        self.browser.anchorClicked.connect(lambda u: self.browser.scrollToAnchor(u.fragment()))
        self.browser.setAccessibleName("Texto do manual")
        split.addWidget(self.tree)
        split.addWidget(self.browser)
        split.setSizes([300, 800])
        lay.addWidget(split, 1)
        self.count = QLabel()
        self.count.setObjectName("Muted")
        bb = QDialogButtonBox(QDialogButtonBox.StandardButton.Close)
        bb.rejected.connect(self.reject)
        row = QHBoxLayout()
        row.addWidget(self.count, 1)
        row.addWidget(bb)
        lay.addLayout(row)
        self._build()
        theme.on_change(self, lambda _p: self.browser.setHtml(manual_html(self.groups)))

    def _build(self) -> None:
        self.browser.setHtml(manual_html(self.groups))
        self.tree.clear()
        n = 0
        for g in self.groups:
            gi = QTreeWidgetItem([g.title])
            gi.setData(0, Qt.ItemDataRole.UserRole, _anchor(g))
            gi.setData(0, Qt.ItemDataRole.UserRole + 1, f"{g.title} {g.description}".lower())
            for s in g.params:
                si = QTreeWidgetItem([s.label])
                si.setData(0, Qt.ItemDataRole.UserRole, _anchor(g, s))
                si.setData(0, Qt.ItemDataRole.UserRole + 1, f"{s.label} {s.help} {s.unit} {s.key}".lower())
                si.setToolTip(0, s.tooltip)
                gi.addChild(si)
                n += 1
            self.tree.addTopLevelItem(gi)
        self.count.setText(f"{len(self.groups)} grupos · {n} opções")

    def _filter(self, text: str) -> None:
        t = text.strip().lower()
        for i in range(self.tree.topLevelItemCount()):
            gi = self.tree.topLevelItem(i)
            any_child = False
            for j in range(gi.childCount()):
                ci = gi.child(j)
                hit = not t or t in str(ci.data(0, Qt.ItemDataRole.UserRole + 1))
                ci.setHidden(not hit)
                any_child |= hit
            gi.setHidden(not (any_child or not t or t in str(gi.data(0, Qt.ItemDataRole.UserRole + 1))))
            gi.setExpanded(bool(t) and any_child)

    def _find_in_text(self) -> None:
        t = self.search.text().strip()
        if t and not self.browser.find(t):
            self.browser.moveCursor(self.browser.textCursor().MoveOperation.Start)
            self.browser.find(t, QTextDocument.FindFlag(0))

    def _go(self, item: QTreeWidgetItem) -> None:
        anchor = item.data(0, Qt.ItemDataRole.UserRole)
        if anchor:
            self.browser.scrollToAnchor(str(anchor))


# --------------------------------------------------------------------------- guia rápido

GUIDE_HTML = """
<h1>Guia rápido: da placa (Gerber) à máquina (GRBL)</h1>
<p class='muted'>Fluxo típico de fresagem por isolamento numa CNC pequena com GRBL.</p>
<h2>1. Importar os arquivos</h2>
<p>Use <b>Arquivo › Abrir</b> (<kbd>Ctrl+O</kbd>) ou arraste os arquivos para a janela. Normalmente:
cobre superior/inferior (<code>.gtl/.gbl</code>), contorno (<code>.gko/.gm1</code>) e furação
(<code>.drl/.xln</code>). O papel de cada arquivo é detectado pelo nome; corrija no painel Propriedades se preciso.</p>
<h2>2. Posicionar</h2>
<p>Arquivos do KiCad/Eagle costumam vir longe do zero. Use <b>Editar › Posicionar placa no zero</b>
(<kbd>Ctrl+Shift+A</kbd>): escolha o ponto de referência (canto inferior esquerdo, centro…), o destino,
uma margem e, se precisar, gire 90°. Cobre, furos, contorno e caminhos já gerados se movem juntos, sem perder
o alinhamento. <kbd>Ctrl+0</kbd> leva o canto inferior esquerdo direto para X0 Y0.
Para fresar o lado de baixo, espelhe a camada inferior (e a furação) horizontalmente.</p>
<h2>3. Gerar caminhos</h2>
<p>Selecione a camada → escolha a operação → escolha a <b>Ferramenta</b> da sua biblioteca (preenche
diâmetro, profundidade, avanços e rotação) ou um <b>Preset</b> salvo → <b>Gerar caminho</b>.
Cadastre suas fresas e brocas em <b>Biblioteca de ferramentas</b> (<kbd>Ctrl+T</kbd>); presets guardam
também a ferramenta escolhida.</p>
<ul>
<li><b>Isolamento</b> no cobre: contorna as trilhas com a fresa (V-bit), separando-as eletricamente.</li>
<li><b>Furação</b> na camada de furos: um ciclo por furo, agrupado por diâmetro de broca. Cada diâmetro tem uma cor (tabela <i>Diâmetros de furo</i> no inspetor); para um G-code por broca use o botão <i>Gerar um G-code por broca</i> logo abaixo da tabela (ou <i>Programas gerados → Um trabalho separado por broca</i>), ou selecione uma linha e use <i>Gerar só este diâmetro</i>. Se a <i>Associação de diâmetros</i> trocar o diâmetro (agrupar / brocas disponíveis), a coluna <i>Broca</i> da tabela mostra qual broca será usada. Para gravar vários G-codes de uma vez, use <i>Arquivo → Salvar G-codes de vários caminhos…</i> (Ctrl+Shift+E): um .nc por caminho, com nome seguro para cartão SD (ex.: <code>Furacao_0-4mm_T1_6_furos.nc</code>).</li>
<li><b>Recorte</b> no contorno: separa a placa do material com pontes (tabs) para ela não se soltar.</li>
<li><b>Limpeza de cobre (NCC)</b>, opcional: remove o cobre que não faz parte do circuito.</li>
</ul>
<h2>4. Conferir e gerar G-code</h2>
<p>Use <b>Simular</b> para ver a ordem dos cortes. Se algum corte for desnecessário, use
<b>Editar caminhos</b> (<kbd>E</kbd>): clique ou arraste para selecionar caminhos e furos e tecle
<kbd>Delete</kbd>, ou use <kbd>T</kbd> para apagar só o trecho dentro de uma área. Tudo pode ser desfeito
(<kbd>Ctrl+Z</kbd>) e a edição fica salva no projeto. Depois selecione o caminho e clique em
<b>Gerar G-code</b>. O resultado aparece na aba <b>G-code</b>, onde pode ser salvo (<code>.nc</code>) ou enviado.</p>
<h2>5. Máquina</h2>
<p>Na aba <b>Máquina</b>: conecte, faça Home (se houver fins de curso), leve a ferramenta ao canto da placa
com o jog, clique <b>Zero XY</b>, desça até tocar a placa (ou use a <b>Sonda Z</b>) e clique <b>Z0</b>.
Em seguida <b>Iniciar</b>. Pausar faz um <i>feed hold</i> seguro; Parar interrompe o programa.</p>
<h2>Dicas</h2>
<ul>
<li>Toda opção tem explicação completa: dê foco ao campo e leia o quadro de ajuda.</li>
<li>Em caso de alarme, leia a explicação exibida, resolva a causa e use <b>Destravar</b>.</li>
<li>O <b>Assistente</b> (<kbd>Ctrl+Shift+W</kbd>) faz os passos 1–4 automaticamente.</li>
</ul>
"""

SHORTCUTS: list[tuple[str, str, str]] = [
    ("Arquivo", "Ctrl+O", "Abrir arquivos Gerber/Excellon/projeto"),
    ("Arquivo", "Ctrl+S", "Salvar projeto"),
    ("Arquivo", "Ctrl+Shift+S", "Salvar projeto como"),
    ("Arquivo", "Ctrl+E", "Exportar G-code"),
    ("Arquivo", "Ctrl+Shift+E", "Salvar G-codes de vários caminhos numa pasta (um .nc por caminho)"),
    ("Arquivo", "Ctrl+Shift+W", "Assistente Gerber → GRBL"),
    ("Editar", "Ctrl+Z / Ctrl+Y", "Desfazer / refazer (remoções e transformações)"),
    ("Editar", "Delete", "Remover item selecionado no Projeto"),
    ("Editar", "Ctrl+,", "Preferências"),
    ("Editar", "Ctrl+T", "Biblioteca de ferramentas"),
    ("Editar", "Ctrl+Shift+A", "Posicionar placa inteira no zero (ancoragem, margem, giro)"),
    ("Editar", "Ctrl+0", "Placa no zero: canto inferior esquerdo em X0 Y0"),
    ("Editar", "Ctrl+Shift+R", "Girar a placa inteira 90° anti-horário (outros ângulos no botão Girar ▾)"),
    ("Editar", "Ctrl+D", "Dupla face: virar o lado de baixo (cobre inferior, furos e contorno) de uma vez"),
    ("Editar", "Ctrl+G / Ctrl+Shift+G", "Agrupar / desagrupar as camadas selecionadas"),
    ("Canvas", "Clique / Alt+clique", "Seleciona o grupo inteiro / só a camada clicada"),
    ("Edição de caminhos", "E", "Entrar/sair do modo de edição do caminho selecionado"),
    ("Edição de caminhos", "Clique / arrastar", "Selecionar (→ só o que está dentro, ← tudo que toca)"),
    ("Edição de caminhos", "Shift/Ctrl+clique", "Somar ou tirar da seleção"),
    ("Edição de caminhos", "Delete", "Excluir selecionados"),
    ("Edição de caminhos", "T", "Ferramenta “apagar trecho”: arraste uma área"),
    ("Edição de caminhos", "R / I / Ctrl+A", "Inverter sentido / inverter seleção / selecionar tudo"),
    ("Edição de caminhos", "Esc / Enter", "Limpar seleção, depois concluir"),
    ("Canvas", "1 ou F", "Ajustar à tela"),
    ("Canvas", "+ / −  ou roda", "Aproximar / afastar (no cursor)"),
    ("Canvas", "Botão do meio / Espaço+arrastar", "Mover a vista"),
    ("Canvas", "Setas", "Mover a vista"),
    ("Canvas", "M", "Régua de medição (clique em dois pontos)"),
    ("Canvas", "Esc", "Cancelar medição/simulação ou limpar seleção"),
    ("Canvas", "G", "Mostrar/ocultar grade"),
    ("Operações", "Ctrl+Enter", "Gerar caminho / G-code do item selecionado"),
    ("Operações", "Ctrl+R", "Simular caminho selecionado"),
    ("Exibir", "Ctrl+1 / 2 / 3 / 4", "Ir para Log / G-code / Máquina / Config. máquina"),
    ("Exibir", "Ctrl+5", "Comandos e sonda: posição, ajuste fino de Z, sonda Z, heightmap e macros"),
    ("Exibir", "Ctrl+6 / Ctrl+7", "Painel Programa (envio) / Console GRBL"),
    ("Exibir", "Arrastar a barra de um painel", "Encaixar em outra borda ou em abas (Exibir › Painéis › Travar painéis impede)"),
    ("Exibir", "Ctrl+Shift+T", "Alternar tema claro/escuro"),
    ("Exibir", "Ctrl+= / Ctrl+-", "Aumentar / reduzir a escala da fonte"),
    ("Máquina", "Setas (pad em foco)", "Jog X/Y"),
    ("Máquina", "PgUp / PgDn (pad em foco)", "Jog Z"),
    ("Máquina", "1–4 (pad em foco)", "Escolher passo do jog"),
    ("Ajuda", "F1", "Guia rápido"),
    ("Ajuda", "Shift+F1", "Manual de opções"),
]


class HtmlDialog(QDialog):
    def __init__(self, title: str, body_html: str, parent: QWidget | None = None,
                 size: tuple[int, int] = (760, 680)) -> None:
        super().__init__(parent)
        self.setWindowTitle(title)
        self.resize(*size)
        lay = QVBoxLayout(self)
        lay.setContentsMargins(12, 12, 12, 12)
        self.browser = QTextBrowser()
        self.browser.setOpenExternalLinks(True)
        self.browser.setAccessibleName(title)
        self._body = body_html
        self.browser.setHtml(f"<html><head><style>{_css()}</style></head><body>{body_html}</body></html>")
        lay.addWidget(self.browser, 1)
        bb = QDialogButtonBox(QDialogButtonBox.StandardButton.Close)
        bb.rejected.connect(self.reject)
        lay.addWidget(bb)


def quick_guide(parent: QWidget | None = None) -> HtmlDialog:
    return HtmlDialog("Guia rápido — BrokCAM", GUIDE_HTML, parent)


class ShortcutsDialog(QDialog):
    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setWindowTitle("Atalhos de teclado — BrokCAM")
        self.resize(720, 640)
        lay = QVBoxLayout(self)
        lay.setContentsMargins(12, 12, 12, 12)
        t = QTableWidget(len(SHORTCUTS), 3)
        t.setHorizontalHeaderLabels(["Área", "Atalho", "Ação"])
        t.verticalHeader().setVisible(False)
        t.setEditTriggers(QTableWidget.EditTrigger.NoEditTriggers)
        t.setAlternatingRowColors(True)
        t.setAccessibleName("Tabela de atalhos de teclado")
        for r, (area, keys, desc) in enumerate(SHORTCUTS):
            for c, v in enumerate((area, keys, desc)):
                t.setItem(r, c, QTableWidgetItem(v))
        hh = t.horizontalHeader()
        hh.setSectionResizeMode(0, QHeaderView.ResizeMode.ResizeToContents)
        hh.setSectionResizeMode(1, QHeaderView.ResizeMode.ResizeToContents)
        hh.setSectionResizeMode(2, QHeaderView.ResizeMode.Stretch)
        lay.addWidget(t, 1)
        bb = QDialogButtonBox(QDialogButtonBox.StandardButton.Close)
        bb.rejected.connect(self.reject)
        lay.addWidget(bb)


class AboutDialog(QDialog):
    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setWindowTitle("Sobre o BrokCAM")
        self.setMinimumWidth(520)
        lay = QVBoxLayout(self)
        lay.setContentsMargins(24, 24, 24, 16)
        lay.setSpacing(10)
        head = QHBoxLayout()
        logo = QLabel()
        logo.setPixmap(pixmap("board", 48, theme.palette().accent, self.devicePixelRatioF()))
        head.addWidget(logo)
        col = QVBoxLayout()
        t = QLabel("BrokCAM")
        t.setObjectName("Heading")
        t.setStyleSheet("font-size: 20pt;")
        v = QLabel(f"Versão {__version__} · CAM para PCB (isolamento) com GRBL")
        v.setObjectName("Muted")
        col.addWidget(t)
        col.addWidget(v)
        head.addLayout(col, 1)
        lay.addLayout(head)
        st = backend.status()
        mods = " · ".join(f"{k}: {'ok' if ok else 'pendente'}" for k, ok in st.items())
        body = QLabel(
            "<p>Reimplementação moderna do fluxo de trabalho do <b>FlatCAM</b>: Gerber/Excellon → "
            "isolamento, furação, recorte e limpeza de cobre → G-code GRBL → envio para a máquina.</p>"
            "<p>O BrokCAM usa o FlatCAM como base de referência funcional. FlatCAM é software livre "
            "sob licença <b>MIT</b>, criado por <b>Juan Pablo Caram</b> e mantido por "
            "<b>Marius Stanciu</b> e colaboradores. Nosso agradecimento a todos eles.</p>"
            "<p>Bibliotecas: Qt/PySide6 (LGPL), Shapely/GEOS, NumPy, pySerial.</p>"
            f"<p class='muted' style='color:{theme.palette().text_muted}'>Módulos: {mods}</p>")
        body.setWordWrap(True)
        body.setTextFormat(Qt.TextFormat.RichText)
        body.setOpenExternalLinks(True)
        lay.addWidget(body)
        bb = QDialogButtonBox(QDialogButtonBox.StandardButton.Ok)
        bb.accepted.connect(self.accept)
        lay.addWidget(bb)


def open_url(url: str) -> None:
    QDesktopServices.openUrl(QUrl(url))

