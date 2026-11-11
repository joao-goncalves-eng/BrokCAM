"""Preferências persistentes do BrokCAM (JSON em ``%APPDATA%/BrokCAM``).

* ``settings.json`` — valores de todos os ``ParamGroup`` (operações, pós-processador,
  interface), arquivos recentes e estado da janela.
* ``presets/<grupo>/<nome>.json`` — presets salvos por operação.

A variável de ambiente ``BROKCAM_CONFIG_DIR`` muda a pasta (usada nos testes).
"""
from __future__ import annotations

import json
import logging
import os
import re
from pathlib import Path
from typing import Any

from brokcam.core.params import ParamGroup, ParamSpec, register, set_conditions

log = logging.getLogger("brokcam.ui.settings")

MAX_RECENT = 10


def config_dir() -> Path:
    env = os.environ.get("BROKCAM_CONFIG_DIR") or os.environ.get("TRACECAM_CONFIG_DIR")
    if env:
        base = Path(env)
    else:
        root = (Path(os.environ.get("APPDATA", Path.home() / "AppData" / "Roaming")) if os.name == "nt"
                else Path(os.environ.get("XDG_CONFIG_HOME", Path.home() / ".config")))
        base = root / "BrokCAM"
        legacy = root / "TraceCAM"           # nome antigo do programa: migra preferências, biblioteca e presets
        if not base.exists() and legacy.is_dir():
            try:
                import shutil
                shutil.copytree(legacy, base)
                log.info("Configurações migradas de %s para %s", legacy, base)
            except OSError as exc:
                log.warning("Não foi possível migrar as configurações antigas: %s", exc)
    base.mkdir(parents=True, exist_ok=True)
    return base


# --------------------------------------------------------------------------- grupos da interface

UI_GROUP = register(ParamGroup(
    key="ui",
    title="Interface",
    description=(
        "Aparência e comportamento da janela do BrokCAM. Estas opções não afetam o "
        "G-code gerado — apenas como as informações são exibidas."),
    params=(
        ParamSpec("theme", "Tema", "choice", "system",
                  "Esquema de cores da interface, no estilo dos programas de CAD (Fusion 360, "
                  "Inventor). 'Seguir o sistema' acompanha o modo claro/escuro do Windows. Escuro e "
                  "Claro são cinzas neutros; Grafite é um cinza médio que cansa menos a vista em "
                  "sessões longas; Azul-aço lembra o Inventor clássico; Meia-noite é um escuro "
                  "azulado. Todos têm contraste mínimo AA (4,5:1) em todo texto.",
                  choices=(("system", "Seguir o sistema"), ("dark", "Escuro"), ("light", "Claro"),
                           ("graphite", "Grafite"), ("steel", "Azul-aço"), ("midnight", "Meia-noite"))),
        ParamSpec("accent", "Cor de destaque", "choice", "theme",
                  "Cor dos botões principais, da seleção, do foco e dos links. 'Padrão do tema' usa o "
                  "azul de cada tema. Qualquer cor escolhida é ajustada automaticamente (mais clara ou "
                  "mais escura) para o texto continuar legível. Também disponível em Exibir › "
                  "Aparência e no botão de aparência da faixa de comandos.",
                  choices=(("theme", "Padrão do tema"), ("blue", "Azul (Autodesk)"), ("royal", "Azul royal"),
                           ("cyan", "Ciano"), ("green", "Verde"), ("orange", "Laranja"), ("red", "Vermelho"),
                           ("pink", "Rosa"), ("purple", "Roxo"), ("amber", "Âmbar"), ("slate", "Grafite"),
                           ("custom", "Personalizada"))),
        ParamSpec("accent_custom", "Cor personalizada", "str", "#0696d7",
                  "Cor de destaque no formato #RRGGBB (ex.: #0696d7), usada quando a cor de destaque é "
                  "'Personalizada'. Pode ser a cor da sua marca ou da sua máquina."),
        ParamSpec("canvas_style", "Fundo da área de trabalho", "choice", "gradient",
                  "Degradê: fundo com transição suave de cor, como nos programas de CAD — ajuda a "
                  "separar a placa do resto da interface. Sólido: uma cor só (melhor para capturas de "
                  "tela e impressão).",
                  choices=(("gradient", "Degradê (estilo CAD)"), ("solid", "Sólido"))),
        ParamSpec("corners", "Cantos", "choice", "soft",
                  "Arredondamento de botões, campos e painéis. Suaves é o visual dos programas de CAD; "
                  "Arredondados é mais moderno; Retos é o mais sóbrio e compacto.",
                  choices=(("soft", "Suaves"), ("round", "Arredondados"), ("square", "Retos"))),
        ParamSpec("density", "Densidade", "choice", "comfortable",
                  "Compacta reduz o espaçamento interno de botões, campos, listas e menus para caber "
                  "mais informação na tela (bom em notebooks). Confortável facilita o clique.",
                  choices=(("comfortable", "Confortável"), ("compact", "Compacta"))),
        ParamSpec("colored_icons", "Ícones coloridos", "bool", True,
                  "Mostra os ícones dos comandos em cores (isolar em cobre, furar em roxo, simular em "
                  "verde…), como no Fusion 360, o que ajuda a encontrar cada comando de relance. "
                  "Desligado, os ícones ficam na cor do texto. No alto contraste são sempre "
                  "monocromáticos."),
        ParamSpec("high_contrast", "Alto contraste", "bool", False,
                  "Ativa um tema preto com texto branco, bordas fortes e destaque amarelo, "
                  "pensado para baixa visão ou telas sob luz intensa. Tem prioridade sobre o "
                  "tema escolhido acima."),
        ParamSpec("font_scale", "Escala da fonte", "int", 100,
                  "Aumenta todo o texto da interface (100% = tamanho padrão). Útil em telas "
                  "pequenas de alta resolução ou para leitura confortável à distância da "
                  "máquina. Valores entre 100% e 200%.",
                  unit="%", minimum=100, maximum=200, step=10, decimals=0),
        ParamSpec("units", "Unidades de exibição", "choice", "mm",
                  "Unidade usada para mostrar coordenadas, medidas e campos de comprimento. "
                  "Internamente o BrokCAM sempre trabalha em milímetros; a escolha aqui só "
                  "converte os valores exibidos e digitados.",
                  choices=(("mm", "Milímetros (mm)"), ("in", "Polegadas (pol)"))),
        ParamSpec("opengl", "Aceleração OpenGL no canvas", "bool", True,
                  "Desenha o canvas com a placa de vídeo (OpenGL), o que deixa zoom e "
                  "deslocamento mais fluidos em placas grandes. Desative se notar falhas de "
                  "desenho, tela preta ou se estiver usando acesso remoto. Requer reiniciar "
                  "a visualização (aplicado ao fechar as Preferências)."),
        ParamSpec("antialias", "Suavização (antialiasing)", "bool", True,
                  "Suaviza as bordas das camadas no canvas. Desligar pode deixar o desenho "
                  "mais rápido em computadores lentos, com aparência serrilhada."),
        ParamSpec("circle_segments", "Resolução de círculos", "int", 32,
                  "Número de segmentos usados para desenhar furos e outros círculos no "
                  "canvas. Mais segmentos deixam o desenho mais redondo, mas usam mais "
                  "memória. Não altera o G-code: furos são executados pelo centro.",
                  minimum=8, maximum=128, step=4, decimals=0, advanced=True),
        ParamSpec("show_grid", "Mostrar grade", "bool", True,
                  "Exibe a grade de referência no canvas. O espaçamento se adapta ao zoom "
                  "(1-2-5) para nunca ficar poluído."),
        ParamSpec("show_rulers", "Mostrar réguas", "bool", True,
                  "Exibe réguas graduadas nas bordas superior e esquerda do canvas, na "
                  "unidade de exibição escolhida."),
        ParamSpec("show_rapids", "Mostrar deslocamentos rápidos", "bool", True,
                  "Mostra os movimentos rápidos (G0, sem cortar) como linhas tracejadas. "
                  "Ajuda a verificar a ordem dos cortes; desligue para ver só os cortes."),
        ParamSpec("rapid_rate", "Velocidade de rápido (estimativa)", "float", 1000.0,
                  "Velocidade usada apenas para estimar o tempo de usinagem dos "
                  "deslocamentos G0. Use o valor de $110/$111 da sua máquina para "
                  "estimativas mais precisas.",
                  unit="mm/min", minimum=10, maximum=50000, step=100, decimals=0, advanced=True),
        ParamSpec("confirm_send", "Confirmar antes de enviar programa", "bool", True,
                  "Pede confirmação (com um resumo do programa) antes de iniciar o envio "
                  "de G-code para a máquina. Recomendado: evita iniciar o programa errado "
                  "ou com o zero de trabalho não definido."),
    ),
))
set_conditions(UI_GROUP, {"accent_custom": (("accent", ("custom",)),)})


def dynamic_spec(group: ParamGroup, key: str) -> ParamSpec | None:
    try:
        return group.spec(key)
    except KeyError:
        return None


# --------------------------------------------------------------------------- persistência

class AppSettings:
    """Armazena valores de ParamGroups e estado auxiliar num JSON."""

    def __init__(self, path: Path | None = None) -> None:
        self.path = path or (config_dir() / "settings.json")
        self.data: dict[str, Any] = {"groups": {}, "recent": [], "window": {}}
        self.load()

    # ------------------------------------------------------------------ arquivo
    def load(self) -> None:
        try:
            if self.path.exists():
                raw = json.loads(self.path.read_text(encoding="utf-8"))
                if isinstance(raw, dict):
                    self.data.update(raw)
        except Exception:
            log.exception("Não foi possível ler %s; usando padrões", self.path)

    def save(self) -> None:
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            tmp = self.path.with_suffix(".tmp")
            tmp.write_text(json.dumps(self.data, indent=2, ensure_ascii=False), encoding="utf-8")
            tmp.replace(self.path)
        except Exception:
            log.exception("Não foi possível salvar %s", self.path)

    # ------------------------------------------------------------------ grupos
    def group_values(self, group: ParamGroup) -> dict[str, Any]:
        """Valores salvos do grupo, validados e completados com os padrões."""
        stored = self.data.setdefault("groups", {}).get(group.key, {})
        out = group.defaults()
        for p in group.params:
            if p.key in stored:
                try:
                    out[p.key] = p.validate(stored[p.key])
                except (ValueError, TypeError):
                    pass
        if group.derive is not None:
            # deixa a regra de derivação ver os dados como foram salvos (ex.: Z antigo da
            # fresa V sem ``vbit_width``) em vez de já completados com os padrões
            keys = {p.key for p in group.params}
            try:
                out = group.validate({k: v for k, v in stored.items() if k in keys})
            except (ValueError, TypeError):
                pass
        return out

    def set_group_values(self, group: ParamGroup, values: dict[str, Any]) -> None:
        self.data.setdefault("groups", {})[group.key] = dict(values)

    def ui(self, key: str) -> Any:
        return self.group_values(UI_GROUP)[key]

    def set_ui(self, key: str, value: Any) -> None:
        vals = self.group_values(UI_GROUP)
        vals[key] = UI_GROUP.spec(key).validate(value)
        self.set_group_values(UI_GROUP, vals)

    # ------------------------------------------------------------------ recentes
    @property
    def recent(self) -> list[str]:
        return [p for p in self.data.get("recent", []) if isinstance(p, str)]

    def add_recent(self, path: str) -> None:
        path = os.path.abspath(path)
        rec = [p for p in self.recent if os.path.normcase(p) != os.path.normcase(path)]
        rec.insert(0, path)
        self.data["recent"] = rec[:MAX_RECENT]

    def clear_recent(self) -> None:
        self.data["recent"] = []

    # ------------------------------------------------------------------ genéricos
    def get(self, key: str, default: Any = None) -> Any:
        return self.data.get(key, default)

    def set(self, key: str, value: Any) -> None:
        self.data[key] = value


# --------------------------------------------------------------------------- presets

def presets_dir(group_key: str) -> Path:
    d = config_dir() / "presets" / _safe(group_key)
    d.mkdir(parents=True, exist_ok=True)
    return d


def _safe(name: str) -> str:
    s = re.sub(r"[^\w\-. ]+", "_", name, flags=re.UNICODE).strip(" .")
    return s or "preset"


def list_presets(group_key: str) -> list[str]:
    return sorted(p.stem for p in presets_dir(group_key).glob("*.json"))


def save_preset(group_key: str, name: str, values: dict[str, Any]) -> Path:
    path = presets_dir(group_key) / f"{_safe(name)}.json"
    path.write_text(json.dumps({"name": name, "group": group_key, "values": values},
                               indent=2, ensure_ascii=False), encoding="utf-8")
    return path


def load_preset(group_key: str, name: str) -> dict[str, Any]:
    path = presets_dir(group_key) / f"{_safe(name)}.json"
    raw = json.loads(path.read_text(encoding="utf-8"))
    vals = raw.get("values", raw)
    return dict(vals) if isinstance(vals, dict) else {}


def delete_preset(group_key: str, name: str) -> None:
    path = presets_dir(group_key) / f"{_safe(name)}.json"
    if path.exists():
        path.unlink()
