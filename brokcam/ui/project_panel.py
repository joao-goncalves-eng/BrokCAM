"""Dock "Projeto": árvore organizada em categorias — Gerber, Furação, Geometria e G-code.

* **Gerber**: camadas de cobre, contorno, máscara… (arquivos .gbr).
* **Furação (Excellon)**: arquivos de furos.
* **Geometria**: caminhos de ferramenta gerados pela CAM (e geometrias importadas).
* **G-code**: programas gerados a partir dos caminhos, ou abertos de arquivo.

Cada categoria tem um "olho" que mostra/oculta tudo dentro dela.
"""
from __future__ import annotations

from PySide6.QtCore import QEvent, QModelIndex, QPoint, QRect, QSize, Qt, Signal
from PySide6.QtGui import QAction, QColor, QFontMetrics, QKeyEvent, QPainter, QPen
from PySide6.QtWidgets import (
    QAbstractItemView, QColorDialog, QHBoxLayout, QLabel, QMenu, QStyle, QStyledItemDelegate,
    QStyleOptionViewItem, QToolButton, QTreeWidget, QTreeWidgetItem, QVBoxLayout, QWidget,
)

from . import theme
from .icons import icon, pixmap
from .project import JobEntry, LayerEntry
from .theme import ROLE_LABELS
from .units import fmt_len, fmt_time

R_KIND = Qt.ItemDataRole.UserRole + 1     # "layer" | "job"
R_ID = Qt.ItemDataRole.UserRole + 2
R_SUB = Qt.ItemDataRole.UserRole + 3
R_COLOR = Qt.ItemDataRole.UserRole + 4
R_VISIBLE = Qt.ItemDataRole.UserRole + 5
R_ICON = Qt.ItemDataRole.UserRole + 6

KIND_ICON = {"gerber": "copper", "excellon": "drill", "geometry": "outline"}

# (chave, título, ícone, descrição acessível)
CATEGORIES = (
    ("gerber", "Gerber", "copper", "Camadas Gerber: cobre, contorno, máscara, serigrafia"),
    ("excellon", "Furação (Excellon)", "drill", "Arquivos de furação"),
    ("geometry", "Geometria · caminhos de ferramenta", "isolation",
     "Caminhos de ferramenta gerados pela CAM e geometrias importadas"),
    ("gcode", "G-code", "gcode", "Programas G-code gerados ou abertos de arquivo"),
)
CAT_IDS = {key: -100 - i for i, (key, *_r) in enumerate(CATEGORIES)}
OP_ICON = {"isolation": "isolation", "ncc": "ncc", "cutout": "cutout", "drilling": "drill",
           "panelize": "panelize"}


class LayerDelegate(QStyledItemDelegate):
    EYE = 22

    def sizeHint(self, option: QStyleOptionViewItem, index: QModelIndex) -> QSize:  # noqa: N802
        fm = QFontMetrics(option.font)
        if index.data(R_KIND) == "category":
            return QSize(200, fm.height() + 16)
        return QSize(200, fm.height() * 2 + 14)

    def _paint_category(self, painter: QPainter, option: QStyleOptionViewItem, index: QModelIndex) -> None:
        p = theme.palette()
        r = option.rect
        dpr = painter.device().devicePixelRatioF() if painter.device() else 1.0
        if option.state & QStyle.StateFlag.State_MouseOver:
            painter.setPen(Qt.PenStyle.NoPen)
            painter.setBrush(QColor(p.hover))
            painter.drawRoundedRect(r.adjusted(2, 1, -2, -1), 5, 5)
        # linha divisória acima do título
        painter.setPen(QPen(QColor(p.border), 1))
        painter.drawLine(r.left() + 4, r.top() + 1, r.right() - 4, r.top() + 1)
        ic = index.data(R_ICON) or "layers"
        painter.drawPixmap(QRect(r.left() + 4, r.center().y() - 7, 14, 14), pixmap(ic, 14, p.accent, dpr))
        f = option.font
        f.setWeight(f.Weight.Bold)
        f.setPointSizeF(option.font.pointSizeF() * 0.82)
        f.setLetterSpacing(f.SpacingType.PercentageSpacing, 106)
        painter.setFont(f)
        painter.setPen(QColor(p.text_muted))
        title = str(index.data(Qt.ItemDataRole.DisplayRole) or "").upper()
        fm = QFontMetrics(f)
        er = self.eye_rect(r)
        count = str(index.data(R_SUB) or "")
        cw = fm.horizontalAdvance(count) + 12
        tx = r.left() + 24
        tw = er.left() - tx - cw - 6
        painter.drawText(QRect(tx, r.top(), tw, r.height()), Qt.AlignmentFlag.AlignVCenter,
                         fm.elidedText(title, Qt.TextElideMode.ElideRight, tw))
        # contador em pastilha
        badge = QRect(er.left() - cw - 4, r.center().y() - fm.height() // 2 - 1, cw, fm.height() + 2)
        painter.setPen(Qt.PenStyle.NoPen)
        painter.setBrush(QColor(p.surface_alt))
        painter.drawRoundedRect(badge, 7, 7)
        painter.setPen(QColor(p.text_muted))
        painter.drawText(badge, Qt.AlignmentFlag.AlignCenter, count)
        visible = bool(index.data(R_VISIBLE))
        painter.drawPixmap(er.adjusted(4, 4, -4, -4),
                           pixmap("eye" if visible else "eye-off", 14, p.text_muted if visible else p.text_disabled, dpr))

    def eye_rect(self, rect: QRect) -> QRect:
        return QRect(rect.right() - self.EYE - 6, rect.center().y() - self.EYE // 2, self.EYE, self.EYE)

    def paint(self, painter: QPainter, option: QStyleOptionViewItem, index: QModelIndex) -> None:
        p = theme.palette()
        painter.save()
        if index.data(R_KIND) == "category":
            self._paint_category(painter, option, index)
            painter.restore()
            return
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        r = option.rect
        selected = bool(option.state & QStyle.StateFlag.State_Selected)
        hover = bool(option.state & QStyle.StateFlag.State_MouseOver)
        if selected or hover:
            bg = QColor(p.selection if selected else p.hover)
            painter.setPen(Qt.PenStyle.NoPen)
            painter.setBrush(bg)
            painter.drawRoundedRect(r.adjusted(2, 1, -2, -1), 6, 6)
        if option.state & QStyle.StateFlag.State_HasFocus:
            painter.setPen(QPen(QColor(p.focus), 1.5))
            painter.setBrush(Qt.BrushStyle.NoBrush)
            painter.drawRoundedRect(r.adjusted(2, 1, -2, -1), 6, 6)
        visible = bool(index.data(R_VISIBLE))
        color = QColor(index.data(R_COLOR) or p.text_muted)
        txt_col = QColor(p.selection_text if selected else p.text)
        sub_col = QColor(p.selection_text if selected else p.text_muted)
        if not visible:
            txt_col.setAlphaF(0.55)
            sub_col.setAlphaF(0.55)
            color.setAlphaF(0.4)
        # ícone do tipo sobre pastilha colorida
        sw = QRect(r.left() + 8, r.center().y() - 13, 26, 26)
        bgc = QColor(color)
        bgc.setAlphaF(0.22 if visible else 0.1)
        painter.setPen(QPen(color, 1.2))
        painter.setBrush(bgc)
        painter.drawRoundedRect(sw, 6, 6)
        ic = index.data(R_ICON) or "geometry"
        dpr = painter.device().devicePixelRatioF() if painter.device() else 1.0
        painter.drawPixmap(sw.adjusted(5, 5, -5, -5), pixmap(ic, 16, color.name(), dpr))
        # textos
        fm = QFontMetrics(option.font)
        x = sw.right() + 10
        w = self.eye_rect(r).left() - x - 6
        name = fm.elidedText(str(index.data(Qt.ItemDataRole.DisplayRole) or ""), Qt.TextElideMode.ElideMiddle, w)
        f = QFontMetrics(option.font)
        bold = option.font
        bold.setWeight(bold.Weight.DemiBold)
        painter.setFont(bold)
        painter.setPen(txt_col)
        top = r.top() + (r.height() - 2 * f.height() - 2) // 2
        painter.drawText(QRect(x, top, w, f.height()), Qt.AlignmentFlag.AlignVCenter, name)
        small = option.font
        small.setWeight(small.Weight.Normal)
        small.setPointSizeF(option.font.pointSizeF() * 0.9)
        painter.setFont(small)
        painter.setPen(sub_col)
        sub = QFontMetrics(small).elidedText(str(index.data(R_SUB) or ""), Qt.TextElideMode.ElideRight, w)
        painter.drawText(QRect(x, top + f.height() + 2, w, f.height()), Qt.AlignmentFlag.AlignVCenter, sub)
        # olho
        er = self.eye_rect(r)
        eye_col = p.selection_text if selected else (p.text if visible else p.text_disabled)
        painter.drawPixmap(er.adjusted(3, 3, -3, -3), pixmap("eye" if visible else "eye-off", 16, eye_col, dpr))
        painter.restore()

    def editorEvent(self, event: QEvent, model, option: QStyleOptionViewItem, index: QModelIndex) -> bool:  # type: ignore[no-untyped-def]  # noqa: N802
        if event.type() == QEvent.Type.MouseButtonRelease:
            pos = event.position().toPoint()  # type: ignore[attr-defined]
            if self.eye_rect(option.rect).contains(pos):
                model.setData(index, not bool(index.data(R_VISIBLE)), R_VISIBLE)
                return True
        return super().editorEvent(event, model, option, index)


class ProjectTree(QTreeWidget):
    toggleVisible = Signal(QTreeWidgetItem)

    def keyPressEvent(self, ev: QKeyEvent) -> None:  # noqa: N802
        it = self.currentItem()
        if it is not None and ev.key() == Qt.Key.Key_Space:
            sel = self.selectedItems()
            if len(sel) > 1:
                on = not all(bool(i.data(0, R_VISIBLE)) for i in sel)
                for i in sel:
                    i.setData(0, R_VISIBLE, on)
            else:
                self.toggleVisible.emit(it)
            return
        super().keyPressEvent(ev)


class ProjectPanel(QWidget):
    """Lista de camadas e jobs. Emite sinais; a janela principal executa as ações."""

    selected = Signal(str, int)              # ("layer"|"job"|"", id)
    visibilityToggled = Signal(str, int, bool)
    action = Signal(str, str, int)           # (ação, tipo, id)
    batch = Signal(str, list)                # (ação, [(tipo, id), ...]) — seleção múltipla
    multiSelected = Signal(list)             # [(tipo, id), ...] quando há 2+ itens selecionados
    openRequested = Signal()

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setObjectName("Panel")
        lay = QVBoxLayout(self)
        lay.setContentsMargins(6, 6, 6, 6)
        lay.setSpacing(6)
        head = QHBoxLayout()
        head.setContentsMargins(4, 0, 0, 0)
        self.count = QLabel("Nenhuma camada")
        self.count.setObjectName("Muted")
        head.addWidget(self.count, 1)
        add = QToolButton()
        add.setIcon(icon("plus"))
        add.setToolTip("Abrir arquivos Gerber/Excellon (Ctrl+O)")
        add.setAccessibleName("Adicionar camadas")
        add.clicked.connect(self.openRequested.emit)
        self.remove_btn = QToolButton()
        self.remove_btn.setIcon(icon("trash"))
        self.remove_btn.setToolTip("Remover o item selecionado (Delete)")
        self.remove_btn.setAccessibleName("Remover item selecionado")
        self.remove_btn.clicked.connect(self._remove_current)
        self.vis_btn = QToolButton()
        self.vis_btn.setIcon(icon("eye"))
        self.vis_btn.setToolTip("Visibilidade: mostrar/ocultar todas as camadas e caminhos")
        self.vis_btn.setAccessibleName("Visibilidade de todas as camadas")
        self.vis_btn.setPopupMode(QToolButton.ToolButtonPopupMode.InstantPopup)
        vm = QMenu(self.vis_btn)
        vm.addAction(icon("eye"), "Mostrar tudo", lambda: self._set_all_visible(True, None))
        vm.addAction(icon("eye-off"), "Ocultar tudo", lambda: self._set_all_visible(False, None))
        vm.addSeparator()
        vm.addAction(icon("eye"), "Mostrar todas as camadas", lambda: self._set_all_visible(True, "layer"))
        vm.addAction(icon("eye-off"), "Ocultar todas as camadas", lambda: self._set_all_visible(False, "layer"))
        vm.addAction(icon("eye"), "Mostrar todos os caminhos", lambda: self._set_all_visible(True, "job"))
        vm.addAction(icon("eye-off"), "Ocultar todos os caminhos", lambda: self._set_all_visible(False, "job"))
        vm.addSeparator()
        vm.addAction(icon("check"), "Selecionar tudo (Ctrl+A)", self.select_all)
        self.vis_btn.setMenu(vm)
        head.addWidget(self.vis_btn)
        head.addWidget(add)
        head.addWidget(self.remove_btn)
        lay.addLayout(head)

        self.tree = ProjectTree()
        self.tree.setHeaderHidden(True)
        self.tree.setRootIsDecorated(True)
        self.tree.setIndentation(14)
        self.tree.setUniformRowHeights(False)
        self.tree.setMouseTracking(True)
        self.tree.setAccessibleName("Camadas e caminhos do projeto")
        self.tree.setAccessibleDescription(
            "Ctrl/Shift+clique seleciona vários, Ctrl+A seleciona tudo, Espaço mostra/oculta, Delete remove, "
            "menu de contexto com mais opções.")
        self.tree.setSelectionMode(QAbstractItemView.SelectionMode.ExtendedSelection)
        self.tree.setContextMenuPolicy(Qt.ContextMenuPolicy.CustomContextMenu)
        self.tree.customContextMenuRequested.connect(self._menu)
        self.delegate = LayerDelegate(self.tree)
        self.tree.setItemDelegate(self.delegate)
        # o delegate desenha a seleção; evita a faixa padrão na área de recuo
        self.tree.setStyleSheet("QTreeView { selection-background-color: transparent; }"
                                "QTreeView::item:selected, QTreeView::branch:selected { background: transparent; }")
        self.tree.currentItemChanged.connect(self._current_changed)
        self.tree.itemSelectionChanged.connect(self._selection_changed)
        self.tree.toggleVisible.connect(self._toggle_item)
        self.tree.model().dataChanged.connect(self._data_changed)
        lay.addWidget(self.tree, 1)
        self.items: dict[tuple[str, int], QTreeWidgetItem] = {}
        self._updating = False
        self.cats: dict[str, QTreeWidgetItem] = {}
        self._job_src: dict[int, str] = {}
        # nome do grupo de uma camada (definido pela janela principal, que conhece o projeto)
        self.group_name_of = lambda gid: ""
        self._build_categories()
        self.remove_btn.setEnabled(False)

    # ------------------------------------------------------------------ categorias
    def _build_categories(self) -> None:
        for key, title, ic, desc in CATEGORIES:
            it = QTreeWidgetItem([title])
            it.setData(0, R_KIND, "category")
            it.setData(0, R_ID, CAT_IDS[key])
            it.setData(0, R_ICON, ic)
            it.setData(0, R_VISIBLE, True)
            it.setData(0, Qt.ItemDataRole.AccessibleTextRole, desc)
            it.setFlags(Qt.ItemFlag.ItemIsEnabled)
            self.tree.addTopLevelItem(it)
            it.setExpanded(True)
            it.setHidden(True)
            self.cats[key] = it

    def _cat_for_layer(self, e: LayerEntry) -> QTreeWidgetItem:
        return self.cats.get(e.kind, self.cats["geometry"])

    def _refresh_categories(self) -> None:
        self._updating = True
        for key, it in self.cats.items():
            n = it.childCount()
            it.setHidden(n == 0)
            it.setData(0, R_SUB, str(n))
            it.setData(0, R_VISIBLE, any(bool(it.child(i).data(0, R_VISIBLE)) for i in range(n)) if n else True)
        self._updating = False

    # ------------------------------------------------------------------ itens
    def _layer_sub(self, e: LayerEntry) -> str:
        role = ROLE_LABELS.get(e.role, e.role)
        t = e.transform
        extra = []
        if t.mirror:
            extra.append("espelhada")
        if t.offset != (0.0, 0.0):
            extra.append("deslocada")
        s = f"{role} · {e.summary()}"
        g = self.group_name_of(e.group) if e.group is not None else ""
        if g:
            s = f"⧉ {g} · {s}"
        return s + (f" · {', '.join(extra)}" if extra else "")

    def _job_sub(self, j: JobEntry) -> str:
        st = j.job.stats()
        parts = []
        if st.get("cut_length_mm"):
            parts.append(fmt_len(st["cut_length_mm"], 0))
        if st.get("holes"):
            parts.append(f"{int(st['holes'])} furos")
        bits = (j.job.settings or {}).get("bits") if j.job.operation == "drilling" else None
        if bits and len(bits) > 1:
            parts.append(f"{len(bits)} brocas")
        if j.estimate_s:
            parts.append(f"≈ {fmt_time(j.estimate_s)}")
        if j.job.gcode:
            parts.append("G-code ✓")
        return " · ".join(parts) or "vazio"

    def add_layer(self, e: LayerEntry) -> None:
        it = QTreeWidgetItem([e.name])
        it.setData(0, R_KIND, "layer")
        it.setData(0, R_ID, e.id)
        it.setFlags(it.flags() & ~Qt.ItemFlag.ItemIsEditable)
        self.items[("layer", e.id)] = it
        self._cat_for_layer(e).addChild(it)
        self.update_layer(e)
        self._update_count()

    def update_layer(self, e: LayerEntry) -> None:
        it = self.items.get(("layer", e.id))
        if it is None:
            return
        self._updating = True
        it.setText(0, e.name)
        it.setData(0, R_SUB, self._layer_sub(e))
        it.setData(0, R_COLOR, e.color)
        it.setData(0, R_VISIBLE, e.visible)
        it.setData(0, R_ICON, KIND_ICON.get(e.kind, "geometry"))
        vis = "visível" if e.visible else "oculta"
        it.setData(0, Qt.ItemDataRole.AccessibleTextRole, f"{e.name}, {ROLE_LABELS.get(e.role, e.role)}, {vis}")
        it.setToolTip(0, f"{e.layer.source_path or 'gerada no BrokCAM'}\n{self._layer_sub(e)}")
        self._updating = False
        self._refresh_categories()

    def remove(self, kind: str, id_: int) -> None:
        it = self.items.pop((kind, id_), None)
        if it is None:
            return
        parent = it.parent()
        if parent is not None:
            parent.removeChild(it)
        else:
            self.tree.takeTopLevelItem(self.tree.indexOfTopLevelItem(it))
        for k in [k for k, v in self.items.items() if v.parent() is it]:
            self.items.pop(k, None)
        if kind == "job":
            self._job_src.pop(id_, None)
        self._refresh_categories()
        self._update_count()

    def add_job(self, j: JobEntry, source_name: str = "") -> None:
        it = QTreeWidgetItem([j.name])
        it.setData(0, R_KIND, "job")
        it.setData(0, R_ID, j.id)
        self.items[("job", j.id)] = it
        self._job_src[j.id] = source_name
        self.cats["geometry"].addChild(it)
        self.update_job(j)
        self._update_count()

    def upsert_gcode(self, kind: str, id_: int, name: str, sub: str, visible: bool) -> None:
        """Cria/atualiza uma entrada na categoria G-code (``kind`` "gcode" = de um caminho,
        "program" = programa completo ou arquivo aberto)."""
        it = self.items.get((kind, id_))
        if it is None:
            it = QTreeWidgetItem([name])
            it.setData(0, R_KIND, kind)
            it.setData(0, R_ID, id_)
            self.items[(kind, id_)] = it
            self.cats["gcode"].addChild(it)
        self._updating = True
        it.setText(0, name)
        it.setData(0, R_SUB, sub)
        it.setData(0, R_COLOR, theme.palette().done)
        it.setData(0, R_VISIBLE, visible)
        it.setData(0, R_ICON, "gcode")
        it.setData(0, Qt.ItemDataRole.AccessibleTextRole,
                   f"G-code {name}, {sub}, {'exibido no canvas' if visible else 'oculto'}")
        it.setToolTip(0, f"{name}\n{sub}")
        self._updating = False
        self._refresh_categories()
        self._update_count()

    def set_gcode_visible(self, kind: str, id_: int, visible: bool) -> None:
        it = self.items.get((kind, id_))
        if it is not None:
            self._updating = True
            it.setData(0, R_VISIBLE, visible)
            self._updating = False
            self._refresh_categories()

    def update_job(self, j: JobEntry) -> None:
        it = self.items.get(("job", j.id))
        if it is None:
            return
        self._updating = True
        it.setText(0, j.name)
        src = self._job_src.get(j.id, "")
        it.setData(0, R_SUB, self._job_sub(j) + (f" · de {src}" if src else ""))
        it.setData(0, R_COLOR, j.color or theme.palette().cut)
        it.setData(0, R_VISIBLE, j.visible)
        it.setData(0, R_ICON, OP_ICON.get(j.job.operation, "job"))
        it.setData(0, Qt.ItemDataRole.AccessibleTextRole,
                   f"Caminho {j.name}, {self._job_sub(j)}, {'visível' if j.visible else 'oculto'}")
        self._updating = False
        self._refresh_categories()

    def clear(self) -> None:
        for it in list(self.items.values()):
            parent = it.parent()
            if parent is not None:
                parent.removeChild(it)
        self.items.clear()
        self._job_src.clear()
        self._refresh_categories()
        self._update_count()

    def select(self, kind: str, id_: int) -> None:
        it = self.items.get((kind, id_))
        if it is not None:
            self.tree.setCurrentItem(it)
        else:
            self.tree.clearSelection()
            self.tree.setCurrentItem(None)  # type: ignore[arg-type]

    def current(self) -> tuple[str, int]:
        it = self.tree.currentItem()
        if it is None or it.data(0, R_KIND) == "category":
            return "", -1
        return str(it.data(0, R_KIND)), int(it.data(0, R_ID))

    def selected_items(self) -> list[tuple[str, int]]:
        """Itens selecionados (vários com Ctrl/Shift), na ordem da árvore."""
        sel = set(id(i) for i in self.tree.selectedItems())
        out = []
        for key, it in self.items.items():
            if id(it) in sel:
                out.append(key)
        return out

    def select_all(self) -> None:
        self.tree.selectAll()

    def select_many(self, keys: list[tuple[str, int]], current: tuple[str, int] | None = None) -> None:
        """Seleciona vários itens (ex.: todas as camadas de um grupo)."""
        self.tree.blockSignals(True)
        self.tree.clearSelection()
        cur_it = self.items.get(current) if current else None
        if cur_it is not None:
            self.tree.setCurrentItem(cur_it)
        for k in keys:
            it = self.items.get(k)
            if it is not None:
                it.setSelected(True)
        self.tree.blockSignals(False)
        self.tree.viewport().update()
        self._selection_changed()

    def _set_all_visible(self, on: bool, kind: str | None) -> None:
        for (k, _i), it in self.items.items():
            if k in ("gcode", "program") and kind is None and on:
                continue          # "mostrar tudo" não liga pré-visualizações de G-code
            if kind is None or k == kind:
                if bool(it.data(0, R_VISIBLE)) != on:
                    it.setData(0, R_VISIBLE, on)

    def _selection_changed(self) -> None:
        sel = self.selected_items()
        self.remove_btn.setEnabled(bool(sel))
        if len(sel) > 1:
            self.multiSelected.emit(sel)

    def _update_count(self) -> None:
        n_l = sum(1 for k in self.items if k[0] == "layer")
        n_j = sum(1 for k in self.items if k[0] == "job")
        n_g = sum(1 for k in self.items if k[0] in ("gcode", "program"))
        if not n_l and not n_j and not n_g:
            self.count.setText("Nenhuma camada")
        else:
            txt = f"{n_l} camada{'s' if n_l != 1 else ''} · {n_j} caminho{'s' if n_j != 1 else ''}"
            if n_g:
                txt += f" · {n_g} G-code"
            self.count.setText(txt)

    # ------------------------------------------------------------------ eventos
    def _current_changed(self, cur: QTreeWidgetItem | None, _prev: QTreeWidgetItem | None) -> None:
        if cur is not None and cur.data(0, R_KIND) == "category":
            return
        self.remove_btn.setEnabled(cur is not None)
        if cur is None:
            self.selected.emit("", -1)
        else:
            self.selected.emit(str(cur.data(0, R_KIND)), int(cur.data(0, R_ID)))

    def _toggle_item(self, it: QTreeWidgetItem) -> None:
        it.setData(0, R_VISIBLE, not bool(it.data(0, R_VISIBLE)))

    def _data_changed(self, tl: QModelIndex, _br: QModelIndex, roles: list[int]) -> None:
        if self._updating or (roles and R_VISIBLE not in roles):
            return
        it = self.tree.itemFromIndex(tl)
        if it is None:
            return
        kind = str(it.data(0, R_KIND))
        on = bool(it.data(0, R_VISIBLE))
        if kind == "category":
            # olho da categoria: aplica a todos os itens dentro dela
            for i in range(it.childCount()):
                ch = it.child(i)
                if bool(ch.data(0, R_VISIBLE)) != on:
                    ch.setData(0, R_VISIBLE, on)
            self._refresh_categories()
            return
        self.visibilityToggled.emit(kind, int(it.data(0, R_ID)), on)
        self._refresh_categories()

    def _remove_current(self) -> None:
        sel = self.selected_items()
        if len(sel) > 1:
            self.batch.emit("remove", sel)
            return
        kind, id_ = self.current()
        if kind:
            self.action.emit("remove", kind, id_)

    def keyPressEvent(self, ev: QKeyEvent) -> None:  # noqa: N802
        if ev.key() == Qt.Key.Key_Delete:
            self._remove_current()
            return
        super().keyPressEvent(ev)

    def _menu(self, pos: QPoint) -> None:
        it = self.tree.itemAt(pos)
        if it is None:
            m = QMenu(self)
            m.addAction(icon("open"), "Abrir arquivos…", self.openRequested.emit)
            m.exec(self.tree.viewport().mapToGlobal(pos))
            return
        kind, id_ = str(it.data(0, R_KIND)), int(it.data(0, R_ID))
        if kind == "category":
            m = QMenu(self)
            vis = bool(it.data(0, R_VISIBLE))
            m.addAction(icon("eye-off" if vis else "eye"), "Ocultar tudo nesta categoria" if vis
                        else "Mostrar tudo nesta categoria", lambda: it.setData(0, R_VISIBLE, not vis))
            m.addAction(icon("check"), "Selecionar tudo nesta categoria",
                        lambda: [it.child(i).setSelected(True) for i in range(it.childCount())])
            m.exec(self.tree.viewport().mapToGlobal(pos))
            return
        if kind in ("gcode", "program"):
            self.tree.setCurrentItem(it)
            m = QMenu(self)
            vis = bool(it.data(0, R_VISIBLE))
            m.addAction(icon("eye-off" if vis else "eye"), "Ocultar do canvas" if vis else "Mostrar no canvas",
                        lambda: self._toggle_item(it))
            m.addAction(icon("save"), "Salvar G-code…", lambda: self.action.emit("gcode_save", kind, id_))
            m.addAction(icon("send"), "Enviar à máquina", lambda: self.action.emit("gcode_send", kind, id_))
            m.addSeparator()
            m.addAction(icon("trash"), "Remover G-code", lambda: self.action.emit("remove", kind, id_))
            m.exec(self.tree.viewport().mapToGlobal(pos))
            return
        sel = self.selected_items()
        if len(sel) > 1 and (kind, id_) in sel:
            self._multi_menu(sel, pos)
            return
        self.tree.setCurrentItem(it)
        m = QMenu(self)
        vis = bool(it.data(0, R_VISIBLE))
        m.addAction(icon("eye-off" if vis else "eye"), "Ocultar" if vis else "Mostrar",
                    lambda: self._toggle_item(it))
        m.addAction(icon("fit"), "Enquadrar no canvas", lambda: self.action.emit("zoom", kind, id_))
        if kind == "layer":
            m.addAction(icon("dot"), "Cor…", lambda: self._pick_color(it, kind, id_))
            roles = m.addMenu("Papel da camada")
            for key, lab in ROLE_LABELS.items():
                a = QAction(lab, roles)
                a.triggered.connect(lambda _=False, k=key: self.action.emit(f"role:{k}", kind, id_))
                roles.addAction(a)
            m.addAction(icon("mirror"), "Dupla face — virar com o lado de baixo…",
                        lambda: self.action.emit("flip", kind, id_))
            m.addAction(icon("group"), "Selecionar o grupo desta camada",
                        lambda: self.action.emit("select_group", kind, id_))
            m.addAction(icon("ungroup"), "Desagrupar", lambda: self.action.emit("ungroup", kind, id_))
            mir = m.addMenu(icon("mirror"), "Espelhar só esta camada")
            mir.addAction("Espelhar horizontalmente (eixo X)", lambda: self.action.emit("mirror:x", kind, id_))
            mir.addAction("Espelhar verticalmente (eixo Y)", lambda: self.action.emit("mirror:y", kind, id_))
            mir.addAction("Remover espelhamento", lambda: self.action.emit("mirror:none", kind, id_))
        else:
            m.addAction(icon("gcode"), "Gerar G-code", lambda: self.action.emit("gcode", kind, id_))
            m.addAction(icon("simulate"), "Simular", lambda: self.action.emit("simulate", kind, id_))
            m.addAction(icon("edit-path"), "Editar caminhos…", lambda: self.action.emit("edit", kind, id_))
            m.addAction(icon("save"), "Salvar G-code em pasta…", lambda: self.batch.emit("save_gcodes", [(kind, id_)]))
        m.addSeparator()
        m.addAction(icon("trash"), "Remover", lambda: self.action.emit("remove", kind, id_))
        m.exec(self.tree.viewport().mapToGlobal(pos))

    def _multi_menu(self, sel: list[tuple[str, int]], pos: QPoint) -> None:
        n_layers = sum(1 for k, _ in sel if k == "layer")
        m = QMenu(self)
        title = m.addAction(f"{len(sel)} itens selecionados")
        title.setEnabled(False)
        m.addSeparator()
        m.addAction(icon("eye"), "Mostrar", lambda: self.batch.emit("show", sel))
        m.addAction(icon("eye-off"), "Ocultar", lambda: self.batch.emit("hide", sel))
        m.addAction(icon("eye"), "Mostrar só estes", lambda: self.batch.emit("solo", sel))
        m.addAction(icon("fit"), "Enquadrar no canvas", lambda: self.batch.emit("zoom", sel))
        n_jobs = sum(1 for k, _ in sel if k == "job")
        if n_jobs:
            m.addSeparator()
            m.addAction(icon("save"), f"Salvar G-codes dos {n_jobs} caminhos numa pasta…",
                        lambda: self.batch.emit("save_gcodes", sel))
        if n_layers:
            m.addSeparator()
            if n_layers >= 2:
                m.addAction(icon("group"), f"Agrupar {n_layers} camadas (Ctrl+G)",
                            lambda: self.batch.emit("group", sel))
            m.addAction(icon("ungroup"), "Desagrupar", lambda: self.batch.emit("ungroup", sel))
            m.addAction(icon("mirror"), f"Virar {n_layers} camada(s) — dupla face…",
                        lambda: self.batch.emit("flip", sel))
        m.addSeparator()
        m.addAction(icon("trash"), f"Remover {len(sel)} itens", lambda: self.batch.emit("remove", sel))
        m.exec(self.tree.viewport().mapToGlobal(pos))

    def _pick_color(self, it: QTreeWidgetItem, kind: str, id_: int) -> None:
        c = QColorDialog.getColor(QColor(it.data(0, R_COLOR) or "#888888"), self, "Cor da camada")
        if c.isValid():
            self.action.emit(f"color:{c.name()}", kind, id_)
