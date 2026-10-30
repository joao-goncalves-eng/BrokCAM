"""Sistema de parâmetros autoexplicativos.

Toda opção configurável do BrokCAM é declarada como um ``ParamSpec``. A interface
gera os formulários automaticamente a partir destas especificações, exibindo o
rótulo, a unidade, o tooltip curto e a explicação completa (painel de ajuda).
Assim nenhuma opção existe sem explicação.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Callable, Sequence


@dataclass(frozen=True)
class ParamSpec:
    key: str                      # identificador único dentro do grupo (snake_case)
    label: str                    # rótulo curto exibido no formulário (PT-BR)
    kind: str                     # "float" | "int" | "bool" | "choice" | "str"
    default: Any
    help: str                     # explicação completa: o que faz, quando mudar, efeitos colaterais
    unit: str = ""                # "mm", "mm/min", "RPM", "s", "%", "" ...
    minimum: float | None = None
    maximum: float | None = None
    step: float | None = None
    decimals: int = 3
    choices: Sequence[tuple[str, str]] = ()   # (valor, rótulo) quando kind == "choice"
    advanced: bool = False        # escondido no modo "básico" da interface
    # Condições de visibilidade: o campo só aparece se, para cada (chave, valores),
    # o valor atual de ``chave`` estiver em ``valores``. Ex.: (("tool_shape", ("vbit",)),)
    visible_if: tuple[tuple[str, tuple[Any, ...]], ...] = ()
    # Condições em que o valor é CALCULADO pelo programa: o campo continua visível, mas
    # fica travado (somente leitura). Mesmo formato de ``visible_if``; todas precisam valer
    # e a chave precisa estar presente. ``locked_note`` explica ao usuário o porquê.
    locked_if: tuple[tuple[str, tuple[Any, ...]], ...] = ()
    locked_note: str = ""

    def is_locked(self, values: dict[str, Any]) -> bool:
        """True se o campo é calculado automaticamente com os ``values`` atuais (``locked_if``)."""
        if not self.locked_if:
            return False
        return all(key in values and values[key] in allowed for key, allowed in self.locked_if)

    def is_relevant(self, values: dict[str, Any]) -> bool:
        """True se o campo faz sentido com os ``values`` atuais (condições ``visible_if``)."""
        for key, allowed in self.visible_if:
            if key in values and values[key] not in allowed:
                return False
        return True

    @property
    def tooltip(self) -> str:
        """Primeira frase da ajuda — usada como tooltip curto."""
        first = self.help.strip().split(". ")[0].rstrip(".")
        return first + "."

    def validate(self, value: Any) -> Any:
        """Converte e limita ``value`` ao tipo/faixa declarados."""
        if self.kind == "float":
            value = float(value)
        elif self.kind == "int":
            value = int(value)
        elif self.kind == "bool":
            value = bool(value)
        elif self.kind == "choice":
            allowed = [c[0] for c in self.choices]
            if value not in allowed:
                raise ValueError(f"{self.key}: valor {value!r} não está em {allowed}")
            return value
        else:
            return str(value)
        if self.minimum is not None and value < self.minimum:
            value = type(value)(self.minimum)
        if self.maximum is not None and value > self.maximum:
            value = type(value)(self.maximum)
        return value


@dataclass(frozen=True)
class ParamGroup:
    key: str                      # ex.: "isolation", "drilling", "grbl_machine"
    title: str                    # título da seção na interface
    description: str              # parágrafo explicando a operação como um todo
    params: tuple[ParamSpec, ...] = field(default_factory=tuple)
    # Regra opcional de valores derivados: ``derive(valores_validados, valores_brutos)`` devolve
    # o dicionário final (ex.: Z de corte calculado da fresa em V). Aplicada em ``validate``,
    # então vale para a interface, para as funções de CAM e para arquivos antigos.
    derive: Callable[[dict[str, Any], dict[str, Any]], dict[str, Any]] | None = field(
        default=None, compare=False, repr=False)

    def defaults(self) -> dict[str, Any]:
        return {p.key: p.default for p in self.params}

    def spec(self, key: str) -> ParamSpec:
        for p in self.params:
            if p.key == key:
                return p
        raise KeyError(key)

    def validate(self, values: dict[str, Any]) -> dict[str, Any]:
        out = self.defaults()
        for p in self.params:
            if p.key in values:
                out[p.key] = p.validate(values[p.key])
        if self.derive is not None:
            out = self.derive(out, values)
        return out


def set_conditions(group: ParamGroup, conditions: dict[str, tuple[tuple[str, tuple[Any, ...]], ...]]) -> ParamGroup:
    """Acrescenta condições de visibilidade a campos de um grupo já criado (no lugar)."""
    from dataclasses import replace
    new = tuple(replace(p, visible_if=p.visible_if + tuple(conditions[p.key])) if p.key in conditions else p
                for p in group.params)
    object.__setattr__(group, "params", new)
    return group


def set_locked(group: ParamGroup, conditions: dict[str, tuple[tuple[str, tuple[Any, ...]], ...]],
               note: str = "") -> ParamGroup:
    """Marca campos de um grupo como calculados (travados) sob ``conditions`` (no lugar)."""
    from dataclasses import replace
    new = tuple(replace(p, locked_if=p.locked_if + tuple(conditions[p.key]), locked_note=note or p.locked_note)
                if p.key in conditions else p for p in group.params)
    object.__setattr__(group, "params", new)
    return group


def set_derive(group: ParamGroup,
               fn: Callable[[dict[str, Any], dict[str, Any]], dict[str, Any]]) -> ParamGroup:
    """Acrescenta uma regra de valores derivados ao grupo (encadeia com a existente)."""
    old = group.derive
    if old is None:
        object.__setattr__(group, "derive", fn)
    else:
        object.__setattr__(group, "derive", lambda v, raw: fn(old(v, raw), raw))
    return group


_REGISTRY: dict[str, ParamGroup] = {}


def register(group: ParamGroup) -> ParamGroup:
    """Registra um grupo para que a interface (ex.: Preferências) o encontre."""
    _REGISTRY[group.key] = group
    return group


def get_group(key: str) -> ParamGroup:
    return _REGISTRY[key]


def all_groups() -> list[ParamGroup]:
    return list(_REGISTRY.values())
