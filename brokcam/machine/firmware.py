"""Identificação do firmware e catálogo de configurações (GRBL 1.1, Grbl_Esp32, MKS DLC32).

Módulo puro (sem Qt). Reúne:

* :func:`detect_firmware` — identifica o firmware a partir do banner e da saída de
  ``$I`` (``"grbl"`` clássico, ``"grbl_esp32"``, ``"mks_dlc32"``, ``"fluidnc"``,
  ``"grblhal"`` ou ``"desconhecido"``);
* parsers das respostas ``$I``, ``$S``/``$+``/``$SC``, ``$#``, ``$G`` e da lista de
  redes WiFi (``$WiFi/ListAPs``/``[ESP410]``);
* :data:`NUMBERED` e :data:`EXTENDED` — catálogo de configurações com nome, tipo,
  unidade, categoria e explicação didática em PT-BR (o que faz, valor típico, risco);
* :func:`validate_value` — valida/normaliza um valor digitado para uma configuração;
* :data:`DLC32_PROFILE` — perfil de referência "MKS DLC32 típica", usado SOMENTE
  como comparação (nunca é aplicado sem confirmação do usuário).

Fontes (código-fonte oficial, out/2026):
``makerbase-mks/MKS-DLC32-FIRMWARE`` (``Firmware/Grbl_Esp32/src``: ``Grbl.h``,
``Report.cpp``, ``SettingsDefinitions.cpp``, ``ProcessSettings.cpp``,
``WebUI/WebSettings.cpp``, ``WebUI/WifiConfig.*``, ``Machines/i2s_out_xyz_mks_dlc32.h``)
e ``bdring/Grbl_Esp32`` (base 1.3a).

Fatos relevantes extraídos do código:

* Banner da DLC32: ``Grbl 1.1 ['$' for help]`` (a MKS mudou ``GRBL_VERSION`` de
  "1.3a" para "1.1"); ``$I`` responde ``[VER:1.1.2022042101:]``, ``[OPT:...]``,
  ``[MSG:Using machine:MKS DLC32]`` e ``[MSG:Mode=AP:SSDI=MKS_DLC:IP=192.168.4.1:MAC=..]``
  (o "SSDI" com erro de digitação é do próprio firmware).
* Grbl_Esp32 original: ``Grbl 1.3a ['$' for help]`` e ``[VER:1.3a.20211103:]``.
* ``$S`` lista todas as configurações pelo nome (``$Sta/SSID=...``); senhas aparecem
  como ``******``. ``$+`` lista as numeradas + estendidas que têm número.
  ``$SC`` lista só as alteradas (com ``(Default=...)``).
* WiFi padrão da DLC32: modo AP, SSID ``MKS_DLC``, senha ``12345678``, IP
  ``192.168.4.1``; Telnet na porta 23 (envia o banner ao conectar), HTTP na 80.
* Reinício da placa: ``$System/Control=RESTART`` (``[ESP444]RESTART``). ``$Bye`` é do
  FluidNC, não existe no Grbl_Esp32/DLC32.
* ``$RST=$`` (configurações), ``$RST=#`` (coordenadas), ``$RST=*`` (tudo) e
  ``$RST=@`` / ``$RST=wifi`` (só WiFi).
"""
from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from typing import Iterable

__all__ = [
    "FirmwareInfo", "SettingDef", "ParserItem", "ProfileValue", "FIRMWARE_LABELS", "CATEGORIES",
    "NUMBERED", "EXTENDED", "NAME_ALIASES", "DLC32_PROFILE", "DLC32_PROFILE_NAME",
    "detect_firmware", "parse_banner", "parse_build_info", "parse_wifi_message",
    "parse_extended_settings", "parse_numbered_settings", "parse_parameters",
    "parse_parser_state", "describe_parser_state", "describe_pins", "decode_options",
    "parse_ap_list", "setting_def", "categorize", "validate_value", "values_equal",
    "is_hidden_value", "HIDDEN_VALUES", "PIN_LABELS", "AXIS_BITS", "STATUS_BITS",
    "RESTART_COMMAND", "split_tcp_url", "backup_payload", "read_backup", "ESP32_ERRORS",
    "esp32_error_text", "DLC32_PROFILE_DESCRIPTION", "to_number", "numbered_equivalent",
]

FIRMWARE_LABELS: dict[str, str] = {
    "grbl": "GRBL 1.1 (Arduino/AVR)",
    "grbl_esp32": "Grbl_Esp32",
    "mks_dlc32": "MKS DLC32 (Grbl_Esp32)",
    "fluidnc": "FluidNC",
    "grblhal": "grblHAL",
    "desconhecido": "Desconhecido",
}

#: Comando que reinicia a placa ESP32 (necessário para aplicar mudanças de WiFi).
RESTART_COMMAND = {"grbl_esp32": "$System/Control=RESTART", "mks_dlc32": "$System/Control=RESTART",
                   "fluidnc": "$Bye"}

#: Códigos de erro extras do Grbl_Esp32/DLC32 (``Error.h``), além dos do GRBL 1.1.
ESP32_ERRORS: dict[int, str] = {
    39: "Parâmetro P acima do máximo permitido.",
    60: "Cartão SD: falha ao montar.", 61: "Cartão SD: falha ao ler o arquivo.",
    62: "Cartão SD: falha ao abrir a pasta.", 63: "Cartão SD: pasta não encontrada.",
    64: "Cartão SD: arquivo vazio.", 65: "Cartão SD: arquivo não encontrado.",
    66: "Cartão SD: falha ao abrir o arquivo.", 67: "Cartão SD ocupado.",
    68: "Cartão SD: falha ao apagar a pasta.", 69: "Cartão SD: falha ao apagar o arquivo.",
    70: "Falha ao iniciar o Bluetooth.", 71: "Falha ao iniciar o WiFi.",
    80: "Valor numérico fora da faixa permitida para esta configuração.",
    81: "Valor inválido para esta configuração (texto, IP ou opção não aceitos).",
    90: "Falha ao enviar a mensagem/notificação.",
    100: "Falha ao gravar na memória NVS da placa.", 101: "Falha ao ler as estatísticas da NVS.",
    110: "Autenticação necessária (configuração protegida por senha da WebUI).",
    120: "Outra interface (USB/WiFi/tela) está ocupada executando um trabalho.",
    130: "Jog cancelado.",
}


def esp32_error_text(code: int) -> str:
    """Mensagem PT-BR de um ``error:N`` considerando os códigos extras do Grbl_Esp32."""
    if code in ESP32_ERRORS:
        return ESP32_ERRORS[code]
    from brokcam.machine.grbl_codes import error_text
    return error_text(code)


#: Valores que o firmware mostra no lugar de senhas (nunca são gravados de volta).
HIDDEN_VALUES = ("******", "<Authentication required>")


# =========================================================================== firmware

@dataclass
class FirmwareInfo:
    """Firmware identificado na placa."""
    kind: str = "desconhecido"
    version: str = ""                  # "1.1h", "1.1", "1.3a", "3.7.8"
    build: str = ""                    # data de compilação do [VER:...]
    machine: str = ""                  # "MKS DLC32" (de [MSG:Using machine:...])
    options: str = ""                  # letras do [OPT:...]
    planner_blocks: int | None = None  # de [OPT:...,15,128] (GRBL clássico)
    rx_buffer: int | None = None
    build_note: str = ""               # texto livre do [VER:x.y.data:texto]
    wifi: list[dict[str, str]] = field(default_factory=list)
    banner: str = ""
    raw: list[str] = field(default_factory=list)

    @property
    def label(self) -> str:
        return FIRMWARE_LABELS.get(self.kind, self.kind)

    @property
    def is_esp32(self) -> bool:
        return self.kind in ("grbl_esp32", "mks_dlc32", "fluidnc")

    @property
    def supports_extended(self) -> bool:
        """Aceita ``$S``/``$Nome=valor`` (configurações estendidas nomeadas)."""
        return self.kind in ("grbl_esp32", "mks_dlc32")

    @property
    def restart_command(self) -> str:
        return RESTART_COMMAND.get(self.kind, "")

    def summary(self) -> str:
        parts = [self.label]
        if self.version:
            parts.append(f"versão {self.version}")
        if self.build:
            parts.append(f"build {self.build}")
        return " · ".join(parts)

    def to_dict(self) -> dict:
        return {"kind": self.kind, "label": self.label, "version": self.version, "build": self.build,
                "machine": self.machine, "options": self.options, "banner": self.banner}


_BANNER_RE = re.compile(r"^(Grbl|GrblHAL)\s+(\S+)(.*)$", re.IGNORECASE)


def parse_banner(line: str) -> tuple[str, str]:
    """``(tipo provável, versão)`` a partir do banner de boas-vindas."""
    s = (line or "").strip()
    m = _BANNER_RE.match(s)
    if not m:
        return "desconhecido", ""
    head, ver, rest = m.group(1), m.group(2), m.group(3)
    if "fluidnc" in rest.lower():
        fm = re.search(r"FluidNC\s+v?(\S+)", rest, re.IGNORECASE)
        return "fluidnc", (fm.group(1) if fm else ver)
    if head.lower() == "grblhal":
        return "grblhal", ver
    if ver.lower().startswith("1.3"):
        return "grbl_esp32", ver
    if re.fullmatch(r"1\.1[a-z]", ver.lower()):
        return "grbl", ver
    if ver == "1.1":                    # a DLC32 imprime "1.1" sem letra
        return "mks_dlc32", ver
    return "desconhecido", ver


_MSG_RE = re.compile(r"^\[MSG:(.*)\]$")


def parse_wifi_message(text: str) -> dict[str, str] | None:
    """Interpreta ``Mode=AP:SSDI=MKS_DLC:IP=192.168.4.1:MAC=..`` (sem o ``[MSG:``).

    Devolve ``{"mode": "AP", "ssid": ..., "ip": ..., "mac": ..., "status": ...}`` ou
    ``{"mode": "OFF"}`` para "No Wifi". ``None`` se não for uma mensagem de WiFi.
    """
    t = text.strip()
    if t.lower().startswith("no wifi"):
        return {"mode": "OFF"}
    if not t.startswith("Mode="):
        return None
    out: dict[str, str] = {}
    for part in t.split(":"):
        k, _, v = part.partition("=")
        k = k.strip().lower()
        if k == "ssdi":               # erro de digitação do firmware no modo AP
            k = "ssid"
        if k:
            out[k] = v.strip()
    if "mac" in out:
        out["mac"] = out["mac"].replace("-", ":")
    return out


def parse_build_info(lines: Iterable[str]) -> dict:
    """Interpreta a saída de ``$I``.

    Retorna ``{"version", "build", "note", "options", "planner_blocks", "rx_buffer",
    "machine", "wifi": [..]}``.
    """
    out: dict = {"version": "", "build": "", "note": "", "options": "", "planner_blocks": None,
                 "rx_buffer": None, "machine": "", "wifi": []}
    for raw in lines:
        ln = raw.strip()
        if ln.startswith("[VER:"):
            body = ln[5:].rstrip("]")
            ver_build, _, note = body.partition(":")
            m = re.match(r"(.+?)\.(\d{6,})$", ver_build)
            if m:
                out["version"], out["build"] = m.group(1), m.group(2)
            else:
                out["version"] = ver_build
            out["note"] = note
        elif ln.startswith("[OPT:"):
            body = ln[5:].rstrip("]")
            parts = body.split(",")
            out["options"] = parts[0]
            try:
                if len(parts) >= 3:
                    out["planner_blocks"], out["rx_buffer"] = int(parts[1]), int(parts[2])
            except ValueError:
                pass
        else:
            m = _MSG_RE.match(ln)
            if not m:
                continue
            msg = m.group(1).strip()
            low = msg.lower()
            if low.startswith("using machine:"):
                out["machine"] = msg.split(":", 1)[1].strip()
            elif low.startswith("machine:"):
                out["machine"] = msg.split(":", 1)[1].strip()
            else:
                w = parse_wifi_message(msg)
                if w is not None:
                    out["wifi"].append(w)
    return out


def detect_firmware(banner: str = "", build_info: Iterable[str] | None = None) -> FirmwareInfo:
    """Identifica o firmware a partir do banner e (opcionalmente) das linhas de ``$I``.

    Regras (da mais forte para a mais fraca):
    1. "FluidNC" no banner/$I → ``fluidnc``; "GrblHAL" → ``grblhal``;
    2. ``[MSG:Using machine:MKS DLC32...]`` ou "DLC32" em qualquer linha → ``mks_dlc32``;
    3. outra máquina em ``Using machine:`` ou mensagem de WiFi (``Mode=STA/AP``) ou
       versão 1.3x → ``grbl_esp32`` (ou ``mks_dlc32`` se a versão for "1.1" sem letra);
    4. versão ``1.1`` + letra (ex. ``1.1h``) → ``grbl`` clássico.
    """
    lines = [str(x) for x in (build_info or [])]
    kind, version = parse_banner(banner) if banner else ("desconhecido", "")
    bi = parse_build_info(lines)
    info = FirmwareInfo(kind=kind, version=version, banner=(banner or "").strip(), raw=lines)
    if bi["version"]:
        info.version = info.version or bi["version"]
        if not banner:
            info.version = bi["version"]
    info.build = bi["build"]
    info.build_note = bi["note"]
    info.options = bi["options"]
    info.planner_blocks = bi["planner_blocks"]
    info.rx_buffer = bi["rx_buffer"]
    info.machine = bi["machine"]
    info.wifi = bi["wifi"]
    blob = " ".join([banner or ""] + lines).lower()
    if "fluidnc" in blob:
        info.kind = "fluidnc"
    elif "grblhal" in blob:
        info.kind = "grblhal"
    elif "dlc32" in blob or "mks dlc" in blob or "mks_dlc" in info.machine.lower():
        info.kind = "mks_dlc32"
    elif info.machine or info.wifi or info.version.lower().startswith("1.3"):
        info.kind = "mks_dlc32" if info.version == "1.1" else "grbl_esp32"
    elif info.kind == "desconhecido" and re.fullmatch(r"1\.1[a-z]", info.version.lower()):
        info.kind = "grbl"
    elif info.kind == "desconhecido" and info.version == "1.1":
        info.kind = "mks_dlc32"
    return info


_OPT_CLASSIC = {
    "V": "Spindle com velocidade variável (PWM)", "N": "Números de linha", "M": "Refrigeração névoa (M7)",
    "C": "Cinemática CoreXY", "P": "Estacionamento (parking)", "Z": "Homing força a origem em zero",
    "H": "Homing de eixo único ($HX...)", "T": "Duas chaves de limite por eixo",
    "A": "Override de avanço durante a sondagem", "D": "Direção do spindle usada como enable",
    "0": "Spindle desliga com S0", "S": "Debounce de limites por software",
    "R": "Controle do override de estacionamento", "+": "Entrada de porta de segurança",
    "*": "Restaurar tudo ($RST=*) desabilitado", "$": "Restaurar $$ ($RST=$) desabilitado",
    "#": "Restaurar coordenadas ($RST=#) desabilitado", "I": "Gravação do $I desabilitada",
    "E": "Sem sincronização ao gravar EEPROM", "W": "Sem sincronização ao mudar WCO",
    "L": "Trava de homing na inicialização desabilitada", "2": "Eixo duplo (dois motores)",
}
_OPT_ESP32 = {
    "M": "Refrigeração névoa (M7)", "P": "Estacionamento (parking)", "H": "Homing de eixo único ($HX...)",
    "L": "Duas chaves de limite por eixo", "A": "Override de avanço durante a sondagem",
    "B": "Bluetooth", "S": "Cartão SD", "R": "Controle do override de estacionamento", "W": "WiFi",
    "*": "Restaurar tudo ($RST=*) desabilitado", "$": "Restaurar $$ ($RST=$) desabilitado",
    "#": "Restaurar coordenadas ($RST=#) desabilitado", "E": "Sem sincronização ao gravar NVS",
}


def decode_options(options: str, kind: str = "grbl") -> list[tuple[str, str]]:
    """Traduz as letras do ``[OPT:...]`` em ``[(letra, significado), ...]``."""
    table = _OPT_ESP32 if kind in ("grbl_esp32", "mks_dlc32") else _OPT_CLASSIC
    out: list[tuple[str, str]] = []
    seen_w = False
    for ch in options:
        if kind in ("grbl_esp32", "mks_dlc32") and ch == "W":
            out.append(("W", "WiFi" if not seen_w else "Sem sincronização ao mudar WCO"))
            seen_w = True
            continue
        out.append((ch, table.get(ch, "Opção de compilação")))
    return out


# =========================================================================== catálogo

CATEGORIES: tuple[str, ...] = (
    "Geral e relatórios",
    "Motores e passos",
    "Velocidades e acelerações",
    "Curso, limites e sonda",
    "Homing",
    "Spindle / Laser",
    "Rede / WiFi",
    "Sistema",
)

AXIS_BITS: tuple[tuple[int, str], ...] = ((1, "X"), (2, "Y"), (4, "Z"))
STATUS_BITS: tuple[tuple[int, str], ...] = ((1, "MPos"), (2, "Buffer"))


@dataclass(frozen=True)
class SettingDef:
    """Descrição de uma configuração (numerada ``$110`` ou nomeada ``$Sta/SSID``).

    ``kind``: "int", "float", "bool", "mask" (bits por eixo), "enum", "string",
    "password" ou "ip". ``risk``: aviso mostrado antes de gravar (vazio = baixo risco).
    ``restart``: a mudança só vale após reiniciar a placa.
    """
    key: str
    name: str
    kind: str
    category: str
    help: str
    unit: str = ""
    minimum: float | None = None
    maximum: float | None = None
    choices: tuple[str, ...] = ()
    typical: str = ""
    risk: str = ""
    restart: bool = False
    bits: tuple[tuple[int, str], ...] = AXIS_BITS
    firmwares: tuple[str, ...] = ("grbl", "grbl_esp32", "mks_dlc32", "grblhal", "fluidnc", "desconhecido")
    alias: str = ""                    # nome estendido equivalente ($0 <-> Stepper/Pulse)
    max_len: int | None = None
    min_len: int | None = None

    @property
    def label(self) -> str:
        return f"${self.key}"

    @property
    def numbered(self) -> bool:
        return self.key.isdigit()

    @property
    def sensitive(self) -> bool:
        return bool(self.risk) or self.restart

    def full_help(self) -> str:
        parts = [self.help.strip()]
        if self.typical:
            parts.append(f"Valor típico: {self.typical}")
        if self.risk:
            parts.append(f"⚠ Risco: {self.risk}")
        if self.restart:
            parts.append("Só passa a valer depois de reiniciar a placa.")
        return "\n\n".join(parts)


_ESP = ("grbl_esp32", "mks_dlc32")
_DLC = ("mks_dlc32",)


def _n(key: int, name: str, kind: str, cat: str, help_: str, **kw) -> SettingDef:
    return SettingDef(str(key), name, kind, cat, help_, **kw)


_G, _M, _V, _L, _H, _S, _N, _Y = CATEGORIES

_NUMBERED_LIST: list[SettingDef] = [
    _n(0, "Pulso de passo", "int", _M,
       "Duração do pulso STEP enviado aos drivers de motor. Pulsos curtos demais podem não ser "
       "percebidos pelo driver (o motor 'engasga' e perde passos); longos demais limitam a "
       "velocidade máxima.", unit="µs", minimum=3, maximum=1000, alias="Stepper/Pulse",
       typical="10 µs (padrão GRBL e DLC32). Drivers externos lentos podem pedir 5–20 µs.",
       risk="Abaixo do exigido pelo driver o motor perde passos e a peça sai fora de medida."),
    _n(1, "Atraso para desligar motores", "int", _M,
       "Tempo que os motores continuam energizados depois de parar. Com 255 o GRBL nunca "
       "desliga os motores (eles ficam travados).", unit="ms", minimum=0, maximum=255,
       alias="Stepper/IdleTime",
       typical="25 ms (padrão; economiza energia) ou 255 (recomendado para fresar PCB: o eixo Z "
               "não 'cai' nem se move entre operações)."),
    _n(2, "Inverter pulso de passo", "mask", _M,
       "Inverte o sinal STEP de cada eixo marcado. Quase nunca é necessário; só mude se o "
       "manual do driver pedir pulso ativo em nível baixo.", unit="máscara", minimum=0, maximum=7,
       alias="Stepper/StepInvert", typical="0 (nenhum eixo)",
       risk="Com o pulso invertido sem necessidade os motores andam de forma irregular."),
    _n(3, "Inverter direção", "mask", _M,
       "Inverte o sentido de giro de cada eixo marcado. Use quando um eixo anda ao contrário do "
       "esperado (ex.: jog X+ move para a esquerda).", unit="máscara", minimum=0, maximum=7,
       alias="Stepper/DirInvert",
       typical="Depende da fiação. A DLC32 vem de fábrica com 1 (X invertido).",
       risk="Com a direção errada o homing e os limites movem o eixo CONTRA a estrutura."),
    _n(4, "Inverter ENABLE dos drivers", "bool", _M,
       "Inverte o pino que liga/desliga os drivers. Se, ao ligar, os motores ficam soltos "
       "(giram com a mão) quando deveriam estar travados, inverta.", alias="Stepper/EnableInvert",
       typical="0 (desligado) na DLC32 e na maioria das placas.",
       risk="Valor errado deixa os motores sem força durante o trabalho."),
    _n(5, "Inverter fins de curso", "bool", _L,
       "Inverte a leitura das chaves de fim de curso. Se a aba Status mostra um limite "
       "'acionado' sem nada encostado, inverta este valor. (No GRBL 1.1 e no Grbl_Esp32 é um "
       "único liga/desliga para todas as chaves.)", alias="Limits/Invert",
       typical="0 no GRBL clássico (chaves NA com pull-up); 1 é o padrão de fábrica da DLC32.",
       risk="Valor errado gera ALARM:1 imediato com limites físicos ($21) ligados."),
    _n(6, "Inverter sonda", "bool", _L,
       "Inverte a leitura do pino da sonda (probe). Se 'Sonda' aparece acionada sem tocar a "
       "placa, inverta.", alias="Probe/Invert",
       typical="0 no GRBL clássico; 1 é o padrão de fábrica da DLC32.",
       risk="Com a sonda invertida o G38.2 'toca' imediatamente (ALARM:4) ou nunca para — a "
            "fresa pode ser empurrada contra a placa."),
    _n(10, "Relatório de status", "mask", _G,
       "Escolhe o que vai no relatório de status: bit MPos (1) envia posição de máquina em vez "
       "da de trabalho; bit Buffer (2) inclui o uso dos buffers (Bf:). O BrokCAM calcula a outra "
       "posição pelo WCO, então ambos funcionam.", unit="máscara", minimum=0, maximum=3,
       bits=STATUS_BITS, alias="Report/Status", typical="1 (MPos) ou 3 (MPos + buffer)."),
    _n(11, "Desvio de junção", "float", _V,
       "Quanto o planejador pode 'arredondar' a passagem por um canto para não frear até zero. "
       "Maior = cantos mais rápidos e menos precisos; menor = mais lento e mais fiel.", unit="mm",
       minimum=0, maximum=10, alias="GCode/JunctionDeviation", typical="0,010 mm"),
    _n(12, "Tolerância de arco", "float", _G,
       "Erro máximo ao transformar arcos G2/G3 em pequenas retas.", unit="mm", minimum=0, maximum=1,
       alias="GCode/ArcTolerance", typical="0,002 mm"),
    _n(13, "Relatório em polegadas", "bool", _G,
       "Liga para reportar posições em polegadas.", alias="Report/Inches", typical="0 (mm)",
       risk="O BrokCAM trabalha em mm; ligar isto faz o DRO mostrar números 25,4× menores."),
    _n(20, "Limites de software", "bool", _L,
       "Recusa movimentos que sairiam do curso da máquina ($130–$132), com ALARM:2, antes de "
       "mover. Exige homing ($22=1) para o GRBL saber onde está.", alias="Limits/Soft",
       typical="1 em máquinas com homing; 0 sem fins de curso.",
       risk="Ligar sem homing habilitado é recusado (error:10)."),
    _n(21, "Limites físicos", "bool", _L,
       "Para tudo imediatamente (ALARM:1) quando uma chave de fim de curso é acionada.",
       alias="Limits/Hard", typical="1 com chaves bem instaladas e fiação blindada.",
       risk="Ruído elétrico (spindle/fios longos) causa alarmes falsos no meio do trabalho."),
    _n(22, "Homing habilitado", "bool", _H,
       "Habilita o ciclo $H. Com ele ligado a máquina inicia em alarme pedindo homing.",
       alias="Homing/Enable", typical="1 se a máquina tem fins de curso; 0 caso contrário.",
       risk="Sem chaves de fim de curso o $H bate o eixo no fim do curso (ALARM:9)."),
    _n(23, "Direção do homing", "mask", _H,
       "Eixos marcados procuram a chave no sentido negativo; desmarcados, no positivo.",
       unit="máscara", minimum=0, maximum=7, alias="Homing/DirInvert",
       typical="Depende de onde estão as chaves. Padrão DLC32: 1 (X).",
       risk="Direção errada leva o eixo para o lado sem chave até bater."),
    _n(24, "Avanço fino do homing", "float", _H,
       "Velocidade lenta da segunda aproximação à chave (a que define a posição com precisão).",
       unit="mm/min", minimum=0, maximum=10000, alias="Homing/Feed",
       typical="25–100 mm/min (GRBL); a DLC32 vem com 300."),
    _n(25, "Avanço de busca do homing", "float", _H,
       "Velocidade rápida da primeira busca pela chave.", unit="mm/min", minimum=0, maximum=10000,
       alias="Homing/Seek", typical="500–1000 mm/min (DLC32: 1000).",
       risk="Rápido demais: o eixo não consegue frear antes de bater ao encontrar a chave."),
    _n(26, "Debounce do homing", "float", _H,
       "Espera para o contato da chave estabilizar antes de ler.", unit="ms", minimum=0,
       maximum=10000, alias="Homing/Debounce", typical="25–250 ms (DLC32: 250)."),
    _n(27, "Recuo do homing", "float", _H,
       "Quanto o eixo se afasta da chave depois de encontrá-la.", unit="mm", minimum=0,
       maximum=1000, alias="Homing/Pulloff", typical="1–5 mm",
       risk="Pouco recuo deixa a chave acionada: ALARM:8 e limites físicos disparando."),
    _n(28, "Frequência do PWM", "float", _S,
       "Frequência do sinal PWM do spindle/laser (só Grbl_Esp32/DLC32; no GRBL clássico é fixa).",
       unit="Hz", minimum=0, maximum=100000, alias="Spindle/PWM/Frequency", firmwares=_ESP,
       typical="1–5 kHz para laser de diodo; siga o manual do driver do spindle.",
       risk="Frequência fora do que o driver aceita = potência errada ou laser que não liga."),
    _n(30, "Rotação/potência máxima (S máx.)", "float", _S,
       "Valor de S que corresponde a 100% do PWM.", unit="RPM", minimum=0, maximum=100000,
       alias="GCode/MaxS",
       typical="1000 em laser (S1000 = 100%); em spindle, a rotação máxima real (ex.: 10000).",
       risk="Diferente do usado no G-code: o laser/spindle trabalha com potência errada."),
    _n(31, "Rotação/potência mínima (S mín.)", "float", _S,
       "Valor de S que corresponde ao PWM mínimo.", unit="RPM", minimum=0, maximum=100000,
       alias="GCode/MinS", typical="0"),
    _n(32, "Modo laser", "bool", _S,
       "Liga o modo laser: não para em mudanças de S, desliga o feixe nos G0 e, com M4, ajusta a "
       "potência à velocidade.", alias="GCode/LaserMode",
       typical="0 em fresadora (o spindle precisa atingir a rotação antes de cortar); 1 em laser.",
       risk="Ligado com spindle: o G-code segue cortando sem esperar o spindle acelerar."),
    _n(34, "PWM com spindle desligado", "float", _S,
       "Ciclo de trabalho do PWM quando o spindle está desligado (Grbl_Esp32; aparece no $+).",
       unit="%", minimum=0, maximum=100, alias="Spindle/PWM/Off", firmwares=_ESP, typical="0 %"),
    _n(35, "PWM mínimo", "float", _S,
       "Ciclo de trabalho do PWM para S mínimo (Grbl_Esp32; aparece no $+).", unit="%",
       minimum=0, maximum=100, alias="Spindle/PWM/Min", firmwares=_ESP, typical="0 %"),
    _n(36, "PWM máximo", "float", _S,
       "Ciclo de trabalho do PWM para S máximo (Grbl_Esp32; aparece no $+).", unit="%",
       minimum=0, maximum=100, alias="Spindle/PWM/Max", firmwares=_ESP, typical="100 %"),
    _n(38, "Bipe da placa", "bool", _Y,
       "Exclusivo da MKS DLC32: liga/desliga o bipe (buzzer) da placa/tela.",
       alias="beep_status", firmwares=_DLC, typical="1 (ligado, padrão)"),
    _n(40, "Idioma da tela", "int", _Y,
       "Exclusivo da MKS DLC32: idioma da tela touch (TS24/TS35). Aceita 0 a 2; o idioma de cada "
       "número depende da versão do firmware MKS.", minimum=0, maximum=2, alias="Language",
       firmwares=_DLC, typical="1 (padrão de fábrica)"),
]

for _i, _ax in enumerate("XYZ"):
    _NUMBERED_LIST += [
        _n(100 + _i, f"Passos por mm {_ax}", "float", _M,
           f"Calibração do eixo {_ax}: quantos passos do motor movem 1 mm. Fórmula: passos por volta "
           f"× micropassos ÷ avanço por volta (mm). Ex.: 200×16÷8 = 400 (fuso T8) ou 200×16÷40 = 80 "
           f"(correia GT2 de 20 dentes).", unit="passos/mm", minimum=1, maximum=100000,
           alias=f"{_ax}/StepsPerMm",
           typical="80 (correia GT2, padrão da DLC32) · 400/800 (fuso T8 com 1/16 ou 1/32).",
           risk="Valor errado deixa TODAS as medidas proporcionalmente erradas"
                + (" — no Z isso muda a profundidade de corte." if _ax == "Z" else ".")),
        _n(110 + _i, f"Velocidade máxima {_ax}", "float", _V,
           f"Velocidade dos rápidos (G0) e limite de qualquer avanço no eixo {_ax}.",
           unit="mm/min", minimum=1, maximum=100000, alias=f"{_ax}/MaxRate",
           typical=("300–800 mm/min em fresadora com fuso" if _ax == "Z" else
                    "1000–3000 mm/min em fresadora pequena; até 6000 em gravadora laser com correia"),
           risk="Acima do que o motor aguenta ele perde passos e a posição fica errada sem aviso."),
        _n(120 + _i, f"Aceleração {_ax}", "float", _V,
           f"Aceleração do eixo {_ax}. Alta demais = trancos e perda de passos; baixa = lento nos "
           f"caminhos com muitos cantos (isolação de PCB).", unit="mm/s²", minimum=1, maximum=100000,
           alias=f"{_ax}/Acceleration", typical="50–200 em fresadora pequena; 500 é o padrão da DLC32.",
           risk="Alta demais causa perda de passos (deslocamento da peça)."),
        _n(130 + _i, f"Curso máximo {_ax}", "float", _L,
           f"Curso útil do eixo {_ax}. Usado pelos limites de software ($20) e pelo homing.",
           unit="mm", minimum=0, maximum=100000, alias=f"{_ax}/MaxTravel",
           typical="Medida real da máquina (ex.: 300×180×45 numa 3018; DLC32 vem com 450×450×50).",
           risk="Maior que o real com limites de software: a máquina pode bater no fim do curso."),
    ]

NUMBERED: dict[str, SettingDef] = {d.key: d for d in _NUMBERED_LIST}


def _e(name: str, label: str, kind: str, cat: str, help_: str, **kw) -> SettingDef:
    kw.setdefault("firmwares", _ESP)
    return SettingDef(name, label, kind, cat, help_, **kw)


_NET_RISK = ("Pode tirar a placa da rede: se você estiver conectado por WiFi (tcp://) a conexão "
             "cai e talvez seja preciso usar o cabo USB para corrigir.")

_EXTENDED_LIST: list[SettingDef] = [
    # ---------------------------------------------------------------- rede
    _e("Radio/Mode", "Modo do rádio", "enum", _N,
       "Como o WiFi da placa funciona: AP = a própria placa cria a rede (padrão da DLC32: rede "
       "'MKS_DLC', IP 192.168.4.1); STA = a placa entra na rede do roteador (Sta/SSID) e recebe um "
       "IP dele; NONE = rádio desligado (só USB).", choices=("NONE", "STA", "AP"),
       typical="AP para usar direto; STA para usar pela rede da oficina.", risk=_NET_RISK,
       restart=True),
    _e("Sta/SSID", "Nome da rede (modo STA)", "string", _N,
       "Nome (SSID) da rede WiFi do roteador à qual a placa deve se conectar no modo STA. "
       "Respeita maiúsculas/minúsculas.", min_len=1, max_len=32, risk=_NET_RISK, restart=True,
       typical="O nome exato da sua rede 2,4 GHz (o ESP32 não conecta em 5 GHz)."),
    _e("Sta/Password", "Senha da rede (modo STA)", "password", _N,
       "Senha da rede do roteador. O firmware nunca mostra a senha gravada (aparece ******); "
       "deixe em branco para manter a atual.", min_len=8, max_len=64, risk=_NET_RISK, restart=True,
       typical="8 a 64 caracteres (WPA2)."),
    _e("Sta/IPMode", "Endereço IP (modo STA)", "enum", _N,
       "DHCP = o roteador escolhe o IP da placa (descubra-o na tela da DLC32 ou no roteador); "
       "Static = usa o IP fixo de Sta/IP, Sta/Gateway e Sta/Netmask.", choices=("DHCP", "Static"),
       typical="DHCP. Use Static só com os três campos de IP corretos.",
       risk="Static com IP/gateway errados deixa a placa inacessível pela rede.", restart=True),
    _e("Sta/IP", "IP fixo (modo STA)", "ip", _N,
       "IP fixo usado quando Sta/IPMode = Static. Deve estar na faixa da sua rede e fora da faixa "
       "do DHCP do roteador.", typical="ex.: 192.168.0.50", risk=_NET_RISK, restart=True),
    _e("Sta/Gateway", "Gateway (modo STA)", "ip", _N,
       "Endereço do roteador quando Sta/IPMode = Static.", typical="ex.: 192.168.0.1",
       risk=_NET_RISK, restart=True),
    _e("Sta/Netmask", "Máscara de rede (modo STA)", "ip", _N,
       "Máscara de sub-rede quando Sta/IPMode = Static.", typical="255.255.255.0",
       risk=_NET_RISK, restart=True),
    _e("AP/SSID", "Nome da rede própria (modo AP)", "string", _N,
       "Nome da rede WiFi criada pela placa no modo AP.", min_len=1, max_len=32,
       typical="MKS_DLC (padrão da DLC32)", risk=_NET_RISK, restart=True),
    _e("AP/Password", "Senha da rede própria (modo AP)", "password", _N,
       "Senha da rede criada pela placa no modo AP (mostrada como ******).", min_len=8, max_len=64,
       typical="12345678 de fábrica na DLC32 — troque por segurança.", risk=_NET_RISK, restart=True),
    _e("AP/IP", "IP da placa (modo AP)", "ip", _N,
       "Endereço da placa na rede que ela mesma cria (modo AP). É o IP usado em tcp://IP:23.",
       typical="192.168.4.1", risk=_NET_RISK, restart=True),
    _e("AP/Channel", "Canal WiFi (modo AP)", "int", _N,
       "Canal de rádio da rede própria (1–14). Troque se houver interferência.", minimum=1,
       maximum=14, typical="1, 6 ou 11", restart=True),
    _e("System/Hostname", "Nome do dispositivo (hostname)", "string", _N,
       "Nome da placa na rede (letras, números e hífen).", min_len=1, max_len=32,
       typical="grblesp (padrão)", restart=True),
    _e("Telnet/Enable", "Servidor Telnet", "enum", _N,
       "Liga o acesso por Telnet (TCP cru), usado pelo BrokCAM em tcp://IP:porta.",
       choices=("OFF", "ON"), typical="ON",
       risk="Com OFF o BrokCAM não consegue se conectar pelo WiFi.", restart=True),
    _e("Telnet/Port", "Porta Telnet", "int", _N,
       "Porta TCP do servidor Telnet.", minimum=1, maximum=65001, typical="23",
       risk="Se mudar, use a nova porta em tcp://IP:porta.", restart=True),
    _e("Http/Enable", "Servidor web (WebUI)", "enum", _N,
       "Liga a interface web da placa (abra http://IP no navegador).", choices=("OFF", "ON"),
       typical="ON", restart=True),
    _e("Http/Port", "Porta HTTP", "int", _N,
       "Porta do servidor web. A WebUI também usa a porta seguinte (+1) para websocket.",
       minimum=1, maximum=65001, typical="80", restart=True),
    _e("Bluetooth/Name", "Nome Bluetooth", "string", _N,
       "Nome do Bluetooth (só em firmwares Grbl_Esp32 compilados com Bluetooth; a DLC32 não usa).",
       min_len=1, max_len=32, firmwares=("grbl_esp32",), restart=True),
    # ---------------------------------------------------------------- spindle
    _e("Spindle/Type", "Tipo de spindle/laser", "enum", _S,
       "Que tipo de saída controla a ferramenta: PWM (spindle/laser por PWM), LASER, RELAY "
       "(liga/desliga), NONE, inversores (HUANYANG, H2A) etc.",
       choices=("NONE", "PWM", "RELAY", "LASER", "DAC", "HUANYANG", "BESC", "10V", "H2A"),
       typical="PWM (padrão da DLC32)",
       risk="Tipo errado = spindle/laser não liga ou liga em potência máxima."),
    _e("Spindle/PWM/Invert", "Inverter PWM", "bool", _S,
       "Inverte o sinal PWM (100% vira 0%).", typical="0",
       risk="Invertido sem necessidade o laser/spindle LIGA no máximo quando deveria estar desligado."),
    _e("Spindle/Enable/Invert", "Inverter enable do spindle", "bool", _S,
       "Inverte o pino de habilitação do spindle.", typical="0"),
    _e("Spindle/Enable/OffWithSpeed", "Desligar enable com S0", "bool", _S,
       "Desliga o pino de enable quando a rotação programada é 0.", typical="0"),
    _e("Spindle/Delay/SpinUp", "Espera para acelerar", "float", _S,
       "Segundos que o firmware espera após M3/M4 para o spindle atingir a rotação.", unit="s",
       minimum=0, maximum=30, typical="0 em laser; 2–5 s em spindle de fresagem."),
    _e("Spindle/Delay/SpinDown", "Espera para parar", "float", _S,
       "Segundos de espera após M5 para o spindle parar.", unit="s", minimum=0, maximum=30,
       typical="0–3 s"),
    _e("Coolant/Delay/TurnOn", "Espera da refrigeração", "float", _S,
       "Segundos de espera após ligar a refrigeração (M7/M8).", unit="s", minimum=0, maximum=30,
       typical="0"),
    _e("Laser/FullPower", "Potência total do laser", "int", _S,
       "Valor de S que corresponde a 100% no modo LASER.", minimum=0, maximum=10000,
       typical="1000"),
    # ---------------------------------------------------------------- homing
    *[_e(f"Homing/Cycle{i}", f"Ciclo de homing {i}", "mask", _H,
         f"Eixos referenciados juntos na etapa {i} do $H (as etapas rodam em ordem 0→5). Ex.: Z "
         f"sozinho na etapa 0 e XY na etapa 1 sobe a ferramenta antes de mover XY.",
         minimum=0, maximum=7, typical=("XY (padrão DLC32)" if i == 0 else "vazio"))
      for i in range(6)],
    _e("Homing/Squared", "Eixos esquadrejados", "mask", _H,
       "Eixos com dois motores e duas chaves que o homing alinha (esquadreja).", minimum=0,
       maximum=7, typical="vazio"),
    *[_e(f"{ax}/Home/Mpos", f"Posição de máquina após homing {ax}", "float", _H,
         f"Coordenada de máquina atribuída ao eixo {ax} quando o homing termina.", unit="mm",
         minimum=-100000, maximum=100000, typical="0")
      for ax in "XYZ"],
    # ---------------------------------------------------------------- motores
    _e("Stepper/Direction/Delay", "Atraso da direção", "int", _M,
       "Atraso entre mudar o pino DIR e o pulso STEP (alguns drivers externos exigem).", unit="µs",
       minimum=0, maximum=1000, typical="0"),
    _e("Stepper/Enable/Delay", "Atraso do enable", "int", _M,
       "Espera após habilitar os drivers antes do primeiro passo.", unit="µs", minimum=0,
       maximum=1000, typical="0"),
    # ---------------------------------------------------------------- sistema
    _e("GCode/Line0", "Linha de inicialização 0 ($N0)", "string", _Y,
       "G-code executado automaticamente a cada reset/ligação (ex.: G21 G90).", max_len=80,
       min_len=0, typical="vazio",
       risk="Um movimento aqui faz a máquina se mover sozinha ao ligar."),
    _e("GCode/Line1", "Linha de inicialização 1 ($N1)", "string", _Y,
       "Segunda linha executada a cada reset/ligação.", max_len=80, min_len=0, typical="vazio",
       risk="Um movimento aqui faz a máquina se mover sozinha ao ligar."),
    _e("Errors/Verbose", "Erros por extenso", "bool", _Y,
       "Responde 'error: texto' em vez de 'error:N'.", typical="0",
       risk="Com 1, o BrokCAM não consegue identificar o número do erro."),
    _e("Message/Level", "Nível das mensagens", "enum", _Y,
       "Quantidade de mensagens [MSG:...] enviadas pelo firmware.",
       choices=("None", "Error", "Warning", "Info", "Debug", "Verbose"), typical="Info"),
    _e("Firmware/Build", "Texto do build", "string", _Y,
       "Texto livre que aparece no [VER:...] do $I.", max_len=60, min_len=0, typical="vazio"),
    *[_e(f"User/Macro{i}", f"Macro do usuário {i}", "string", _Y,
         "G-code executado pelo botão de macro físico/da WebUI.", max_len=120, min_len=0,
         typical="vazio") for i in range(4)],
]

EXTENDED: dict[str, SettingDef] = {d.key: d for d in _EXTENDED_LIST}

#: nome estendido (minúsculas) -> número equivalente ("stepper/pulse" -> "0")
NAME_ALIASES: dict[str, str] = {d.alias.lower(): d.key for d in _NUMBERED_LIST if d.alias}
_EXT_LOWER: dict[str, SettingDef] = {k.lower(): v for k, v in EXTENDED.items()}


def setting_def(key: str) -> SettingDef:
    """Definição de uma configuração (numerada ou nomeada); genérica se desconhecida."""
    k = str(key).lstrip("$")
    if k in NUMBERED:
        return NUMBERED[k]
    d = _EXT_LOWER.get(k.lower())
    if d is not None:
        return d
    # eixos extras (A/B/C): mesmas configurações dos eixos X/Y/Z
    if k.isdigit() and 100 <= int(k) < 140 and int(k) % 10 in (3, 4, 5):
        base = NUMBERED[str(int(k) - int(k) % 10)]
        ax = "ABC"[int(k) % 10 - 3]
        return _axis_copy(base, k, ax)
    m = re.fullmatch(r"([A-Ca-c])/(.+)", k)
    if m and f"x/{m.group(2).lower()}" in NAME_ALIASES:
        base = NUMBERED[NAME_ALIASES[f"x/{m.group(2).lower()}"]]
        return _axis_copy(base, k, m.group(1).upper())
    m = re.fullmatch(r"([XYZABCxyzabc])/(Current/Run|Current/Hold|Microsteps|StallGuard)", k, re.IGNORECASE)
    if m:
        what = m.group(2).lower()
        name, unit, kind = {"current/run": ("Corrente de trabalho", "A", "float"),
                            "current/hold": ("Corrente parado", "A", "float"),
                            "microsteps": ("Micropassos", "", "int"),
                            "stallguard": ("Sensibilidade StallGuard", "", "int")}[what]
        return SettingDef(k, f"{name} {m.group(1).upper()}", kind, _M,
                          "Só vale para drivers Trinamic controlados pelo firmware (SPI/UART). Na "
                          "DLC32 com drivers comuns (stepsticks) não tem efeito: a corrente é ajustada "
                          "no trimpot do driver e os micropassos nos jumpers.", unit=unit,
                          typical="não usado na DLC32 padrão")
    if k.isdigit():
        n = int(k)
        cat = _G if n < 20 else _M if 140 <= n < 180 else _Y
        return SettingDef(k, f"Configuração ${k}", "float", cat,
                          "Configuração específica deste firmware, sem descrição no catálogo do "
                          "BrokCAM. Consulte o manual da placa antes de alterar.",
                          risk="Configuração sem descrição: altere só se souber o efeito.")
    cat = _N if k.split("/")[0].lower() in ("sta", "ap", "wifi", "http", "telnet", "radio", "bluetooth") \
        else _H if k.lower().startswith("homing") else _S if k.lower().startswith(("spindle", "laser")) \
        else _Y
    return SettingDef(k, k, "string", cat,
                      "Configuração estendida do firmware sem descrição no catálogo do BrokCAM. "
                      "Consulte a documentação do Grbl_Esp32/MKS antes de alterar.",
                      risk="Configuração sem descrição: altere só se souber o efeito.",
                      restart=cat == _N)


def numbered_equivalent(name: str) -> str | None:
    """Número equivalente de uma configuração nomeada (``"X/MaxRate"`` → ``"110"``)."""
    low = str(name).lower().lstrip("$")
    if low in NAME_ALIASES:
        return NAME_ALIASES[low]
    m = re.fullmatch(r"([abc])/(.+)", low)
    if m and f"x/{m.group(2)}" in NAME_ALIASES:
        return str(int(NAME_ALIASES[f"x/{m.group(2)}"]) + 3 + "abc".index(m.group(1)))
    return None


def _axis_copy(base: SettingDef, key: str, axis: str) -> SettingDef:
    from dataclasses import replace
    return replace(base, key=key, name=base.name[:-1] + axis,
                   help=base.help.replace("eixo X", f"eixo {axis}").replace(" X:", f" {axis}:")
                   + f" (Eixo extra {axis}: só tem efeito se a placa tiver esse eixo.)",
                   alias=f"{axis}/{base.alias.split('/', 1)[1]}" if base.alias else "")


def categorize(keys: Iterable[str]) -> dict[str, list[str]]:
    """Agrupa chaves por categoria, na ordem de :data:`CATEGORIES` (numeradas primeiro)."""
    out: dict[str, list[str]] = {c: [] for c in CATEGORIES}
    for k in keys:
        out.setdefault(setting_def(k).category, []).append(str(k))

    def order(k: str) -> tuple[int, int, str]:
        return (0, int(k), "") if k.isdigit() else (1, 0, k.lower())
    return {c: sorted(v, key=order) for c, v in out.items() if v}


# =========================================================================== valores

def is_hidden_value(value: str | None) -> bool:
    return value is not None and value.strip() in HIDDEN_VALUES


_IP_RE = re.compile(r"^(\d{1,3})\.(\d{1,3})\.(\d{1,3})\.(\d{1,3})$")
_FORBIDDEN = set("?!~\r\n\t")


_AXES = "XYZABC"


def to_number(text: str, kind: str = "float") -> float:
    """Converte o texto de um valor em número.

    Aceita as formas do Grbl_Esp32 além de números: ``On``/``Off`` (flags no ``$S``)
    e listas de eixos ``"XY"`` (máscaras no ``$S``; vazio = 0).
    """
    s = str(text).strip()
    if kind == "bool" and s.lower() in ("on", "true", "yes", "enabled", "off", "false", "no", "disabled"):
        return 1.0 if s.lower() in ("on", "true", "yes", "enabled") else 0.0
    if kind == "mask":
        if s == "":
            return 0.0
        if s.isalpha():
            v = 0
            for ch in s.upper():
                i = _AXES.find(ch)
                if i < 0:
                    raise ValueError(f"Eixo desconhecido: {ch}")
                v |= 1 << i
            return float(v)
    return float(s.replace(",", "."))


def _num(text: str) -> float:
    return to_number(text)


def _fmt_num(v: float, kind: str) -> str:
    if kind in ("int", "bool", "mask"):
        return str(int(round(v)))
    s = f"{v:.3f}".rstrip("0").rstrip(".")
    return s if s not in ("", "-0") else "0"


def validate_value(defn: SettingDef, text: str) -> str:
    """Valida ``text`` para ``defn`` e devolve o valor normalizado a enviar.

    Levanta ``ValueError`` com uma mensagem em PT-BR se o valor for inválido.
    """
    raw = "" if text is None else str(text)
    if any(c in _FORBIDDEN for c in raw):
        raise ValueError("Os caracteres ? ! ~ e quebras de linha são comandos de tempo real do GRBL "
                         "e não podem ser enviados dentro de um valor.")
    if any(ord(c) > 126 or ord(c) < 32 for c in raw):
        raise ValueError("Use apenas caracteres ASCII (sem acentos).")
    k = defn.kind
    s = raw.strip()
    if k in ("int", "float", "bool", "mask"):
        if not s and k != "mask":
            raise ValueError("Informe um número.")
        try:
            v = to_number(s, k)
        except ValueError:
            raise ValueError(f"'{s}' não é um número válido.") from None
        if k in ("int", "bool", "mask") and abs(v - round(v)) > 1e-9:
            raise ValueError("Este valor precisa ser inteiro.")
        lo, hi = defn.minimum, defn.maximum
        if k == "bool":
            lo, hi = 0, 1
        if v < 0 and (lo is None or lo >= 0):
            raise ValueError("O valor não pode ser negativo.")
        if lo is not None and v < lo - 1e-9:
            raise ValueError(f"Mínimo: {_fmt_num(lo, k)} {defn.unit}".strip())
        if hi is not None and v > hi + 1e-9:
            raise ValueError(f"Máximo: {_fmt_num(hi, k)} {defn.unit}".strip())
        return _fmt_num(v, k)
    if k == "enum":
        for c in defn.choices:
            if c.lower() == s.lower():
                return c
        raise ValueError("Escolha uma das opções: " + ", ".join(defn.choices))
    if k == "ip":
        m = _IP_RE.match(s)
        if not m or any(int(g) > 255 for g in m.groups()):
            raise ValueError("Endereço IP inválido (use o formato 192.168.0.10).")
        return s
    # string / password
    if k == "password" and s == "":
        return ""
    if defn.key.lower() == "system/hostname" and not re.fullmatch(r"[A-Za-z0-9-]+", s):
        raise ValueError("O hostname aceita só letras, números e hífen.")
    if defn.min_len is not None and len(s) < defn.min_len:
        raise ValueError(f"Mínimo de {defn.min_len} caracteres.")
    if defn.max_len is not None and len(s) > defn.max_len:
        raise ValueError(f"Máximo de {defn.max_len} caracteres.")
    return s


def values_equal(defn: SettingDef, a: str | None, b: str | None) -> bool:
    """Compara dois valores de uma configuração (números com tolerância)."""
    if a is None or b is None:
        return a is b
    if defn.kind in ("int", "float", "bool", "mask"):
        try:
            x, y = to_number(a, defn.kind), to_number(b, defn.kind)
            return abs(x - y) <= 1e-6 * max(1.0, abs(y))
        except ValueError:
            return a.strip() == b.strip()
    if defn.kind == "enum":
        return a.strip().lower() == b.strip().lower()
    return a == b


# =========================================================================== parsers

_SETTING_LINE = re.compile(r"^\$([^=\s]+)=(.*)$")
_DEFAULT_SUFFIX = re.compile(r"\s{2,}\(Default=.*\)\s*$")


def parse_extended_settings(lines: Iterable[str]) -> dict[str, str]:
    """Interpreta ``$S``/``$+``/``$SC``: ``{"Sta/SSID": "MKS_DLC", "10": "1", ...}``.

    Mantém o valor exatamente como veio (inclusive espaços internos); remove o sufixo
    ``(Default=...)`` do ``$SC``. Ignora linhas que não são ``$nome=valor``.
    """
    out: dict[str, str] = {}
    for raw in lines:
        ln = raw.rstrip("\r\n")
        m = _SETTING_LINE.match(ln.strip())
        if not m:
            continue
        out[m.group(1)] = _DEFAULT_SUFFIX.sub("", m.group(2)).strip()
    return out


def parse_numbered_settings(lines: Iterable[str]) -> dict[int, str]:
    """``$$`` como texto: ``{110: "1000.000", ...}`` (preserva o formato original)."""
    out: dict[int, str] = {}
    for ln in lines:
        m = re.match(r"^\$(\d+)=(\S+)", ln.strip())
        if m:
            out[int(m.group(1))] = m.group(2)
    return out


def _vec(text: str) -> tuple[float, ...]:
    return tuple(float(v) for v in text.split(",") if v.strip())


def parse_parameters(lines: Iterable[str]) -> dict[str, tuple[float, ...]]:
    """``$#``: ``{"G54": (x,y,z), ..., "TLO": (v,), "PRB": (x,y,z,ok)}``."""
    out: dict[str, tuple[float, ...]] = {}
    for ln in lines:
        m = re.match(r"\[(\w+(?:\.\d)?):([^\]]*)\]", ln.strip())
        if not m:
            continue
        key, val = m.group(1), m.group(2)
        try:
            if key == "PRB":
                coords, _, flag = val.rpartition(":")
                out[key] = _vec(coords) + (float(flag or 0),)
            elif key in ("GC", "MSG", "VER", "OPT"):
                continue
            else:
                out[key] = _vec(val)
        except ValueError:
            continue
    return out


def parse_parser_state(lines: Iterable[str]) -> list[str]:
    """``$G``: palavras modais ``['G0', 'G54', 'G17', 'G21', 'G90', 'G94', 'M5', 'M9', 'T0', 'F0', 'S0']``."""
    for ln in lines:
        s = ln.strip()
        if s.startswith("[GC:"):
            return s[4:].rstrip("]").split()
    return []


@dataclass(frozen=True)
class ParserItem:
    group: str
    code: str
    meaning: str


_MOTION = {"G0": "Rápido (G0)", "G1": "Linear com avanço (G1)", "G2": "Arco horário (G2)",
           "G3": "Arco anti-horário (G3)", "G38.2": "Sondagem (G38.2)", "G38.3": "Sondagem (G38.3)",
           "G38.4": "Sondagem afastando (G38.4)", "G38.5": "Sondagem afastando (G38.5)",
           "G80": "Movimento cancelado (G80)"}
_MODAL = {
    "G17": ("Plano", "XY (G17) — arcos no plano da mesa"),
    "G18": ("Plano", "ZX (G18)"), "G19": ("Plano", "YZ (G19)"),
    "G20": ("Unidades", "Polegadas (G20)"), "G21": ("Unidades", "Milímetros (G21)"),
    "G90": ("Distância", "Absoluta (G90) — coordenadas a partir do zero de trabalho"),
    "G91": ("Distância", "Incremental (G91) — coordenadas relativas à posição atual"),
    "G91.1": ("Arcos IJK", "Centro incremental (G91.1)"),
    "G93": ("Modo de avanço", "Inverso do tempo (G93)"),
    "G94": ("Modo de avanço", "Unidades por minuto (G94)"),
    "G40": ("Compensação de raio", "Desligada (G40)"),
    "G43.1": ("Comprimento da ferramenta", "Compensação dinâmica ativa (G43.1)"),
    "G49": ("Comprimento da ferramenta", "Sem compensação (G49)"),
    "G61": ("Caminho", "Parada exata (G61)"),
    "M3": ("Spindle", "Ligado, sentido horário (M3)"),
    "M4": ("Spindle", "Ligado, anti-horário / laser dinâmico (M4)"),
    "M5": ("Spindle", "Desligado (M5)"),
    "M7": ("Refrigeração", "Névoa ligada (M7)"), "M8": ("Refrigeração", "Líquido ligado (M8)"),
    "M9": ("Refrigeração", "Desligada (M9)"),
    "M0": ("Programa", "Pausa programada (M0)"), "M1": ("Programa", "Pausa opcional (M1)"),
    "M2": ("Programa", "Fim de programa (M2)"), "M30": ("Programa", "Fim de programa (M30)"),
    "M56": ("Estacionamento", "Override de estacionamento (M56)"),
}


def describe_parser_state(words: Iterable[str]) -> list[ParserItem]:
    """Traduz as palavras do ``$G`` em itens legíveis (grupo, código, significado)."""
    items: list[ParserItem] = []
    for w in words:
        u = w.upper()
        if u in _MOTION:
            items.append(ParserItem("Movimento", u, _MOTION[u]))
        elif re.fullmatch(r"G5[4-9](\.\d)?", u):
            n = int(u[1:3]) - 53
            items.append(ParserItem("Sistema de coordenadas", u, f"Coordenadas de trabalho {n} ({u})"))
        elif u in _MODAL:
            g, m = _MODAL[u]
            items.append(ParserItem(g, u, m))
        elif u.startswith("T"):
            items.append(ParserItem("Ferramenta", u, f"Ferramenta {u[1:]}"))
        elif u.startswith("F"):
            items.append(ParserItem("Avanço (F)", u, f"{u[1:]} mm/min programados"))
        elif u.startswith("S"):
            items.append(ParserItem("Rotação/potência (S)", u, f"S{u[1:]} programado"))
        else:
            items.append(ParserItem("Outro", u, u))
    return items


PIN_LABELS: dict[str, str] = {
    "X": "Limite X", "Y": "Limite Y", "Z": "Limite Z", "A": "Limite A", "B": "Limite B",
    "C": "Limite C", "P": "Sonda", "D": "Porta", "H": "Botão pausa", "R": "Botão reset",
    "S": "Botão início",
}


def describe_pins(pins: str) -> dict[str, bool]:
    """``"XP"`` → ``{"X": True, "Y": False, "Z": False, "P": True, "D": False, ...}``."""
    active = set((pins or "").upper())
    return {k: (k in active) for k in PIN_LABELS}


def parse_ap_list(lines: Iterable[str]) -> list[dict]:
    """Lista de redes do ``$WiFi/ListAPs`` / ``[ESP410]`` (JSON ``{"AP_LIST": [...]}``)."""
    text = "".join(lines)
    i = text.find("{")
    if i < 0:
        return []
    try:
        data = json.loads(text[i:text.rfind("}") + 1])
    except ValueError:
        return []
    out = []
    for ap in data.get("AP_LIST", []):
        try:
            out.append({"ssid": str(ap.get("SSID", "")), "signal": int(ap.get("SIGNAL", 0)),
                        "protected": str(ap.get("IS_PROTECTED", "")).lower() in ("1", "true")})
        except (TypeError, ValueError):
            continue
    return sorted(out, key=lambda a: -a["signal"])


def split_tcp_url(url: str, default_port: int = 23) -> tuple[str, int]:
    """``"tcp://192.168.4.1:23"`` → ``("192.168.4.1", 23)``. Aceita ``telnet://`` e sem esquema."""
    s = url.strip()
    s = re.sub(r"^(tcp|telnet)://", "", s, flags=re.IGNORECASE).rstrip("/")
    if not s:
        raise ValueError("Endereço vazio.")
    if s.startswith("["):                          # IPv6 [::1]:23
        host, _, rest = s[1:].partition("]")
        port = rest.lstrip(":")
    elif s.count(":") == 1:
        host, port = s.split(":")
    else:
        host, port = s, ""
    if not host:
        raise ValueError("Informe o IP ou nome da placa.")
    try:
        p = int(port) if port else default_port
    except ValueError:
        raise ValueError(f"Porta inválida: {port}") from None
    if not 1 <= p <= 65535:
        raise ValueError(f"Porta fora da faixa: {p}")
    return host, p


# =========================================================================== backup

BACKUP_FORMAT = "brokcam-machine-settings"


def backup_payload(numbered: dict, extended: dict, fw: FirmwareInfo | None = None,
                   created: str = "") -> dict:
    """Conteúdo do arquivo de backup (JSON). Senhas ocultas (``******``) não são salvas."""
    return {
        "format": BACKUP_FORMAT, "version": 1, "created": created,
        "firmware": fw.to_dict() if fw is not None else {},
        "numbered": {str(k): str(v) for k, v in sorted(numbered.items(), key=lambda kv: int(kv[0]))},
        "extended": {str(k): str(v) for k, v in extended.items()
                     if not is_hidden_value(str(v)) and not str(k).isdigit()
                     and str(k).lower() not in NAME_ALIASES},
    }


def read_backup(data: dict) -> tuple[dict[str, str], dict[str, str]]:
    """Valida um backup e devolve ``(numeradas, estendidas)`` como texto."""
    if not isinstance(data, dict) or data.get("format") not in (BACKUP_FORMAT, "tracecam-machine-settings"):
        raise ValueError("O arquivo não é um backup de configurações do BrokCAM.")
    num = {str(k): str(v) for k, v in (data.get("numbered") or {}).items() if str(k).isdigit()}
    ext = {str(k): str(v) for k, v in (data.get("extended") or {}).items()
           if not is_hidden_value(str(v))}
    return num, ext


# =========================================================================== perfil de referência

@dataclass(frozen=True)
class ProfileValue:
    value: str
    note: str
    machine_specific: bool = False     # depende da mecânica: diferença não é "erro"


DLC32_PROFILE_NAME = "MKS DLC32 típica (gravadora/fresadora pequena)"
DLC32_PROFILE_DESCRIPTION = (
    "Valores de referência para uma MKS DLC32 numa gravadora a laser ou fresadora pequena tipo "
    "3018. Partem dos padrões de fábrica do firmware MKS (Machines/i2s_out_xyz_mks_dlc32.h) com "
    "ajustes para fresar PCB. Use só como COMPARAÇÃO: valores de calibração (passos/mm, curso, "
    "direções) dependem da sua mecânica e nunca devem ser copiados sem medir.")

_P = ProfileValue
DLC32_PROFILE: dict[str, ProfileValue] = {
    "0": _P("10", "Padrão do firmware DLC32 (10 µs)."),
    "1": _P("255", "Fábrica = 25 ms; 255 mantém os motores travados (o Z não cai entre passadas)."),
    "2": _P("0", "Nenhum driver da DLC32 exige pulso invertido."),
    "3": _P("1", "Padrão de fábrica (X invertido); depende da fiação dos motores.", True),
    "4": _P("0", "Padrão da DLC32."),
    "5": _P("1", "Padrão de fábrica da DLC32 para os conectores de fim de curso."),
    "6": _P("1", "Padrão de fábrica da DLC32 para o pino de sonda."),
    "10": _P("1", "Posição de máquina no relatório (o BrokCAM calcula a de trabalho)."),
    "11": _P("0.01", "Padrão do firmware."),
    "12": _P("0.002", "Padrão do firmware."),
    "13": _P("0", "Relatórios em mm."),
    "20": _P("0", "Sem fins de curso de fábrica; ligue junto com $22 se instalar chaves."),
    "21": _P("0", "Sem fins de curso de fábrica."),
    "22": _P("0", "Sem fins de curso de fábrica."),
    "23": _P("1", "Padrão de fábrica; depende de onde estão as chaves.", True),
    "24": _P("300", "Padrão de fábrica."),
    "25": _P("1000", "Padrão de fábrica."),
    "26": _P("250", "Padrão de fábrica."),
    "27": _P("1", "Padrão de fábrica."),
    "28": _P("1000", "1 kHz funciona com a maioria dos lasers de diodo e drivers PWM."),
    "30": _P("1000", "S1000 = 100% (convenção de laser e do pós-processador do BrokCAM)."),
    "31": _P("0", "Padrão."),
    "32": _P("0", "0 para fresar PCB; use 1 se a máquina for um laser."),
    "38": _P("1", "Padrão de fábrica."),
    "40": _P("1", "Padrão de fábrica."),
    "100": _P("80", "Correia GT2 20 dentes, 1/16 (padrão da DLC32). Fuso T8: 400.", True),
    "101": _P("80", "Correia GT2 20 dentes, 1/16 (padrão da DLC32). Fuso T8: 400.", True),
    "102": _P("400", "Fuso T8 1/16 no Z (fresadora). O padrão de fábrica é 80.", True),
    "110": _P("3000", "Fábrica = 6000 (pensado em laser com correia); 3000 é mais seguro."),
    "111": _P("3000", "Fábrica = 6000; 3000 é mais seguro em fresadora pequena."),
    "112": _P("600", "Z com fuso: rápido demais perde passos e muda a profundidade."),
    "120": _P("500", "Padrão de fábrica (X)."),
    "121": _P("500", "Padrão de fábrica (Y)."),
    "122": _P("100", "Z mais suave que o padrão (500) para não perder passos."),
    "130": _P("450", "Padrão de fábrica; meça o curso real.", True),
    "131": _P("450", "Padrão de fábrica; meça o curso real.", True),
    "132": _P("50", "Padrão de fábrica; meça o curso real.", True),
    "Radio/Mode": _P("AP", "Padrão: a placa cria a rede MKS_DLC."),
    "AP/SSID": _P("MKS_DLC", "Nome de fábrica da rede."),
    "AP/IP": _P("192.168.4.1", "IP de fábrica no modo AP."),
    "AP/Channel": _P("1", "Canal de fábrica."),
    "Sta/IPMode": _P("DHCP", "O roteador escolhe o IP."),
    "Telnet/Enable": _P("ON", "Necessário para o BrokCAM via WiFi."),
    "Telnet/Port": _P("23", "Porta de fábrica."),
    "Http/Enable": _P("ON", "WebUI ligada."),
    "Http/Port": _P("80", "Porta de fábrica."),
    "System/Hostname": _P("grblesp", "Nome de fábrica.", True),
    "Spindle/Type": _P("PWM", "Padrão da DLC32."),
    "Errors/Verbose": _P("0", "Erros numéricos (o BrokCAM traduz)."),
    "Message/Level": _P("Info", "Padrão."),
}
