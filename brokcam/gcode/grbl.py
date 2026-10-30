"""Pós-processador GRBL 1.1: converte ``Job``/``PathOp`` em texto G-code.

Projeto
-------
* Gera apenas comandos aceitos pelo GRBL 1.1 (G0/G1/G4/G10/G17/G21/G90/G94,
  M0/M2/M30/M3/M4/M5, T, S, F). Ciclos fixos (G81/G83) são expandidos em
  movimentos explícitos; compensação de raio (G41/G42) nunca é usada; ``M6`` só
  aparece se o usuário pedir explicitamente (o GRBL padrão responde ``error:20``).
* Rápido: as coordenadas são transformadas e arredondadas com numpy de uma vez e
  as linhas são montadas com listas + ``"\\n".join``. Eixos que não mudam são
  omitidos (economiza bytes no buffer serial de 128 bytes do GRBL).
* Puro: nenhuma dependência de Qt, nenhum estado global além do registro do
  ``ParamGroup``.
"""
from __future__ import annotations

import math
import re
import unicodedata
from dataclasses import dataclass
from typing import Any, Iterable

import numpy as np

from brokcam.core.model import CutParams, Job, PathOp, Tool
from brokcam.core.params import ParamGroup, ParamSpec, register, set_conditions

__all__ = ["GRBL_POST", "generate_gcode", "generate_program", "post_settings"]

# --------------------------------------------------------------------------- opções

GRBL_POST = register(ParamGroup(
    key="grbl_post",
    title="Pós-processador GRBL 1.1",
    description=(
        "Converte os caminhos calculados pela CAM em G-code para máquinas com firmware "
        "GRBL 1.1 (fresadoras CNC de PCB e gravadoras a laser). Aqui você escolhe o tipo "
        "de máquina, como as trocas de ferramenta são tratadas, o que acontece no início "
        "e no fim do programa e otimizações de tamanho do arquivo. Os valores padrão "
        "funcionam na maioria das fresadoras 3018/CNC caseiras com GRBL."
    ),
    params=(
        ParamSpec(
            "variant", "Tipo de máquina", "choice", "grbl_mill",
            help=(
                "Define o estilo do G-code gerado. 'Fresadora' (grbl_mill): usa o spindle "
                "com M3 S<RPM>, movimentos em Z para descer/subir a ferramenta e pausas para "
                "troca de ferramenta conforme a opção 'Troca de ferramenta'. 'Laser' "
                "(grbl_laser): exige $32=1 (modo laser) na máquina; usa M4 (potência "
                "dinâmica, proporcional à velocidade real, evitando queimar nos cantos), o "
                "valor S vira a potência do laser (0 até $30), não há NENHUM movimento em Z "
                "(o foco é ajustado manualmente) e não há trocas de ferramenta. 'Sem troca "
                "de ferramenta' (grbl_no_toolchange): igual à fresadora, porém nunca pausa "
                "para trocar ferramenta — use quando cada arquivo tem uma única ferramenta "
                "ou você prefere gerar um arquivo por ferramenta. Risco: usar 'Laser' numa "
                "fresadora faz o spindle girar sem descer a fresa (não corta nada); usar "
                "'Fresadora' num laser gera M3 (potência constante) e pode queimar a placa."
            ),
            choices=(("grbl_mill", "Fresadora (spindle)"),
                     ("grbl_laser", "Laser ($32=1, M4 dinâmico)"),
                     ("grbl_no_toolchange", "Fresadora sem troca de ferramenta")),
        ),
        ParamSpec(
            "coord_decimals", "Casas decimais (coordenadas)", "int", 4,
            help=(
                "Número de casas decimais das coordenadas X/Y/Z. Em milímetros, 3 casas "
                "= resolução de 1 µm, mais do que suficiente para qualquer fresadora "
                "caseira; 4 é o padrão do FlatCAM. Valores maiores só aumentam o arquivo e "
                "o tempo de transmissão serial; menores que 3 podem deformar pads finos e "
                "círculos pequenos."
            ),
            minimum=1, maximum=6, step=1, decimals=0, advanced=True,
        ),
        ParamSpec(
            "feed_decimals", "Casas decimais (avanço)", "int", 1,
            help=(
                "Número de casas decimais dos avanços F (mm/min) e do valor S. Avanços são "
                "valores grandes (ex.: 120 mm/min), então 0 ou 1 casa basta. Não afeta a "
                "precisão do corte."
            ),
            minimum=0, maximum=4, step=1, decimals=0, advanced=True,
        ),
        ParamSpec(
            "start_gcode", "G-code inicial personalizado", "str", "",
            help=(
                "Linhas de G-code inseridas logo após o cabeçalho (G21 G90 G94 G17), antes "
                "do primeiro movimento. Separe comandos com quebras de linha. Uso típico: "
                "ligar exaustor/ar (M8) ou selecionar outro sistema de coordenadas (G55). "
                "Comandos $ (ex.: $H) não são G-code e não devem ir aqui. Risco: o texto é enviado como está; "
                "comandos inválidos causam 'error' no GRBL e movimentos aqui podem colidir."
            ),
        ),
        ParamSpec(
            "end_gcode", "G-code final personalizado", "str", "",
            help=(
                "Linhas de G-code inseridas no final, depois de desligar o spindle e "
                "levantar a ferramenta, antes do fim de programa (M2/M30). Uso típico: "
                "desligar refrigeração (M9) ou mover para uma posição de descarga. Risco: "
                "enviado sem verificação."
            ),
        ),
        ParamSpec(
            "program_end", "Comando de fim de programa", "choice", "M2",
            help=(
                "Comando colocado na última linha. M2 e M30 são equivalentes no GRBL: "
                "encerram o programa, desligam spindle/refrigeração e restauram estados "
                "modais (G90, G54, F...). 'Nenhum' deixa a máquina no estado atual — útil "
                "quando vários arquivos são enviados em sequência."
            ),
            choices=(("M2", "M2"), ("M30", "M30"), ("none", "Nenhum")),
            advanced=True,
        ),
        ParamSpec(
            "end_z", "Z final", "float", 10.0,
            help=(
                "Altura (coordenada de trabalho) para onde a ferramenta sobe ao terminar o "
                "programa, para facilitar a remoção da placa. Típico: 10–30 mm. Risco: "
                "valores acima do curso da máquina disparam limite de software (ALARM:2) "
                "ou batem no fim de curso. Ignorado no modo laser."
            ),
            unit="mm", minimum=-50, maximum=200, step=1, decimals=3,
        ),
        ParamSpec(
            "end_move_xy", "Mover para posição final XY", "bool", False,
            help=(
                "Se ligado, após subir para o Z final a máquina vai em movimento rápido "
                "para (X final, Y final) em coordenadas de trabalho — por exemplo para "
                "trazer a mesa para frente. Tem prioridade sobre 'Retornar à origem'."
            ),
        ),
        ParamSpec(
            "end_x", "X final", "float", 0.0,
            help="Coordenada X (de trabalho) da posição final, usada se 'Mover para posição final XY' estiver ligado.",
            unit="mm", minimum=-2000, maximum=2000, decimals=3,
        ),
        ParamSpec(
            "end_y", "Y final", "float", 0.0,
            help="Coordenada Y (de trabalho) da posição final, usada se 'Mover para posição final XY' estiver ligado.",
            unit="mm", minimum=-2000, maximum=2000, decimals=3,
        ),
        ParamSpec(
            "return_home", "Retornar à origem no fim", "bool", True,
            help=(
                "Se ligado, ao final do programa (depois de subir para o Z final) a "
                "máquina volta em rápido para X0 Y0 do sistema de coordenadas de trabalho "
                "(o ponto que você zerou). Isso NÃO é o homing ($H). Desligue se a origem "
                "estiver num lugar onde a ferramenta atrapalhe a remoção da placa."
            ),
        ),
        ParamSpec(
            "toolchange_mode", "Troca de ferramenta", "choice", "m0",
            help=(
                "Como pausar entre operações com ferramentas diferentes. 'Só M0 com "
                "mensagem' (recomendado): desliga o spindle, sobe para o Z de troca, "
                "envia T<n> e M0 com uma mensagem (MSG) dizendo qual ferramenta colocar; o "
                "GRBL fica em pausa (Hold) até você apertar 'Retomar' (~). Depois da troca "
                "você normalmente precisa refazer o zero Z (sonda). 'M6 + M0': igual, mas "
                "também envia M6 — o GRBL 1.1 padrão NÃO suporta M6 e responde error:20; "
                "use apenas com forks/senders que interceptam o M6. 'Ignorar': não pausa "
                "(útil se todas as operações usam a mesma ferramenta). Risco de 'Ignorar' "
                "com ferramentas diferentes: a operação seguinte é feita com a ferramenta "
                "errada."
            ),
            choices=(("m0", "Só M0 com mensagem"),
                     ("m6_m0", "M6 + M0 (GRBL modificado)"),
                     ("ignore", "Ignorar")),
        ),
        ParamSpec(
            "toolchange_z", "Z de troca de ferramenta", "float", 15.0,
            help=(
                "Altura (coordenada de trabalho) para onde a ferramenta sobe antes da "
                "pausa de troca, deixando espaço para as mãos. Típico: 15–40 mm. Risco: "
                "acima do curso da máquina dispara alarme de limite."
            ),
            unit="mm", minimum=-50, maximum=200, step=1, decimals=3,
        ),
        ParamSpec(
            "toolchange_move_xy", "Mover XY na troca", "bool", False,
            help=(
                "Se ligado, além de subir, a máquina vai para (X de troca, Y de troca) "
                "antes de pausar, por exemplo para um canto de acesso fácil."
            ),
            advanced=True,
        ),
        ParamSpec(
            "toolchange_x", "X de troca", "float", 0.0,
            help="Coordenada X (de trabalho) da posição de troca de ferramenta.",
            unit="mm", minimum=-2000, maximum=2000, decimals=3, advanced=True,
        ),
        ParamSpec(
            "toolchange_y", "Y de troca", "float", 0.0,
            help="Coordenada Y (de trabalho) da posição de troca de ferramenta.",
            unit="mm", minimum=-2000, maximum=2000, decimals=3, advanced=True,
        ),
        ParamSpec(
            "toolchange_first_tool", "Pausar também na 1ª ferramenta", "bool", False,
            help=(
                "Se ligado, o programa já começa com uma pausa pedindo a primeira "
                "ferramenta. Útil quando o arquivo é enviado sem conferir a fresa montada. "
                "Desligado: assume que a primeira ferramenta já está no spindle."
            ),
            advanced=True,
        ),
        ParamSpec(
            "spindle_direction", "Sentido do spindle", "choice", "M3",
            help=(
                "M3 = horário (padrão de fresas comuns), M4 = anti-horário. Só altere se o "
                "spindle estiver ligado invertido. No modo laser é sempre M4 (dinâmico)."
            ),
            choices=(("M3", "M3 — horário"), ("M4", "M4 — anti-horário")),
            advanced=True,
        ),
        ParamSpec(
            "spindle_warmup_s", "Aquecimento do spindle", "float", 1.0,
            help=(
                "Pausa (G4 P<s>) logo após ligar o spindle, para ele atingir a rotação "
                "antes de descer na placa. Típico: 1–3 s para spindles DC 775/ER11; 0 se "
                "o spindle tiver partida suave rápida. Se a operação definir 'dwell' "
                "próprio (> 0), o maior valor é usado. Risco: valor baixo faz a fresa "
                "entrar no cobre devagar e quebrar (fresas de 0,1 mm são frágeis)."
            ),
            unit="s", minimum=0, maximum=60, step=0.5, decimals=1,
        ),
        ParamSpec(
            "laser_off_mode", "Laser entre caminhos", "choice", "auto",
            help=(
                "Como garantir o laser desligado nos deslocamentos (G0). 'Automático': "
                "com $32=1 o GRBL desliga o laser sozinho em G0 — gera o menor arquivo. "
                "'S0': adiciona S0 aos deslocamentos e S<potência> no primeiro corte — "
                "seguro mesmo se $32 estiver errado. 'M5': desliga (M5) antes de cada "
                "deslocamento e religa (M4 S) antes de cada caminho — o mais seguro, porém "
                "o maior arquivo. Risco de 'Automático' com $32=0: o laser fica aceso nos "
                "deslocamentos e risca a placa inteira."
            ),
            choices=(("auto", "Automático ($32=1)"), ("s0", "S0 nos deslocamentos"),
                     ("m5", "M5/M4 a cada caminho")),
        ),
        ParamSpec(
            "laser_drill_pulse_s", "Pulso do laser em furos", "float", 0.1,
            help=(
                "No modo laser, furos (operações 'drill') viram um disparo parado: M3 "
                "S<potência>, G4 P<tempo>, M5. Isto define o tempo do disparo (mínimo). "
                "Típico 0,05–0,5 s para marcar o centro dos furos."
            ),
            unit="s", minimum=0.0, maximum=10, step=0.05, decimals=2, advanced=True,
        ),
        ParamSpec(
            "comments", "Comentários no G-code", "bool", True,
            help=(
                "Inclui comentários entre parênteses (nome do trabalho, ferramenta, "
                "parâmetros, limites). Ajudam a conferir o arquivo, mas ocupam buffer "
                "serial (o enviador do BrokCAM os remove automaticamente). As mensagens "
                "de troca de ferramenta (MSG) são sempre mantidas."
            ),
        ),
        ParamSpec(
            "rapid_z_g0", "Usar G0 para subir Z", "bool", True,
            help=(
                "Ligado: a ferramenta sai do material e sobe até o Z de deslocamento em "
                "movimento rápido (G0), o mais eficiente. Desligado: sobe com G1 no avanço "
                "de mergulho (mais lento, porém mais suave para máquinas com Z frouxo ou "
                "spindles pesados)."
            ),
            advanced=True,
        ),
        ParamSpec(
            "avoid_retract", "Evitar retrações curtas", "bool", False,
            help=(
                "Se o início do próximo caminho estiver a menos de (fator × diâmetro da "
                "ferramenta) do ponto atual, vai até ele em G1 SEM subir a ferramenta, "
                "economizando tempo em isolamentos com muitos segmentos próximos. Risco: o "
                "trecho de ligação corta o material entre os caminhos — pode cortar uma "
                "trilha fina. Ligue apenas se entender a geometria (ex.: passes paralelos "
                "de limpeza de cobre)."
            ),
        ),
        ParamSpec(
            "avoid_retract_factor", "Fator de distância (retração)", "float", 1.0,
            help=(
                "Multiplicador do diâmetro da ferramenta usado por 'Evitar retrações "
                "curtas'. 1,0 = só liga caminhos cuja distância é menor que o próprio "
                "diâmetro (a ligação fica dentro de uma área praticamente já fresada)."
            ),
            minimum=0.0, maximum=10.0, step=0.1, decimals=2, advanced=True,
        ),
        ParamSpec(
            "peck_retract", "Retração da furação por bicadas", "choice", "full",
            help=(
                "Quando a furação usa multiprofundidade (bicadas/peck, equivalente ao "
                "G83 que o GRBL não tem), define o recuo entre bicadas. 'Total': sobe até o "
                "Z de deslocamento (limpa todo o cavaco; mais lento). 'Parcial': recua só "
                "a 'folga da bicada' (quebra o cavaco, mais rápido; equivalente ao G73)."
            ),
            choices=(("full", "Total (G83)"), ("partial", "Parcial (G73)")),
            advanced=True,
        ),
        ParamSpec(
            "peck_clearance", "Folga da bicada", "float", 0.3,
            help=(
                "Distância acima do fundo da bicada anterior para onde a broca volta em "
                "rápido antes de continuar furando com avanço. Típico 0,2–0,5 mm. Muito "
                "pequena: a broca pode bater no cavaco em rápido."
            ),
            unit="mm", minimum=0.05, maximum=5.0, step=0.05, decimals=2, advanced=True,
        ),
        ParamSpec(
            "drill_dwell_s", "Pausa no fundo do furo", "float", 0.0,
            help=(
                "Pausa (G4) com a broca no fundo de cada furo antes de subir, para "
                "limpar a borda. Normalmente 0 em PCB. Cada 0,1 s × 500 furos = 50 s a "
                "mais de usinagem."
            ),
            unit="s", minimum=0.0, maximum=10.0, step=0.1, decimals=2, advanced=True,
        ),
        ParamSpec(
            "offset_x", "Deslocamento X", "float", 0.0,
            help=(
                "Soma este valor a todas as coordenadas X geradas. Use para posicionar o "
                "desenho na placa virgem sem mudar o zero da máquina. Típico 0."
            ),
            unit="mm", minimum=-2000, maximum=2000, decimals=3,
        ),
        ParamSpec(
            "offset_y", "Deslocamento Y", "float", 0.0,
            help="Soma este valor a todas as coordenadas Y geradas (veja 'Deslocamento X').",
            unit="mm", minimum=-2000, maximum=2000, decimals=3,
        ),
        ParamSpec(
            "mirror_x", "Espelhar em X (camada inferior)", "bool", False,
            help=(
                "Espelha horizontalmente: X' = 2 × eixo − X (aplicado antes do "
                "deslocamento). Necessário para a face inferior (bottom) de placas dupla "
                "face, que é vista por baixo. Normalmente o espelhamento é feito na CAM; "
                "ligue aqui só se a CAM não espelhou — espelhar duas vezes desfaz. Nota: "
                "espelhar inverte o sentido dos caminhos (concordante ↔ discordante)."
            ),
        ),
        ParamSpec(
            "mirror_axis_x", "Eixo do espelhamento X", "float", 0.0,
            help=(
                "Posição X da linha vertical em torno da qual se espelha. Use o centro da "
                "placa (ou a posição dos pinos de alinhamento) para que frente e verso "
                "coincidam ao virar a placa."
            ),
            unit="mm", minimum=-2000, maximum=2000, decimals=3,
        ),
        ParamSpec(
            "max_line_length", "Tamanho máximo de linha", "int", 70,
            help=(
                "Comprimento máximo de cada linha em caracteres. O GRBL 1.1 aceita no "
                "máximo ~79 caracteres por linha (acima disso: error:11). Comentários "
                "longos são truncados; linhas de comando longas demais (ex.: no G-code "
                "personalizado) geram erro na geração. 0 = sem limite."
            ),
            minimum=0, maximum=256, step=1, decimals=0, advanced=True,
        ),
        ParamSpec(
            "line_numbers", "Numerar linhas (N)", "bool", False,
            help=(
                "Prefixa cada comando com N<número>. O GRBL ignora a numeração (só a "
                "reporta no status 'Ln' se compilado com essa opção), então ela apenas "
                "aumenta o arquivo e o tráfego serial. Útil para comparar com outros "
                "programas."
            ),
            advanced=True,
        ),
        ParamSpec(
            "line_number_step", "Incremento da numeração", "int", 10,
            help="Incremento entre números de linha N quando a numeração está ligada.",
            minimum=1, maximum=1000, step=1, decimals=0, advanced=True,
        ),
        ParamSpec(
            "compact", "Remover espaços", "bool", False,
            help=(
                "Remove os espaços dos comandos (ex.: 'G1X10.5Y3' em vez de 'G1 X10.5 "
                "Y3'). O GRBL aceita e o arquivo fica ~15% menor, permitindo mais "
                "comandos no buffer serial de 128 bytes — ajuda em caminhos com muitos "
                "segmentos curtos onde a máquina 'engasga'. Fica menos legível."
            ),
            advanced=True,
        ),
        ParamSpec(
            "z_offset", "Ajuste de profundidade (offset Z)", "float", 0.0, unit="mm",
            minimum=-5.0, maximum=5.0, step=0.01, decimals=3,
            help=(
                "Soma este valor a TODOS os Z do programa. Negativo = corta mais fundo; positivo = "
                "mais raso. Útil para corrigir uma isolação que ficou rasa (ex.: -0,02 mm) sem "
                "regerar os caminhos, ou para compensar a espessura de uma folha sob a placa. "
                "Para ajustar o zero da máquina sem mexer no arquivo, use o ajuste fino de Z no "
                "painel Comandos da máquina."
            ),
        ),
        ParamSpec(
            "omit_modal", "Omitir G1 repetido", "bool", False,
            help=(
                "Em sequências de cortes, escreve só 'X.. Y..' sem repetir G1 (o modo de "
                "movimento é modal). Reduz o arquivo e o tráfego serial. Seguro no GRBL."
            ),
            advanced=True,
        ),
    ),
))


def post_settings(settings: dict[str, Any] | None = None) -> dict[str, Any]:
    """Retorna as opções do pós-processador validadas e completadas com padrões."""
    return GRBL_POST.validate(dict(settings or {}))


# --------------------------------------------------------------------------- utilidades

_COMMENT_SAN = re.compile(r"[()]")


def _ascii(text: str) -> str:
    """Remove acentos/caracteres não ASCII (o GRBL conta bytes, não caracteres)."""
    norm = unicodedata.normalize("NFKD", text)
    return norm.encode("ascii", "ignore").decode("ascii")


def _comment(text: str) -> str:
    return "(" + _COMMENT_SAN.sub("", _ascii(text)).strip() + ")"


_SPACES_OUTSIDE = re.compile(r"(\([^)]*\)|;.*$)| +")


def _strip_spaces(line: str) -> str:
    if "(" not in line and ";" not in line:
        return line.replace(" ", "")
    return _SPACES_OUTSIDE.sub(lambda m: m.group(1) or "", line)


def _is_comment(line: str) -> bool:
    return line.startswith("(") or line.startswith(";")


def _tool_key(tool: Tool) -> tuple:
    """Identidade da ferramenta para decidir trocas.

    Operações consecutivas com a mesma ferramenta física (mesmo número, diâmetro e
    formato — ex.: pontes do recorte, emitidas como PathOp separado) NÃO geram troca.
    Considerar só o número seria perigoso: o isolamento (T1 V-bit) seguido da furação
    (T1 broca) passaria sem pausa para troca.
    """
    return (int(tool.number), round(float(tool.diameter), 4), tool.shape)


@dataclass
class _Fmt:
    """Formatos de número pré-montados para um conjunto de opções."""
    cd: int
    fd: int
    sep: str

    def c(self, v: float) -> str:
        return f"{round(v, self.cd) + 0.0:.{self.cd}f}"

    def f(self, v: float) -> str:
        return f"{round(v, self.fd) + 0.0:.{self.fd}f}"


# --------------------------------------------------------------------------- emissor

class _Emitter:
    """Acumula linhas de G-code e o estado modal da máquina durante a geração."""

    def __init__(self, s: dict[str, Any]) -> None:
        self.s = s
        self.out: list[str] = []
        self.job_toolchange_z: float | None = None
        self.fmt = _Fmt(s["coord_decimals"], s["feed_decimals"], "" if s["compact"] else " ")
        self.sp = self.fmt.sep
        self.comments: bool = s["comments"]
        # estado
        self.x_s: str | None = None   # última coordenada X formatada ("X1.0000")
        self.y_s: str | None = None
        self.xy: np.ndarray | None = None
        self.z: float | None = None
        self.feed: float | None = None
        self.spindle: tuple[str, float] | None = None   # ("M3", rpm) ligado
        self.tool: tuple | None = None
        self.motion: str | None = None
        self.laser_power_on: bool = False
        cd = s["coord_decimals"]
        self._fx = f"X%.{cd}f"
        self._fy = f"Y%.{cd}f"
        self.mirror = bool(s["mirror_x"])
        self.axis = float(s["mirror_axis_x"])
        self.ox = float(s["offset_x"])
        self.oy = float(s["offset_y"])
        self.bounds = [math.inf, math.inf, -math.inf, -math.inf]

    # ------------------------------------------------------------- básicos
    def comment(self, text: str) -> None:
        if self.comments:
            self.out.append(_comment(text))

    def raw(self, text: str) -> None:
        for line in text.splitlines():
            line = line.strip()
            if line:
                self.out.append(line)

    def transform(self, pts: np.ndarray) -> np.ndarray:
        """Aplica espelhamento, deslocamento e arredondamento; acumula limites."""
        a = np.asarray(pts, dtype=np.float64).reshape(-1, 2)
        if self.mirror or self.ox or self.oy:
            a = a.copy()
            if self.mirror:
                a[:, 0] = 2.0 * self.axis - a[:, 0]
            a[:, 0] += self.ox
            a[:, 1] += self.oy
        a = np.round(a, self.fmt.cd) + 0.0
        if len(a):
            mn = a.min(axis=0)
            mx = a.max(axis=0)
            b = self.bounds
            b[0] = min(b[0], mn[0]); b[1] = min(b[1], mn[1])
            b[2] = max(b[2], mx[0]); b[3] = max(b[3], mx[1])
        return a

    def _f_word(self, feed: float) -> str:
        if self.feed is not None and abs(self.feed - feed) < 1e-9:
            return ""
        self.feed = feed
        return self.sp + "F" + self.fmt.f(feed)

    def g0_z(self, z: float, feed_z: float | None = None) -> None:
        if self.z is not None and abs(self.z - z) < 1e-9:
            return
        if self.s["rapid_z_g0"] or feed_z is None or (self.z is not None and z <= self.z):
            self.out.append("G0" + self.sp + "Z" + self.fmt.c(z))
            self.motion = "G0"
        else:
            self.out.append("G1" + self.sp + "Z" + self.fmt.c(z) + self._f_word(feed_z))
            self.motion = "G1"
        self.z = z

    def retract(self, z: float, feed_z: float) -> None:
        """Sobe para ``z`` (G0, ou G1 no avanço de mergulho se 'rapid_z_g0' desligado)."""
        if self.z is not None and abs(self.z - z) < 1e-9:
            return
        if self.s["rapid_z_g0"]:
            self.out.append("G0" + self.sp + "Z" + self.fmt.c(z))
            self.motion = "G0"
        else:
            self.out.append("G1" + self.sp + "Z" + self.fmt.c(z) + self._f_word(feed_z))
            self.motion = "G1"
        self.z = z

    def g1_z(self, z: float, feed: float) -> None:
        self.out.append("G1" + self.sp + "Z" + self.fmt.c(z) + self._f_word(feed))
        self.motion = "G1"
        self.z = z

    def _xy_words(self, x: float, y: float, force: bool = False) -> str:
        xs = self._fx % x
        ys = self._fy % y
        parts = []
        if force or xs != self.x_s:
            parts.append(xs)
        if force or ys != self.y_s:
            parts.append(ys)
        self.x_s, self.y_s = xs, ys
        return self.sp.join(parts)

    def g0_xy(self, xy: np.ndarray, suffix: str = "") -> None:
        x, y = float(xy[0]), float(xy[1])
        words = self._xy_words(x, y)
        if words or suffix:
            self.out.append("G0" + self.sp + words + suffix if words else "G0" + suffix)
            self.motion = "G0"
        self.xy = np.array([x, y])

    def g1_path(self, pts: np.ndarray, feed: float, extra_first: str = "") -> None:
        """Emite cortes G1 para ``pts`` (já transformados), a partir da posição atual.

        Caminho rápido: formata todas as coordenadas de uma vez e omite eixos que não
        mudam e segmentos nulos.
        """
        n = len(pts)
        if n == 0:
            return
        xs = pts[:, 0].tolist()
        ys = pts[:, 1].tolist()
        fx, fy = self._fx, self._fy
        sx = [fx % v for v in xs]
        sy = [fy % v for v in ys]
        sp = self.sp
        px, py = self.x_s, self.y_s
        body: list[str] = []
        append = body.append
        for a, b in zip(sx, sy):
            if a != px:
                append(a + sp + b if b != py else a)
            elif b != py:
                append(b)
            px, py = a, b
        self.x_s, self.y_s = px, py
        self.xy = pts[-1].copy()
        if not body:
            return
        g1 = "G1" + sp
        fw = self._f_word(feed)
        first = g1 + body[0] + fw + extra_first
        if self.s["omit_modal"]:
            if not (self.motion == "G1" and not fw and not extra_first):
                body[0] = first
        else:
            body[0] = first
            if len(body) > 1:
                body[1:] = [g1 + t for t in body[1:]]
        self.out.extend(body)
        self.motion = "G1"

    def dwell(self, seconds: float) -> None:
        if seconds > 0:
            self.out.append("G4" + self.sp + "P" + f"{seconds:.3f}".rstrip("0").rstrip("."))

    def spindle_on(self, direction: str, rpm: float, warmup: float) -> None:
        state = (direction, round(rpm, 3))
        if self.spindle == state:
            return
        self.out.append(direction + self.sp + "S" + self.fmt.f(rpm))
        self.spindle = state
        self.dwell(warmup)

    def spindle_off(self) -> None:
        if self.spindle is not None:
            self.out.append("M5")
            self.spindle = None
        self.laser_power_on = False


# --------------------------------------------------------------------------- operações

def _pass_depths(p: CutParams) -> list[float]:
    """Profundidades sucessivas (multiprofundidade) até ``cut_z``."""
    cz = float(p.cut_z)
    if not p.multi_depth or p.depth_per_pass <= 0 or cz >= 0:
        return [cz]
    n = max(1, math.ceil(abs(cz) / p.depth_per_pass - 1e-9))
    return [max(cz, -p.depth_per_pass * (i + 1)) for i in range(n)]


def _is_laser(s: dict[str, Any], op: PathOp) -> bool:
    return s["variant"] == "grbl_laser" or bool(op.params.laser_mode)


def _emit_toolchange(e: _Emitter, op: PathOp, laser: bool) -> None:
    s = e.s
    mode = s["toolchange_mode"]
    if laser or s["variant"] == "grbl_no_toolchange" or mode == "ignore":
        return
    t = op.tool
    e.spindle_off()
    e.comment(f"Troca de ferramenta: T{t.number} {t.name or t.shape} D{t.diameter:g}mm")
    e.g0_z(float(e.job_toolchange_z if e.job_toolchange_z is not None else s["toolchange_z"]))
    if s["toolchange_move_xy"]:
        e.g0_xy(np.array([s["toolchange_x"], s["toolchange_y"]], dtype=float))
    e.out.append(f"T{int(t.number)}")
    if mode == "m6_m0":
        e.out.append("M6")
    name = _COMMENT_SAN.sub("", _ascii(t.name or t.shape)).replace(",", " ")
    msg = f"(MSG,Troque para T{int(t.number)} {name} D{t.diameter:g}mm e retome)"
    e.out.append(msg)
    e.out.append("M0")
    e.feed = None   # após pausa o operador pode ter mudado overrides; reemite F


def _emit_mill(e: _Emitter, op: PathOp, laser: bool) -> None:
    s = e.s
    p = op.params
    tz = float(p.travel_z)
    depths = _pass_depths(p)
    thresh = float(op.tool.diameter) * float(s["avoid_retract_factor"]) if s["avoid_retract"] else -1.0
    off_mode = s["laser_off_mode"]
    sp = e.sp
    power = e.fmt.f(p.laser_power)

    if laser:
        # M4 dinâmico: laser apagado em G0 (se $32=1), potência S em G1.
        if off_mode != "m5":
            e.out.append("M4" + sp + "S" + (power if off_mode == "auto" else "0"))
            e.spindle = ("M4", p.laser_power if off_mode == "auto" else 0.0)
            e.laser_power_on = off_mode == "auto"
    else:
        if e.z is None or e.z < tz:
            e.retract(tz, p.feed_z)
        warm = max(float(s["spindle_warmup_s"]), float(p.dwell_s))
        e.spindle_on(s["spindle_direction"], p.spindle_rpm, warm)

    for raw in op.paths:
        if raw is None or len(raw) == 0:
            continue
        pts = e.transform(raw)
        if len(pts) > 1:
            keep = np.empty(len(pts), dtype=bool)
            keep[0] = True
            np.any(pts[1:] != pts[:-1], axis=1, out=keep[1:])
            pts = pts[keep]
        entry = pts[0]
        closed = len(pts) > 2 and bool(np.all(pts[0] == pts[-1]))

        if laser:
            if off_mode == "m5":
                e.spindle_off()
                e.g0_xy(entry)
                e.out.append("M4" + sp + "S" + power)
                e.spindle = ("M4", p.laser_power)
            elif off_mode == "s0":
                if e.xy is None or np.any(e.xy != entry):
                    e.g0_xy(entry, suffix=(sp + "S0") if e.laser_power_on else "")
                    e.laser_power_on = False
            else:
                e.g0_xy(entry)
            if len(pts) == 1:
                continue
            extra = ""
            if off_mode == "s0" and not e.laser_power_on:
                extra = sp + "S" + power
                e.laser_power_on = True
            e.g1_path(pts[1:], p.feed_xy, extra_first=extra)
            continue

        # ---- fresadora
        linked = False
        if (thresh > 0 and e.xy is not None and e.z is not None and e.z < tz
                and float(np.hypot(*(entry - e.xy))) < thresh):
            e.g1_path(entry[None, :], p.feed_xy)
            linked = True
        if not linked:
            if e.z is None or e.z < tz:
                e.retract(tz, p.feed_z)
            e.g0_xy(entry)
            if e.z > tz:
                e.g0_z(tz)   # vindo do Z de troca: desce em rápido até o Z de deslocamento
        if len(pts) == 1:
            e.g1_z(depths[-1], p.feed_z)
            continue
        fwd = pts[1:]
        rev = pts[-2::-1]
        for i, d in enumerate(depths):
            if e.z is None or abs(e.z - d) > 1e-9:
                e.g1_z(d, p.feed_z)
            if closed or i % 2 == 0:
                e.g1_path(fwd, p.feed_xy)
            else:
                e.g1_path(rev, p.feed_xy)
    if not laser:
        e.retract(tz, p.feed_z)


def _emit_drill(e: _Emitter, op: PathOp, laser: bool) -> None:
    s = e.s
    p = op.params
    pts = e.transform(op.points) if len(op.points) else np.zeros((0, 2))
    if not len(pts):
        return
    sp = e.sp
    if laser:
        pulse = max(float(s["laser_drill_pulse_s"]), float(p.dwell_s), float(s["drill_dwell_s"]), 0.01)
        power = e.fmt.f(p.laser_power)
        e.spindle_off()
        for xy in pts:
            e.g0_xy(xy)
            e.out.append("M3" + sp + "S" + power)
            e.dwell(pulse)
            e.out.append("M5")
        return

    tz = float(p.travel_z)
    if e.z is None or e.z < tz:
        e.retract(tz, p.feed_z)
    warm = max(float(s["spindle_warmup_s"]), float(p.dwell_s))
    e.spindle_on(s["spindle_direction"], p.spindle_rpm, warm)
    if e.z > tz:
        e.g0_xy(pts[0])
        e.g0_z(tz)   # vindo do Z de troca: desce em rápido até o Z de deslocamento
    depths = _pass_depths(p)
    bottom_dwell = float(s["drill_dwell_s"])
    tz_s = "Z" + e.fmt.c(tz)
    up = ("G0" + sp + tz_s) if s["rapid_z_g0"] else ("G1" + sp + tz_s + sp + "F" + e.fmt.f(p.feed_z))

    if len(depths) == 1 and bottom_dwell <= 0:
        # caminho rápido vetorizado: G0 XY / G1 Z / sobe
        down_first = "G1" + sp + "Z" + e.fmt.c(depths[0]) + sp + "F" + e.fmt.f(p.feed_z)
        down = "G1" + sp + "Z" + e.fmt.c(depths[0])
        if not s["rapid_z_g0"]:
            down = down_first
        fx, fy = e._fx, e._fy
        xs = pts[:, 0].tolist()
        ys = pts[:, 1].tolist()
        out = e.out
        g0 = "G0" + sp
        first = True
        for x, y in zip(xs, ys):
            out.append(g0 + (fx % x) + sp + (fy % y))
            if first:
                out.append(down_first)
                first = False
            else:
                out.append(down)
            out.append(up)
        e.x_s, e.y_s = fx % xs[-1], fy % ys[-1]
        e.xy = pts[-1].copy()
        e.feed = float(p.feed_z)
        e.motion = "G0" if s["rapid_z_g0"] else "G1"
        e.z = tz
        return

    clear = float(s["peck_clearance"])
    partial = s["peck_retract"] == "partial"
    for xy in pts:
        e.g0_xy(xy)
        prev = None
        for i, d in enumerate(depths):
            if prev is not None:
                if partial:
                    pass  # já está em prev + folga
                else:
                    e.g0_z(min(tz, prev + clear))
            e.g1_z(d, p.feed_z)
            if i < len(depths) - 1:
                if partial:
                    e.g0_z(min(tz, d + clear))
                else:
                    e.retract(tz, p.feed_z)
            prev = d
        e.dwell(bottom_dwell)
        e.retract(tz, p.feed_z)


# --------------------------------------------------------------------------- API

def _header(e: _Emitter, jobs: list[Job]) -> None:
    s = e.s
    e.comment("BrokCAM - G-code para GRBL 1.1")
    e.comment(f"Variante: {s['variant']}")
    for j in jobs:
        e.comment(f"Trabalho: {j.name} {('- ' + j.operation) if j.operation else ''}")
    if s["mirror_x"]:
        e.comment(f"Espelhado em X no eixo {s['mirror_axis_x']:g}")
    if s["offset_x"] or s["offset_y"]:
        e.comment(f"Deslocamento X{s['offset_x']:g} Y{s['offset_y']:g}")
    e.out.append(e.sp.join(["G21", "G90", "G94", "G17"]))


def generate_program(jobs: list[Job], settings: dict[str, Any] | None = None) -> str:
    """Gera um único programa G-code com todos os ``jobs`` em sequência.

    Entre operações com ferramentas diferentes é inserida uma troca de ferramenta
    conforme ``toolchange_mode``. ``settings`` usa as chaves de ``GRBL_POST``
    (valores ausentes recebem o padrão).

    Retorna o texto completo, terminado por quebra de linha.
    """
    s = post_settings(settings)
    e = _Emitter(s)
    _header(e, jobs)
    e.raw(s["start_gcode"])
    bounds_idx = len(e.out)
    any_mill = False

    for job in jobs:
        if len(jobs) > 1:
            e.comment(f"--- {job.name}")
        tcz = job.settings.get("toolchange_z") if job.settings else None
        e.job_toolchange_z = float(tcz) if tcz is not None else None
        if e.comments:
            for w in (job.settings or {}).get("warnings", []) or []:
                e.comment(f"AVISO: {w}")
        for op in job.ops:
            laser = _is_laser(s, op)
            any_mill |= not laser
            key = _tool_key(op.tool)
            if e.tool is not None and key != e.tool:
                _emit_toolchange(e, op, laser)
            elif e.tool is None and s["toolchange_first_tool"]:
                _emit_toolchange(e, op, laser)
            e.tool = key
            t = op.tool
            p = op.params
            if e.comments:
                # o T da MÁQUINA vem primeiro e rotulado; o rótulo da operação pode citar os T do
                # ARQUIVO Excellon ("arquivo T3"). Se passar de max_line_length, só o fim é cortado.
                e.comment(f"Op T{t.number} da maquina {t.shape} D{t.diameter:g}mm: {op.label or op.kind}")
                if laser:
                    e.comment(f"Laser S{p.laser_power:g} F{p.feed_xy:g}")
                else:
                    e.comment(f"Z corte {p.cut_z:g} Z desloc {p.travel_z:g} F{p.feed_xy:g} Fz{p.feed_z:g} S{p.spindle_rpm:g}")
            if op.kind == "drill":
                _emit_drill(e, op, laser)
            else:
                _emit_mill(e, op, laser)


    # ---- fim
    e.spindle_off()
    if any_mill:
        e.g0_z(float(s["end_z"]))
    if s["end_move_xy"]:
        e.g0_xy(np.array([s["end_x"], s["end_y"]], dtype=float))
    elif s["return_home"]:
        e.g0_xy(np.zeros(2))
    e.raw(s["end_gcode"])
    if s["program_end"] != "none":
        e.out.append(s["program_end"])

    if e.comments and math.isfinite(e.bounds[0]):
        b = e.bounds
        e.out.insert(bounds_idx, _comment(f"Limites X {b[0]:.3f} .. {b[2]:.3f}  Y {b[1]:.3f} .. {b[3]:.3f} mm"))
    lines = _finalize(e.out, s)
    return "\n".join(lines) + "\n"


def generate_gcode(job: Job, settings: dict[str, Any] | None = None) -> str:
    """Gera o G-code de um único ``Job`` e o armazena em ``job.gcode``."""
    text = generate_program([job], settings)
    job.gcode = text
    return text


_Z_WORD = re.compile(r"Z(-?\d+(?:\.\d*)?)")


def _offset_z(lines: list[str], dz: float, decimals: int) -> list[str]:
    """Soma ``dz`` a todos os Z fora de comentários."""
    out = []
    for ln in lines:
        if _is_comment(ln) or "Z" not in ln:
            out.append(ln)
            continue
        head, sep, tail = ln.partition("(")
        head = _Z_WORD.sub(lambda m: f"Z{float(m.group(1)) + dz:.{decimals}f}", head)
        out.append(head + sep + tail)
    return out


def _finalize(lines: list[str], s: dict[str, Any]) -> list[str]:
    """Pós-passadas: offset Z, remoção de espaços, numeração N e limite de tamanho de linha."""
    if float(s.get("z_offset", 0.0) or 0.0):
        lines = _offset_z(lines, float(s["z_offset"]), int(s["coord_decimals"]))
    if s["compact"]:
        lines = [ln if _is_comment(ln) else _strip_spaces(ln) for ln in lines]
    if s["line_numbers"]:
        step = int(s["line_number_step"])
        n = 0
        sep = "" if s["compact"] else " "
        out = []
        for ln in lines:
            if _is_comment(ln):
                out.append(ln)
            else:
                n += step
                if n > 9_999_999:
                    n = step
                out.append(f"N{n}{sep}{ln}")
        lines = out
    limit = int(s["max_line_length"])
    if limit > 0:
        for i, ln in enumerate(lines):
            if len(ln) > limit:
                if ln.startswith("(") and ln.endswith(")"):
                    lines[i] = ln[: limit - 1] + ")"
                else:
                    raise ValueError(
                        f"Linha {i + 1} do G-code tem {len(ln)} caracteres (limite {limit}): {ln[:40]}..."
                    )
    return lines


def iter_lines(text: str) -> Iterable[str]:
    """Itera as linhas não vazias de um texto G-code (conveniência para o enviador)."""
    for ln in text.splitlines():
        ln = ln.strip()
        if ln:
            yield ln


# Campos que só têm efeito em certas combinações (a interface esconde os demais).
set_conditions(GRBL_POST, {
    "end_x": (("end_move_xy", (True,)),), "end_y": (("end_move_xy", (True,)),),
    "return_home": (("end_move_xy", (False,)),),
    "toolchange_z": (("toolchange_mode", ("m0", "m6_m0")), ("variant", ("grbl_mill",))),
    "toolchange_move_xy": (("toolchange_mode", ("m0", "m6_m0")), ("variant", ("grbl_mill",))),
    "toolchange_x": (("toolchange_move_xy", (True,)), ("toolchange_mode", ("m0", "m6_m0")),
                     ("variant", ("grbl_mill",))),
    "toolchange_y": (("toolchange_move_xy", (True,)), ("toolchange_mode", ("m0", "m6_m0")),
                     ("variant", ("grbl_mill",))),
    "toolchange_first_tool": (("toolchange_mode", ("m0", "m6_m0")), ("variant", ("grbl_mill",))),
    "toolchange_mode": (("variant", ("grbl_mill",)),),
    "laser_off_mode": (("variant", ("grbl_laser",)),),
    "laser_drill_pulse_s": (("variant", ("grbl_laser",)),),
    "spindle_direction": (("variant", ("grbl_mill", "grbl_no_toolchange")),),
    "spindle_warmup_s": (("variant", ("grbl_mill", "grbl_no_toolchange")),),
    "end_z": (("variant", ("grbl_mill", "grbl_no_toolchange")),),
    "rapid_z_g0": (("variant", ("grbl_mill", "grbl_no_toolchange")),),
    "peck_retract": (("variant", ("grbl_mill", "grbl_no_toolchange")),),
    "peck_clearance": (("peck_retract", ("partial",)), ("variant", ("grbl_mill", "grbl_no_toolchange"))),
    "avoid_retract_factor": (("avoid_retract", (True,)),),
    "mirror_axis_x": (("mirror_x", (True,)),),
    "line_number_step": (("line_numbers", (True,)),),
})
