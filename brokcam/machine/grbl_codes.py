"""Tabelas do GRBL 1.1: erros, alarmes, configurações ($) e estados, em PT-BR.

Usadas pelo controlador para traduzir respostas (``error:N``, ``ALARM:N``) e pela
interface para explicar as configurações da máquina e o que fazer em cada estado.
"""
from __future__ import annotations

from typing import NamedTuple

__all__ = ["ERRORS", "ALARMS", "SETTINGS", "STATES", "GrblSetting", "StateInfo",
           "error_text", "alarm_text", "state_info"]


ERRORS: dict[int, str] = {
    1: "Esperava uma letra de comando: uma palavra G-code é letra + número e a letra não foi encontrada.",
    2: "Formato de número inválido: valor ausente ou mal formatado (ex.: 'X1..2').",
    3: "Comando '$' não reconhecido ou não suportado.",
    4: "Valor negativo recebido onde se esperava um valor positivo.",
    5: "Homing desabilitado: o ciclo $H exige $22=1.",
    6: "O pulso mínimo de passo ($0) deve ser maior que 3 µs.",
    7: "Falha de leitura da EEPROM: as configurações foram restauradas para o padrão. Confira $$.",
    8: "Comando '$' só pode ser usado com a máquina parada (Idle). Aguarde terminar o movimento.",
    9: "G-code bloqueado durante alarme ou jog. Desbloqueie ($X) ou faça homing ($H) primeiro.",
    10: "Limites de software ($20) exigem homing habilitado ($22=1).",
    11: "Linha longa demais: excedeu o máximo de caracteres por linha (~80). A linha foi descartada.",
    12: "O valor configurado excede a taxa máxima de passos suportada (~30 kHz).",
    13: "Porta de segurança aberta: o estado de porta foi iniciado.",
    14: "Informação de build ou linha de inicialização excedeu o tamanho permitido na EEPROM.",
    15: "O destino do jog ultrapassa o curso da máquina (limites de software). Jog ignorado.",
    16: "Comando de jog inválido: falta '=' ou contém G-code proibido (use $J=G91 X.. F..).",
    17: "Modo laser ($32=1) exige saída PWM no spindle.",
    20: "Comando G-code não suportado ou inválido (ex.: M6, G83, G41 não existem no GRBL).",
    21: "Mais de um comando do mesmo grupo modal na mesma linha (ex.: G0 G1).",
    22: "Avanço indefinido: G1/G2/G3 precisa de um F definido antes.",
    23: "O comando exige um valor inteiro.",
    24: "Dois comandos na mesma linha que usam palavras de eixo (X/Y/Z).",
    25: "Palavra G-code repetida na mesma linha.",
    26: "Nenhuma palavra de eixo encontrada em um comando que precisa delas.",
    27: "Número de linha N inválido (deve ser entre 1 e 9.999.999).",
    28: "Falta um valor obrigatório para o comando (ex.: P em G4 ou G10).",
    29: "Sistemas de coordenadas G59.1, G59.2 e G59.3 não são suportados.",
    30: "G53 só é permitido junto com G0 ou G1.",
    31: "Há palavras de eixo na linha mas nenhum comando as usa.",
    32: "Arcos G2/G3 precisam de pelo menos uma palavra de eixo do plano ativo.",
    33: "Destino do movimento inválido (ex.: arco impossível ou sonda já no destino).",
    34: "Raio do arco inválido: os pontos não fecham com o raio R informado.",
    35: "Arcos G2/G3 precisam de pelo menos um deslocamento do centro (I/J/K) do plano ativo.",
    36: "Palavras de valor não usadas na linha (ex.: P sem comando que o use).",
    37: "G43.1 (compensação dinâmica de comprimento) não atribuída ao eixo configurado.",
    38: "Número de ferramenta maior que o máximo suportado (255).",
}

ALARMS: dict[int, str] = {
    1: "Limite físico (fim de curso) acionado. A posição provavelmente foi perdida — refaça o homing.",
    2: "Limite de software: o destino do movimento está fora do curso da máquina. Nada se moveu; confira o zero e o tamanho do trabalho.",
    3: "Reset durante movimento: a posição pode ter sido perdida. Refaça o homing ou o zero.",
    4: "Falha da sonda: a sonda já estava acionada antes de começar (fio encostado/em curto?).",
    5: "Falha da sonda: a sonda não tocou na placa dentro da distância programada.",
    6: "Falha de homing: o ciclo foi interrompido por reset.",
    7: "Falha de homing: a porta de segurança foi aberta durante o homing.",
    8: "Falha de homing: o recuo não liberou a chave de fim de curso. Aumente $27 ou verifique a fiação.",
    9: "Falha de homing: a chave de fim de curso não foi encontrada dentro do curso. Verifique chaves, fiação e $130–$132.",
    10: "Falha de homing: em eixo duplo (dois motores), a segunda chave não foi encontrada.",
}


class GrblSetting(NamedTuple):
    """Descrição de uma configuração ``$n`` do GRBL 1.1."""
    name: str
    unit: str
    help: str


SETTINGS: dict[int, GrblSetting] = {
    0: GrblSetting("Pulso de passo", "µs",
                   "Duração do pulso STEP enviado aos drivers. Típico 10 µs. Drivers lentos podem precisar mais; menos de 3 µs é rejeitado."),
    1: GrblSetting("Atraso para desligar motores", "ms",
                   "Tempo que os motores ficam energizados depois de parar. 25 ms economiza energia; 255 mantém sempre travados (recomendado em CNC, evita perder posição)."),
    2: GrblSetting("Inverter pulso de passo", "máscara",
                   "Máscara de bits (X=1, Y=2, Z=4) que inverte o sinal STEP. Normalmente 0; só mude se o driver exigir pulso invertido."),
    3: GrblSetting("Inverter direção", "máscara",
                   "Máscara de bits (X=1, Y=2, Z=4) que inverte o sentido de cada eixo. Use quando um eixo anda ao contrário (ex.: 4 = inverte Z)."),
    4: GrblSetting("Inverter ENABLE", "bool",
                   "Inverte o pino de habilitação dos drivers. 0 na maioria das placas; mude se os motores ficarem soltos quando deveriam travar."),
    5: GrblSetting("Inverter fins de curso", "bool",
                   "0 = chaves NA com pull-up (padrão); 1 = chaves NF. Se aparecer 'Pn:XYZ' com nada acionado, inverta."),
    6: GrblSetting("Inverter sonda", "bool",
                   "0 = sonda normalmente aberta (padrão, placa de toque); 1 = inverte. Se 'Pn:P' aparece sem tocar, inverta."),
    10: GrblSetting("Máscara do relatório de status", "máscara",
                    "Bit 0 (1): reporta MPos (máquina) em vez de WPos (trabalho). Bit 1 (2): inclui buffer (Bf:). Típico 1 ou 3. O BrokCAM calcula a outra posição usando WCO."),
    11: GrblSetting("Desvio de junção", "mm",
                    "Quanto o planejador pode 'arredondar' cantos para manter velocidade. Típico 0,010 mm. Maior = mais rápido nos cantos, menos preciso."),
    12: GrblSetting("Tolerância de arco", "mm",
                    "Erro máximo ao dividir arcos G2/G3 em retas. Típico 0,002 mm."),
    13: GrblSetting("Relatório em polegadas", "bool",
                    "0 = mm (padrão); 1 = polegadas nos relatórios de posição."),
    20: GrblSetting("Limites de software", "bool",
                    "1 = recusa movimentos fora do curso ($130–$132) com ALARM:2. Exige homing ($22=1). Recomendado se a máquina tem fins de curso."),
    21: GrblSetting("Limites físicos", "bool",
                    "1 = parada imediata (ALARM:1) ao acionar um fim de curso. Recomendado com chaves bem instaladas; ruído elétrico pode causar alarmes falsos."),
    22: GrblSetting("Homing habilitado", "bool",
                    "1 = habilita $H. Com ele ligado, o GRBL inicia em alarme pedindo homing. Precisa de chaves de fim de curso."),
    23: GrblSetting("Direção do homing", "máscara",
                    "Máscara de bits (X=1, Y=2, Z=4) que inverte o sentido de busca das chaves no homing. Padrão: busca no sentido positivo."),
    24: GrblSetting("Avanço fino do homing", "mm/min",
                    "Velocidade lenta usada para localizar a chave com precisão. Típico 25–50 mm/min."),
    25: GrblSetting("Avanço de busca do homing", "mm/min",
                    "Velocidade rápida para encontrar a chave pela primeira vez. Típico 500–1000 mm/min."),
    26: GrblSetting("Debounce do homing", "ms",
                    "Tempo de espera para a chave estabilizar. Típico 25–250 ms."),
    27: GrblSetting("Recuo do homing", "mm",
                    "Quanto o eixo se afasta da chave após encontrá-la. Típico 1–5 mm; pouco = ALARM:8."),
    30: GrblSetting("Rotação/potência máxima", "RPM",
                    "Valor S que corresponde a 100% de PWM. Em laser normalmente 1000; em spindle, a rotação máxima real."),
    31: GrblSetting("Rotação/potência mínima", "RPM",
                    "Valor S que corresponde ao PWM mínimo. Normalmente 0."),
    32: GrblSetting("Modo laser", "bool",
                    "1 = modo laser: não para nos M3/M4/S, desliga o laser em G0 e com M4 ajusta a potência à velocidade. Use 0 em fresadoras (o spindle precisa parar para trocar rotação)."),
    100: GrblSetting("Passos/mm X", "passos/mm",
                     "Calibração do eixo X: passos do motor × micropassos / avanço por volta. Ex.: 200×16/8 = 400 (fuso T8) ou 80 (correia GT2 20 dentes)."),
    101: GrblSetting("Passos/mm Y", "passos/mm", "Calibração do eixo Y (veja $100)."),
    102: GrblSetting("Passos/mm Z", "passos/mm", "Calibração do eixo Z (veja $100). Erros aqui afetam diretamente a profundidade de corte."),
    110: GrblSetting("Velocidade máx. X", "mm/min",
                     "Velocidade dos movimentos rápidos (G0) e limite de qualquer avanço em X. Típico 500–3000 em 3018. Alto demais = motor perde passos."),
    111: GrblSetting("Velocidade máx. Y", "mm/min", "Como $110, para o eixo Y."),
    112: GrblSetting("Velocidade máx. Z", "mm/min", "Como $110, para o eixo Z. Normalmente bem menor (300–800)."),
    120: GrblSetting("Aceleração X", "mm/s²",
                     "Aceleração do eixo X. Típico 10–200 em 3018. Alta demais = perda de passos e vibração; baixa = lento em caminhos com muitos cantos."),
    121: GrblSetting("Aceleração Y", "mm/s²", "Como $120, para o eixo Y."),
    122: GrblSetting("Aceleração Z", "mm/s²", "Como $120, para o eixo Z."),
    130: GrblSetting("Curso máximo X", "mm",
                     "Curso total do eixo X, usado pelos limites de software ($20). Ex.: 300 numa 3018."),
    131: GrblSetting("Curso máximo Y", "mm", "Curso total do eixo Y (veja $130). Ex.: 180 numa 3018."),
    132: GrblSetting("Curso máximo Z", "mm", "Curso total do eixo Z (veja $130). Ex.: 45 numa 3018."),
}


class StateInfo(NamedTuple):
    """Explicação de um estado do GRBL e a ação recomendada ao usuário."""
    title: str
    meaning: str
    action: str


STATES: dict[str, StateInfo] = {
    "Idle": StateInfo("Parada", "A máquina está parada e pronta para receber comandos.",
                      "Pode enviar programas, fazer jog, zerar ou sondar."),
    "Run": StateInfo("Executando", "A máquina está executando movimentos do programa.",
                     "Aguarde. Use Pausar (!) para segurar o movimento ou Parar (reset) em emergência."),
    "Hold:0": StateInfo("Pausada", "Movimento retido e totalmente parado (feed hold ou M0).",
                        "Use Retomar (~) para continuar. Em troca de ferramenta, troque a fresa e refaça o zero Z antes."),
    "Hold:1": StateInfo("Pausando", "Desacelerando até parar após uma pausa (feed hold).",
                        "Aguarde a parada completa (Hold:0)."),
    "Jog": StateInfo("Jog", "Movimento manual (jog) em andamento.",
                     "Solte a tecla/botão ou use Cancelar jog (0x85) para parar."),
    "Alarm": StateInfo("Alarme", "A máquina está bloqueada por um alarme ou aguardando homing. Comandos G-code são recusados.",
                       "Leia a mensagem do alarme, corrija a causa e faça Homing ($H) ou Desbloquear ($X). Após $X confira o zero, pois a posição pode estar errada."),
    "Door:0": StateInfo("Porta aberta (pronta)", "A porta de segurança foi aberta e a máquina está parada; pronta para retomar.",
                        "Feche a porta e use Retomar (~)."),
    "Door:1": StateInfo("Porta fechada (retomando)", "A porta foi fechada e a máquina está restaurando spindle/posição.",
                        "Aguarde a retomada."),
    "Door:2": StateInfo("Porta aberta (parando)", "A porta foi aberta durante o movimento; a máquina está parando e recuando.",
                        "Aguarde parar; depois feche a porta e retome."),
    "Door:3": StateInfo("Porta (restaurando)", "Retornando da porta aberta; recuando para continuar.",
                        "Aguarde."),
    "Check": StateInfo("Modo verificação", "Modo $C: o G-code é analisado e validado sem mover a máquina.",
                       "Útil para testar o arquivo. Envie $C novamente para sair (isso causa um reset)."),
    "Home": StateInfo("Homing", "Ciclo de referência (homing) em andamento.",
                      "Aguarde terminar. Em caso de colisão use Parar (reset)."),
    "Sleep": StateInfo("Dormindo", "Modo de suspensão ($SLP): motores e spindle desligados.",
                       "Faça um reset (Parar) para acordar a máquina."),
}


def error_text(code: int) -> str:
    """Mensagem PT-BR para ``error:code`` (ou genérica se desconhecido)."""
    return ERRORS.get(code, f"Erro desconhecido {code}.")


def alarm_text(code: int) -> str:
    """Mensagem PT-BR para ``ALARM:code`` (ou genérica se desconhecido)."""
    return ALARMS.get(code, f"Alarme desconhecido {code}.")


def state_info(state: str) -> StateInfo:
    """Explicação de um estado (aceita 'Hold', 'Door' sem subestado)."""
    if state in STATES:
        return STATES[state]
    base = state.split(":")[0]
    for key in (base + ":0", base):
        if key in STATES:
            return STATES[key]
    return StateInfo(state, "Estado desconhecido.", "Consulte a documentação do GRBL.")
