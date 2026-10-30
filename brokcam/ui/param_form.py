"""Formulários gerados automaticamente a partir de ``ParamGroup``/``ParamSpec``.

Cada ``ParamSpec`` vira um editor adequado ao tipo, com tooltip curto, nome e
descrição acessíveis, botão "restaurar padrão" e indicação visual de valor
alterado. Um :class:`HelpPanel` abaixo do formulário mostra a explicação completa
do campo em foco ou sob o mouse. Opções ``advanced`` ficam ocultas até o usuário
pedir. Presets podem ser salvos por grupo.
"""
from __future__ import annotations

import html
from typing import Any, Callable

from PySide6.QtCore import QEvent, QObject, QSize, Qt, Signal
from PySide6.QtWidgets import (
    QAbstractSpinBox, QCheckBox, QComboBox, QDoubleSpinBox, QFrame, QGridLayout, QHBoxLayout,
    QInputDialog, QLabel, QLineEdit, QMessageBox, QPlainTextEdit, QPushButton, QScrollArea, QSizePolicy,
    QSpinBox, QToolButton, QVBoxLayout, QWidget,
)

from brokcam.core.params import ParamGroup, ParamSpec

from . import settings as app_settings
from . import theme, units
from .icons import icon

_MULTILINE_HINTS = ("gcode", "header", "footer", "preamble", "postamble", "start_code",
                    "end_code", "toolchange_code", "custom", "macro")


def _is_multiline(spec: ParamSpec) -> bool:
    if spec.kind != "str":
        return False
    if isinstance(spec.default, str) and "\n" in spec.default:
        return True
    k = spec.key.lower()
    return any(h in k for h in _MULTILINE_HINTS)


def _fmt_value(spec: ParamSpec, value: Any) -> str:
    if spec.kind == "bool":
        return "ligado" if value else "desligado"
    if spec.kind == "choice":
        for v, lab in spec.choices:
            if v == value:
                return lab
        return str(value)
    if spec.kind in ("float", "int"):
        factor, unit = units.spec_factor(spec.unit)
        dec = 0 if spec.kind == "int" else min(6, spec.decimals + (1 if factor != 1 else 0))
        txt = f"{float(value) / factor:.{dec}f}".replace(".", ",")
        return f"{txt} {unit}".strip()
    if spec.kind == "str":
        v = str(value)
        if not v:
            return "(vazio)"
        return v if len(v) < 40 else v[:37].replace("\n", " ") + "…"
    return str(value)


def spec_range_text(spec: ParamSpec) -> str:
    if spec.kind not in ("float", "int"):
        return ""
    factor, unit = units.spec_factor(spec.unit)
    def f(v: float) -> str:
        dec = 0 if spec.kind == "int" else min(6, spec.decimals)
        return f"{v / factor:.{dec}f}".replace(".", ",")
    lo, hi = spec.minimum, spec.maximum
    if lo is not None and hi is not None:
        return f"{f(lo)} a {f(hi)} {unit}".strip()
    if lo is not None:
        return f"≥ {f(lo)} {unit}".strip()
    if hi is not None:
        return f"≤ {f(hi)} {unit}".strip()
    return ""


# Verificações de bom senso que geram aviso visual (não bloqueiam).
def _sanity_warning(spec: ParamSpec, value: Any) -> str:
    k = spec.key.lower()
    try:
        if k.endswith("cut_z") and float(value) > 0:
            return "Profundidade de corte positiva fica ACIMA da superfície: nada será cortado."
        if (k.endswith("travel_z") or k.endswith("safe_z")) and float(value) <= 0:
            return "Altura de deslocamento ≤ 0 pode arrastar a ferramenta na placa."
        if ("feed" in k or "rate" in k) and spec.kind in ("float", "int") and float(value) <= 0:
            return "Avanço zero ou negativo: o GRBL recusará o movimento (erro 22)."
        if ("diameter" in k or "dia" == k or k.endswith("_dia")) and float(value) <= 0:
            return "O diâmetro da ferramenta precisa ser maior que zero."
    except (TypeError, ValueError):
        return ""
    return ""


class HelpPanel(QFrame):
    """Painel de ajuda contextual: explicação completa do campo em foco."""

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setObjectName("HelpPanel")
        self.setAccessibleName("Ajuda do campo")
        lay = QVBoxLayout(self)
        lay.setContentsMargins(12, 10, 12, 10)
        lay.setSpacing(4)
        head = QHBoxLayout()
        head.setSpacing(6)
        self._icon = QLabel()
        self._icon.setFixedSize(16, 16)
        self.title = QLabel()
        self.title.setObjectName("HelpTitle")
        self.title.setWordWrap(True)
        head.addWidget(self._icon, 0, Qt.AlignmentFlag.AlignTop)
        head.addWidget(self.title, 1)
        lay.addLayout(head)
        self.body = QLabel()
        self.body.setWordWrap(True)
        self.body.setTextFormat(Qt.TextFormat.RichText)
        self.body.setAlignment(Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignTop)
        self.body.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
        self.body.setSizePolicy(QSizePolicy.Policy.Preferred, QSizePolicy.Policy.Minimum)
        self.body_scroll = QScrollArea()
        self.body_scroll.setWidgetResizable(True)
        self.body_scroll.setFrameShape(QFrame.Shape.NoFrame)
        self.body_scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        self.body_scroll.setWidget(self.body)
        self.body_scroll.setSizeAdjustPolicy(QScrollArea.SizeAdjustPolicy.AdjustToContents)
        lay.addWidget(self.body_scroll, 1)
        self.meta = QLabel()
        self.meta.setObjectName("Muted")
        self.meta.setWordWrap(True)
        lay.addWidget(self.meta)
        self._default_title = ""
        self._default_body = ""
        theme.on_change(self, lambda _p: self._refresh_icon())
        self._refresh_icon()

    def _refresh_icon(self) -> None:
        from .icons import pixmap
        self._icon.setPixmap(pixmap("info", 16, theme.palette().accent, self.devicePixelRatioF()))

    def set_default(self, title: str, text: str) -> None:
        self._default_title, self._default_body = title, text
        self.show_default()

    def show_default(self) -> None:
        self.title.setText(self._default_title)
        self.body.setText(_paragraphs(self._default_body))
        self.meta.setText("Passe o mouse ou foque um campo para ver a explicação completa.")

    def show_spec(self, spec: ParamSpec, warning: str = "") -> None:
        factor, unit = units.spec_factor(spec.unit)
        title = spec.label + (f" ({unit})" if unit else "")
        self.title.setText(title)
        body = _paragraphs(spec.help)
        if warning:
            col = theme.palette().warning
            body += f'<p style="color:{col}; font-weight:600">⚠ {html.escape(warning)}</p>'
        self.body.setText(body)
        parts = [f"Padrão: {_fmt_value(spec, spec.default)}"]
        rng = spec_range_text(spec)
        if rng:
            parts.append(f"Faixa: {rng}")
        if spec.advanced:
            parts.append("Opção avançada")
        self.meta.setText("  ·  ".join(parts))
        self.setAccessibleDescription(spec.help)


def _paragraphs(text: str) -> str:
    paras = [p.strip() for p in (text or "").split("\n\n") if p.strip()]
    return "".join(f"<p style='margin:0 0 6px 0'>{html.escape(p).replace(chr(10), '<br>')}</p>"
                   for p in paras)


class _HoverFilter(QObject):
    def __init__(self, on_enter: Callable[[], None], on_leave: Callable[[], None]) -> None:
        super().__init__()
        self.on_enter, self.on_leave = on_enter, on_leave

    def eventFilter(self, obj: QObject, ev: QEvent) -> bool:  # noqa: N802
        t = ev.type()
        if t in (QEvent.Type.Enter, QEvent.Type.FocusIn):
            self.on_enter()
        elif t == QEvent.Type.Leave:
            self.on_leave()
        return False


class _NoWheelMixin:
    """Evita trocar valores ao rolar a página com a roda do mouse sem foco."""

    def wheelEvent(self, ev):  # type: ignore[no-untyped-def]  # noqa: N802
        if self.hasFocus():  # type: ignore[attr-defined]
            super().wheelEvent(ev)  # type: ignore[misc]
        else:
            ev.ignore()


class DoubleSpin(_NoWheelMixin, QDoubleSpinBox):
    pass


class IntSpin(_NoWheelMixin, QSpinBox):
    pass


class Combo(_NoWheelMixin, QComboBox):
    """Combo que não força a largura do painel pelo item mais longo."""

    def __init__(self, *a, **k) -> None:  # type: ignore[no-untyped-def]
        super().__init__(*a, **k)
        self.setSizeAdjustPolicy(QComboBox.SizeAdjustPolicy.AdjustToMinimumContentsLengthWithIcon)
        self.setMinimumContentsLength(8)
        self.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Fixed)

    def showPopup(self) -> None:  # noqa: N802
        self.view().setMinimumWidth(self.view().sizeHintForColumn(0) + 24)
        super().showPopup()


class FieldRow:
    """Um campo do formulário (rótulo + editor + restaurar)."""

    def __init__(self, form: "ParamForm", spec: ParamSpec) -> None:
        self.form = form
        self.spec = spec
        self.locked = False
        self.manual_value: Any = None       # valor do usuário antes de o campo ser travado
        self._exact: float | None = None    # último valor exato (mm) recebido por set_value
        self.factor, self.unit = units.spec_factor(spec.unit, form.display_units)
        self.label = QLabel(spec.label)
        self.label.setWordWrap(True)
        self.label.setToolTip(spec.tooltip)
        self.editor = self._make_editor()
        self.editor.setToolTip(spec.tooltip)
        self.editor.setAccessibleName(spec.label + (f" ({self.unit})" if self.unit else ""))
        self.editor.setAccessibleDescription(spec.help)
        self.editor.setWhatsThis(spec.help)
        self.label.setBuddy(self.editor)
        self.reset_btn = QToolButton()
        self.reset_btn.setIcon(icon("restore"))
        self.reset_btn.setIconSize(QSize(14, 14))
        self.reset_btn.setAutoRaise(True)
        self.reset_btn.setToolTip(f"Restaurar padrão ({_fmt_value(spec, spec.default)})")
        self.reset_btn.setAccessibleName(f"Restaurar padrão de {spec.label}")
        self.reset_btn.setFocusPolicy(Qt.FocusPolicy.TabFocus)
        sp = self.reset_btn.sizePolicy()
        sp.setRetainSizeWhenHidden(True)
        self.reset_btn.setSizePolicy(sp)
        self.reset_btn.clicked.connect(self.reset)
        self._filter = _HoverFilter(lambda: form._show_help(self), form._hover_left)
        self._filter.setParent(form)
        for w in (self.label, self.editor):
            w.installEventFilter(self._filter)
        if isinstance(self.editor, QAbstractSpinBox):
            le = self.editor.findChild(QLineEdit)
            if le is not None:
                le.installEventFilter(self._filter)

    # ------------------------------------------------------------------ editores
    def _make_editor(self) -> QWidget:
        s = self.spec
        if s.kind == "float":
            w = DoubleSpin()
            dec = min(6, s.decimals + (1 if self.factor != 1 else 0))
            w.setDecimals(dec)
            lo = s.minimum if s.minimum is not None else -1e6
            hi = s.maximum if s.maximum is not None else 1e6
            w.setRange(lo / self.factor, hi / self.factor)
            step = s.step if s.step is not None else max(10 ** -s.decimals, 0.1 if s.decimals <= 2 else 10 ** -(s.decimals - 1))
            w.setSingleStep(step / self.factor)
            if self.unit:
                w.setSuffix(f" {self.unit}")
            w.setKeyboardTracking(False)
            w.setAlignment(Qt.AlignmentFlag.AlignRight)
            w.setMinimumWidth(104)
            w.setStepType(QAbstractSpinBox.StepType.DefaultStepType)
            w.valueChanged.connect(self._changed)
            return w
        if s.kind == "int":
            w = IntSpin()
            lo = int(s.minimum) if s.minimum is not None else -2_000_000_000
            hi = int(s.maximum) if s.maximum is not None else 2_000_000_000
            w.setRange(lo, hi)
            w.setSingleStep(int(s.step) if s.step else 1)
            if self.unit:
                w.setSuffix(f" {self.unit}")
            w.setKeyboardTracking(False)
            w.setAlignment(Qt.AlignmentFlag.AlignRight)
            w.setMinimumWidth(104)
            w.valueChanged.connect(self._changed)
            return w
        if s.kind == "bool":
            w = QCheckBox()
            w.setText("Ativado")
            w.toggled.connect(lambda on: (w.setText("Ativado" if on else "Desativado"), self._changed()))
            return w
        if s.kind == "choice":
            w = Combo()
            for val, lab in s.choices:
                w.addItem(lab, val)
            w.currentIndexChanged.connect(self._changed)
            w.setMinimumWidth(104)
            return w
        if _is_multiline(s):
            w = QPlainTextEdit()
            w.setFont(theme.mono_font(self.form.font_scale(), -0.5))
            w.setTabChangesFocus(True)
            w.setMinimumHeight(72)
            w.setMaximumHeight(140)
            w.textChanged.connect(self._changed)
            return w
        w = QLineEdit()
        w.textChanged.connect(self._changed)
        return w

    # ------------------------------------------------------------------ valores
    def value(self) -> Any:
        e = self.editor
        s = self.spec
        if s.kind == "float":
            v = float(e.value()) * self.factor  # type: ignore[attr-defined]
            # o spinbox arredonda (ex.: polegadas com 4 casas = 2,5 µm); se o texto exibido
            # ainda corresponde ao último valor exato recebido, devolve esse valor exato
            exact = self._exact
            if exact is not None and abs(v - exact) <= 0.51 * 10 ** -e.decimals() * self.factor:  # type: ignore[attr-defined]
                return exact
            return round(v, 9)
        if s.kind == "int":
            return int(e.value())  # type: ignore[attr-defined]
        if s.kind == "bool":
            return bool(e.isChecked())  # type: ignore[attr-defined]
        if s.kind == "choice":
            return e.currentData()  # type: ignore[attr-defined]
        if isinstance(e, QPlainTextEdit):
            return e.toPlainText()
        return e.text()  # type: ignore[attr-defined]

    def set_value(self, value: Any) -> None:
        s = self.spec
        e = self.editor
        try:
            value = s.validate(value)
        except (TypeError, ValueError):
            value = s.default
        e.blockSignals(True)
        try:
            if s.kind == "float":
                e.setValue(float(value) / self.factor)  # type: ignore[attr-defined]
                self._exact = float(value)
            elif s.kind == "int":
                e.setValue(int(value))  # type: ignore[attr-defined]
            elif s.kind == "bool":
                e.setChecked(bool(value))  # type: ignore[attr-defined]
                e.setText("Ativado" if value else "Desativado")  # type: ignore[attr-defined]
            elif s.kind == "choice":
                idx = e.findData(value)  # type: ignore[attr-defined]
                e.setCurrentIndex(max(0, idx))  # type: ignore[attr-defined]
            elif isinstance(e, QPlainTextEdit):
                e.setPlainText(str(value))
            else:
                e.setText(str(value))  # type: ignore[attr-defined]
        finally:
            e.blockSignals(False)
        self._update_state()

    def set_locked(self, on: bool) -> None:
        """Trava/destrava o campo (valor calculado pelo programa, somente leitura)."""
        if on == self.locked:
            return
        self.locked = on
        e = self.editor
        if isinstance(e, (QAbstractSpinBox, QLineEdit, QPlainTextEdit)):
            # somente leitura: o spinbox também deixa de aceitar setas/teclas de passo.
            # (Não use NoButtons: com o QSS do tema a área de texto encolhe para 1 px.)
            e.setReadOnly(on)
        else:
            e.setEnabled(not on)
        self.label.setText(self.spec.label + (" (automático)" if on else ""))
        unit = f" ({self.unit})" if self.unit else ""
        e.setAccessibleName(self.spec.label + unit + (", calculado automaticamente, somente leitura" if on else ""))
        theme.set_property(e, "locked", on)
        inner = e.findChild(QLineEdit) if isinstance(e, QAbstractSpinBox) else None
        if inner is not None:
            # o QLineEdit interno do spinbox não é repolido sozinho: sem isto a cor do texto
            # some (fica preta sobre o fundo escuro) ao travar/destravar
            inner.style().unpolish(inner)
            inner.style().polish(inner)
            inner.update()
        self._update_state()

    def _help_note(self) -> str:
        return "\n\n".join(t for t in (self.warning(), self.lock_note()) if t)

    def lock_note(self) -> str:
        return (self.spec.locked_note or "Valor calculado automaticamente.") if self.locked else ""

    def is_default(self) -> bool:
        v, d = self.value(), self.spec.default
        if self.spec.kind == "float":
            tol = 10 ** -(self.spec.decimals + 1) * max(1.0, self.factor)
            return abs(float(v) - float(d)) <= tol
        return v == d

    def reset(self) -> None:
        if self.locked:
            return
        self.set_value(self.spec.default)
        self.form._emit_changed()

    def warning(self) -> str:
        if self.locked and self.spec.key == "cut_z":
            from brokcam.cam.tools import VBIT_DEEP_WARNING, VBIT_MAX_DEPTH
            z = abs(float(self.value()))
            if z >= VBIT_MAX_DEPTH - 1e-9:
                return (f"A largura do sulco pedida exigiria mais de {VBIT_MAX_DEPTH:g} mm de "
                        "profundidade; o Z foi limitado e o sulco ficará mais estreito. Reduza a "
                        "largura ou use uma fresa de ângulo maior.")
            if z > VBIT_DEEP_WARNING:
                return (f"Profundidade calculada de {z:.3f} mm é muito funda para isolar cobre "
                        "(típico: 0,05–0,20 mm). Reduza a largura do sulco ou use uma fresa de "
                        "ângulo maior.")
        return _sanity_warning(self.spec, self.value())

    def _update_state(self) -> None:
        modified = not self.is_default() and not self.locked
        self.reset_btn.setVisible(modified)
        warn = self.warning()
        if self.editor.property("modified") != modified:
            theme.set_property(self.editor, "modified", modified)
        if bool(self.editor.property("invalid")) != bool(warn):
            theme.set_property(self.editor, "invalid", bool(warn))
        lock = (f"\n🔒 {self.lock_note()}" if self.locked else "")
        self.editor.setToolTip(self.spec.tooltip + (f"\n⚠ {warn}" if warn else "") + lock)

    def _changed(self, *_a: Any) -> None:
        self._update_state()
        self.form._emit_changed()
        if self.form.help_panel is not None and self.form._current is self:
            self.form.help_panel.show_spec(self.spec, self._help_note())

    def set_visible(self, on: bool) -> None:
        for w in (self.label, self.editor):
            w.setVisible(on)
        sp = self.reset_btn.sizePolicy()
        sp.setRetainSizeWhenHidden(on)      # linhas ocultas não deixam espaço vazio
        self.reset_btn.setSizePolicy(sp)
        self.reset_btn.setVisible(on and not self.is_default() and not self.locked)


class ParamForm(QWidget):
    """Formulário completo para um ``ParamGroup``."""

    valuesChanged = Signal(dict)
    presetApplied = Signal(dict)       # dados brutos do preset carregado (inclui extras)

    def __init__(self, group: ParamGroup, values: dict[str, Any] | None = None, *,
                 show_header: bool = True, show_presets: bool = True, show_help: bool = True,
                 display_units: str | None = None, help_panel: HelpPanel | None = None,
                 parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.group = group
        self.display_units = display_units or units.display_units()
        self.rows: dict[str, FieldRow] = {}
        self._current: FieldRow | None = None
        self._show_advanced = False
        self._suspend = False
        self.setAccessibleName(group.title)
        self.setAccessibleDescription(group.description)

        root = QVBoxLayout(self)
        root.setContentsMargins(0, 0, 0, 0)
        root.setSpacing(8)

        if show_header:
            title = QLabel(group.title)
            title.setObjectName("Heading")
            title.setWordWrap(True)
            root.addWidget(title)
            if group.description:
                desc = QLabel(group.description)
                desc.setObjectName("Muted")
                desc.setWordWrap(True)
                root.addWidget(desc)

        self.preset_combo: QComboBox | None = None
        # Função opcional que devolve dados extras gravados junto com o preset
        # (ex.: a ferramenta da biblioteca escolhida no painel da operação).
        self.preset_extra: Callable[[], dict[str, Any]] | None = None
        if show_presets:
            root.addLayout(self._build_presets())

        grid_host = QWidget()
        self.grid = QGridLayout(grid_host)
        self.grid.setContentsMargins(0, 4, 0, 0)
        self.grid.setHorizontalSpacing(8)
        self.grid.setVerticalSpacing(8)
        self.grid.setColumnStretch(0, 2)
        self.grid.setColumnStretch(1, 3)
        root.addWidget(grid_host)

        r = 0
        for spec in group.params:
            row = FieldRow(self, spec)
            self.rows[spec.key] = row
            if isinstance(row.editor, QPlainTextEdit):
                self.grid.addWidget(row.label, r, 0, 1, 2)
                self.grid.addWidget(row.reset_btn, r, 2)
                r += 1
                self.grid.addWidget(row.editor, r, 0, 1, 3)
            else:
                self.grid.addWidget(row.label, r, 0)
                self.grid.addWidget(row.editor, r, 1)
                self.grid.addWidget(row.reset_btn, r, 2)
            r += 1

        n_adv = sum(1 for s in group.params if s.advanced)
        self.adv_btn: QPushButton | None = None
        if n_adv:
            self.adv_btn = QPushButton()
            self.adv_btn.setProperty("link", True)
            self.adv_btn.setCheckable(False)
            self.adv_btn.clicked.connect(lambda: self.set_show_advanced(not self._show_advanced))
            self.adv_btn.setAccessibleDescription("Mostra ou oculta as opções avançadas deste formulário.")
            root.addWidget(self.adv_btn, 0, Qt.AlignmentFlag.AlignLeft)
            self._n_adv = n_adv

        self.help_panel = help_panel
        if show_help and help_panel is None:
            self.help_panel = HelpPanel()
            root.addWidget(self.help_panel)
        if self.help_panel is not None:
            self.help_panel.set_default(group.title, group.description)

        self.set_values(values or group.defaults())
        self._suspend = True        # montagem inicial: não conta como valor "manual" do usuário
        try:
            self.set_show_advanced(False)
        finally:
            self._suspend = False
        self._fix_tab_order()

    # ------------------------------------------------------------------ presets
    def _build_presets(self) -> QHBoxLayout:
        lay = QHBoxLayout()
        lay.setSpacing(6)
        lab = QLabel("Preset")
        lab.setObjectName("Muted")
        combo = Combo()
        combo.setAccessibleName("Preset de parâmetros")
        combo.setToolTip("Carrega um conjunto de valores salvo anteriormente para esta operação.")
        combo.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Fixed)
        lab.setBuddy(combo)
        save = QToolButton()
        save.setIcon(icon("bookmark"))
        save.setToolTip("Salvar os valores atuais como preset")
        save.setAccessibleName("Salvar preset")
        save.clicked.connect(self._save_preset)
        delete = QToolButton()
        delete.setIcon(icon("trash"))
        delete.setToolTip("Excluir o preset selecionado")
        delete.setAccessibleName("Excluir preset")
        delete.clicked.connect(self._delete_preset)
        lay.addWidget(lab)
        lay.addWidget(combo, 1)
        lay.addWidget(save)
        lay.addWidget(delete)
        self.preset_combo = combo
        self._reload_presets()
        combo.activated.connect(self._apply_preset)
        return lay

    def _reload_presets(self, select: str | None = None) -> None:
        c = self.preset_combo
        if c is None:
            return
        c.blockSignals(True)
        c.clear()
        c.addItem("Padrões de fábrica", None)
        for name in app_settings.list_presets(self.group.key):
            c.addItem(name, name)
        if select:
            c.setCurrentIndex(max(0, c.findData(select)))
        c.blockSignals(False)

    def _apply_preset(self, idx: int) -> None:
        assert self.preset_combo is not None
        name = self.preset_combo.itemData(idx)
        raw: dict[str, Any] = {}
        if name is None:
            self.set_values(self.group.defaults())
        else:
            try:
                raw = app_settings.load_preset(self.group.key, name)
                vals = {**self.group.defaults(), **raw}
                if self.group.derive is not None:
                    try:
                        vals = self.group.validate(raw)   # derivação vê o preset como foi salvo
                    except (ValueError, TypeError):
                        pass
                self.set_values(vals)
            except Exception as exc:
                QMessageBox.warning(self, "Preset", f"Não foi possível carregar o preset:\n{exc}")
                return
        self.presetApplied.emit(raw)
        self._emit_changed()

    def _save_preset(self) -> None:
        name, ok = QInputDialog.getText(self, "Salvar preset",
                                        f"Nome do preset para “{self.group.title}”:")
        if not ok or not name.strip():
            return
        data = dict(self.values())
        if self.preset_extra is not None:
            data.update(self.preset_extra())
        app_settings.save_preset(self.group.key, name.strip(), data)
        self._reload_presets(select=name.strip())

    def _delete_preset(self) -> None:
        c = self.preset_combo
        if c is None or c.currentData() is None:
            return
        name = c.currentData()
        if QMessageBox.question(self, "Excluir preset", f"Excluir o preset “{name}”?") \
                == QMessageBox.StandardButton.Yes:
            app_settings.delete_preset(self.group.key, name)
            self._reload_presets()

    # ------------------------------------------------------------------ API
    def values(self) -> dict[str, Any]:
        raw = {k: r.value() for k, r in self.rows.items()}
        try:
            return self.group.validate(raw)
        except (ValueError, TypeError):
            return raw

    def set_values(self, values: dict[str, Any]) -> None:
        if self.group.derive is not None:
            # completa valores derivados (ex.: largura da V a partir de um Z antigo)
            try:
                values = {**values, **self.group.validate(values)}
            except (ValueError, TypeError):
                pass
        self._suspend = True
        try:
            for k, row in self.rows.items():
                row.set_value(values.get(k, row.spec.default))
            # ainda suspenso: valores postos pelo programa (Z já derivado da V, preset,
            # ferramenta da biblioteca) não contam como "Z manual" do usuário
            self._apply_visibility()
        finally:
            self._suspend = False

    def _apply_visibility(self) -> None:
        """Mostra só os campos relevantes: avançados conforme o botão e ``visible_if`` conforme os valores."""
        raw = {k: r.value() for k, r in self.rows.items()}
        self._apply_locks(raw)
        n_adv = 0
        for row in self.rows.values():
            relevant = row.spec.is_relevant(raw)
            if row.spec.advanced and relevant:
                n_adv += 1
            row.set_visible(relevant and (self._show_advanced or not row.spec.advanced))
        self._n_adv = n_adv
        if self.adv_btn is not None:
            self.adv_btn.setVisible(n_adv > 0)
            self.adv_btn.setText(("Ocultar opções avançadas" if self._show_advanced else
                                  f"Mostrar opções avançadas ({n_adv})"))

    def _apply_locks(self, raw: dict[str, Any]) -> None:
        """Trava os campos calculados (``locked_if``) e mostra neles o valor derivado."""
        derived: dict[str, Any] | None = None
        if self.group.derive is not None:
            try:
                derived = self.group.validate(raw)
            except (ValueError, TypeError):
                derived = None
        for key, row in self.rows.items():
            lock = row.spec.is_locked(raw)
            if lock and not row.locked and not self._suspend:
                row.manual_value = row.value()      # lembra o valor digitado antes de travar
            if not lock and row.locked and not self._suspend:
                # ao destravar (ex.: V → fresa de topo) não herda o Z calculado da V:
                # volta ao último valor manual ou ao padrão da operação
                row.set_value(row.manual_value if row.manual_value is not None else row.spec.default)
                raw[key] = row.value()
            if lock and derived is not None and key in derived:
                row.set_value(derived[key])
                raw[key] = row.value()
            row.set_locked(lock)

    def is_relevant(self, key: str) -> bool:
        """True se o campo ``key`` está ativo (visível pelas condições) com os valores atuais."""
        raw = {k: r.value() for k, r in self.rows.items()}
        return self.rows[key].spec.is_relevant(raw)

    def reset_all(self) -> None:
        self.set_values(self.group.defaults())
        self._emit_changed()

    def warnings(self) -> dict[str, str]:
        return {k: w for k, r in self.rows.items() if (w := r.warning())}

    def set_show_advanced(self, on: bool) -> None:
        self._show_advanced = on
        self._apply_visibility()

    def editor(self, key: str) -> QWidget:
        return self.rows[key].editor

    def font_scale(self) -> float:
        return theme.manager().font_scale

    # ------------------------------------------------------------------ internos
    def _emit_changed(self) -> None:
        if not self._suspend:
            self._apply_visibility()
            self.valuesChanged.emit(self.values())

    def _show_help(self, row: FieldRow) -> None:
        self._current = row
        if self.help_panel is not None:
            self.help_panel.show_spec(row.spec, row._help_note())

    def _hover_left(self) -> None:
        # Mantém a ajuda do campo com foco (se houver) ao tirar o mouse.
        from PySide6.QtWidgets import QApplication
        fw = QApplication.focusWidget()
        for row in self.rows.values():
            if fw is not None and (fw is row.editor or row.editor.isAncestorOf(fw)):
                self._show_help(row)
                return

    def _fix_tab_order(self) -> None:
        prev: QWidget | None = None
        for row in self.rows.values():
            if prev is not None:
                QWidget.setTabOrder(prev, row.editor)
            prev = row.editor
