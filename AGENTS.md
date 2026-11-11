# BrokCAM — guia para agentes

Leia isto antes de mexer no código. Resume o que o software é, como está organizado, como
rodar/testar, as convenções que o dono do projeto exige e as armadilhas já encontradas.
Para a visão de camadas, veja também [ARCHITECTURE.md](ARCHITECTURE.md); para o usuário
final, [README.md](README.md).

## 1. O que é

CAM desktop (Windows) para **fabricar PCB caseira em CNC/laser com GRBL**: lê Gerber/Excellon,
gera caminhos (isolamento, limpeza de cobre, recorte, furação, painelização), pós-processa para
**GRBL 1.1** e envia à máquina pela serial (ou WiFi). Nasceu da engenharia reversa do FlatCAM
8.994 (MIT), mas é código novo. Nome anterior: **TraceCAM** — ainda aceito em arquivos antigos.

Máquina real do dono: **MKS DLC32 (Grbl_Esp32, firmware 1.1h)**, porta **COM19** (CH340),
115200 baud. As COM3–COM9 do PC dele são Bluetooth.

## 2. Como rodar

```bash
.venv/Scripts/python.exe run.py                 # app (ou duplo clique em BrokCAM.bat)
.venv/Scripts/python.exe -m pytest -q           # suíte completa: ~350 testes, ~4–5 min
.venv/Scripts/python.exe -m pytest -q tests/test_cam.py   # um arquivo
.venv/Scripts/python.exe -m pyflakes brokcam    # lint rápido (instalado no venv)
```

- Ambiente: `.venv` com Python 3.11, PySide6 6.12, shapely 2.2, numpy 2.4, pyserial 3.5, pytest.
  O `python` do PATH é o do **Inkscape** (sem pip) — use sempre `.venv/Scripts/python.exe`.
- **Não é um repositório git.** Não há histórico: faça mudanças pequenas e rode os testes.
- Testes de interface rodam sem tela: `QT_QPA_PLATFORM=offscreen`, `QT_QPA_FONTDIR=C:\Windows\Fonts`
  (sem isso o texto vira quadrados) e `BROKCAM_CONFIG_DIR=<pasta temporária>` (não suje as
  preferências reais em `%APPDATA%\BrokCAM`). Os arquivos `tests/test_ui*.py` já fazem isso.
- Testar a máquina sem hardware: porta **`SIMULADOR`** (GRBL) ou **`SIMULADOR-DLC32`**
  (`brokcam/machine/simulator.py`). O simulador tem buffer de 128 bytes, planejador, alarmes,
  homing, sonda (`probe_surface` = função z(x,y) para placa empenada) e fins de curso
  (`limit_pins="XYZ"` ou `limit_pins_fn(mpos) -> str`). `speed=math.inf` executa instantâneo.
- Para ver a interface, gere screenshots offscreen (`widget.grab().save(...)`) e **olhe as
  imagens** — todo ajuste visual até aqui foi validado assim. Exemplos em `docs/screenshots/`.

## 3. Mapa do código (`brokcam/`)

Dependências só descem: `ui → machine/gcode/cam → parsers → core`. **Somente `ui/` importa Qt.**

| Pasta | Arquivos-chave | Papel |
|---|---|---|
| `core/` | `model.py`, `params.py`, `serialize.py` | Dataclasses compartilhadas (`GerberLayer`, `DrillLayer`, `GeometryLayer`, `Tool`, `CutParams`, `PathOp`, `Job`); sistema de opções explicadas; JSON de jobs/camadas |
| `parsers/` | `gerber.py`, `excellon.py`, `detect.py` | RS-274X/X2 e Excellon vetorizados (shapely 2); `load_file`, `guess_role` |
| `cam/` | `isolation.py`, `ncc.py`, `cutout.py`, `drilling.py`, `panelize.py`, `optimize.py`, `operations.py`, `common.py`, `tools.py`, `library.py`, `edit.py` | Operações puras `func(layer, settings, progress=None, cancel=None) -> Job`; registro `OPERATIONS`; biblioteca de ferramentas; edição manual de caminhos |
| `gcode/` | `grbl.py`, `preview.py`, `autolevel.py`, `importer.py` | Pós-processador GRBL (`GRBL_POST`, `generate_gcode/generate_program`), parser para preview/tempo, heightmap, importação de G-code → `Job` + `GeometryLayer` (`GCODE_IMPORT`) |
| `machine/` | `grbl.py`, `transport.py`, `simulator.py`, `grbl_codes.py`, `firmware.py` | Controlador GRBL em Python puro (threads + callbacks), serial/TCP, simulador, textos de erro/alarme/`$$`, catálogo Grbl_Esp32/DLC32 |
| `ui/` | ver abaixo | PySide6 |

UI (`brokcam/ui/`):

- `main_window.py` — janela, ações, faixa de comandos, espaços de trabalho, integração de tudo (maior arquivo).
- `ribbon.py` — `Ribbon`, `SegmentedControl` (Preparar/Máquina), `CollapsibleSection`.
- `canvas.py` — `CanvasView` (QGraphicsView + OpenGL opcional), itens de camada/job/G-code, simulação, modo de edição de caminhos (`edit_*`), barra de navegação.
- `project.py` — modelo do projeto (`Project`, `LayerEntry`, `JobEntry`, `Transform` com espelho→giro→deslocamento e forma matricial), grupos, `.bcam`.
- `commands.py` — todos os `QUndoCommand` (camadas, jobs, transformações, `AffineCmd`, `FlipLayersCmd`, `GroupCmd`, `EditJobOpsCmd`, `ClearJobGcodeCmd`…).
- `project_panel.py` — árvore por categorias (Gerber · Furação · Geometria · G-code), seleção múltipla.
- `operation_panel.py` — inspetor (camada, job, G-code, seleção múltipla) com formulários gerados.
- `param_form.py` — formulário automático a partir de `ParamGroup`, ajuda contextual, presets.
- `machine_panel.py` (`MachinePanel`, `GrblBridge`), `machine_tools.py` (posição/offset Z, sonda, heightmap, fins de curso, macros), `machine_config.py` (`$$` e config DLC32).
- Diálogos: `align_dialog.py`, `double_sided.py`, `tool_library.py`, `preferences.py`, `help_dialogs.py`, `wizard.py`, `welcome.py`.
- `theme.py` (bases `BASES` escuro/claro/grafite/azul-aço/meia-noite + alto contraste, cor de destaque `ACCENTS` → `derive_palette` com ajuste AA automático, `Style` (degradê, cantos, densidade, ícones coloridos `ICON_TINTS`) e QSS), `icons.py` + `icons/*.svg` (ícones próprios, recoloridos pelo tema), `backend.py` (fachada para os módulos não-UI), `workers.py` (`QThreadPool`), `settings.py` (`%APPDATA%\BrokCAM`).

`_reference/` = FlatCAM decompilado, **só consulta** (não importe, não copie a arquitetura).

## 4. Convenções obrigatórias

1. **Português do Brasil** em toda a interface, mensagens, docstrings e ajuda.
2. **Toda opção é explicada.** Opções novas são `ParamSpec` com `help` didático (o que faz,
   valor típico, risco). Use `visible_if` (ou `set_conditions(grupo, {...})`) para esconder campos
   que não têm efeito com os valores atuais. A interface gera formulários e o Manual a partir disso.
3. **Unidade interna = mm.** Conversão para polegadas só na exibição (`ui/units.py`).
4. **CAM/G-code/máquina sem Qt.** Funções puras e testáveis; a UI roda trabalho pesado em
   `TaskRunner`/threads e recebe resultados por sinal. Nunca toque widgets fora da thread da GUI
   (`GrblBridge` converte callbacks do controlador em sinais).
5. **Desempenho:** shapely 2 vetorizado e numpy; evite laços Python por vértice.
6. **Tudo que altera o projeto passa por `QUndoCommand`** (Ctrl+Z precisa funcionar).
7. **Segurança da máquina:** não grave configurações (`$n=...`, `$Nome=...`) nem mova a máquina
   real sem confirmação explícita do dono. Leitura (`$$`, `$I`, `?`, `$#`) é segura. Comandos de
   máquina na UI ficam bloqueados durante envio de programa e quando desconectado.
8. **Visual profissional e acessível:** nomes/descrições acessíveis em todo controle, contraste AA,
   teclado completo, estados com texto além de cor. A faixa de comandos deve caber com rótulos a
   partir de ~1536 px (hoje ~1527 px) — se acrescentar botões, compacte algo.
9. Mantenha compatibilidade com arquivos antigos (`.tcam`, formatos `tracecam-*`).

## 5. Fluxos e decisões que não são óbvias

- **Espaços de trabalho:** *Preparar* mostra Projeto, Propriedades, Log e G-code. *Máquina* mostra
  os painéis da máquina (`main_window.machine_docks`: Máquina, Controle e jog, Programa, Console GRBL,
  Ajustes $$, Comandos e sonda, Config. máquina); esses painéis somem
  no Preparar mesmo destacados. `_show_dock` de um painel de máquina troca para o espaço Máquina.
- **Painéis do rodapé** usam `DockTitleBar` (`ui/dock_title.py`): destacar/fechar, duplo clique.
- **Layout da Máquina** (estilo Candle): `MachinePanel(split=True)` expõe `jog_panel`, `program_panel`,
  `console_panel`, `settings_panel`, cada um num dock. Padrões em `_default_machine_layout` /
  `_default_prepare_layout` (montados após `_detach_all_docks` — `tabifyDockWidget` não tira um dock de
  um grupo de abas existente). Cada espaço guarda seu layout (`window.state` = Preparar, `window.ws_machine`,
  versão `LAYOUT_VERSION`=3; layouts antigos são ignorados). *Travar painéis* remove Movable/Floatable.
- **Segurança do envio** (`machine/safety.py`, engenharia reversa do Candle): *Parar* = `safe_stop()`
  (feed hold → espera `Hold:0` → reset, sem ALARM:3); *Reset (emergência)* continua imediato.
  *Iniciar da linha* reconstrói o estado modal e envia um preâmbulo seguro; *Pausar em M6* troca M6 por
  M5+M0; *Conferir curso* compara o programa + WCO com $130–$132 (só com $22=1). O progresso do envio
  é limitado a ~20 eventos/s e o eco de cada linha desligado (menos sinais entre threads).
- **Grupos de camadas:** arquivos abertos juntos viram o grupo “Placa N”. Mover uma camada do grupo
  move o grupo (`AffineCmd`); espelhar só uma camada agrupada é bloqueado (`_blocked_by_group`).
  Clique no canvas seleciona o grupo; Alt+clique, só a camada.
- **Transformações** são compostas em forma matricial (`Transform.compose`); caminhos já gerados
  acompanham (`Project.transform_layers`), com inversão do sentido quando há espelho (preserva
  fresamento concordante).
- **Jobs editados à mão** (`settings["edited"]`) são salvos com os caminhos no `.bcam`; os demais
  são regenerados a partir dos parâmetros ao abrir. Camadas geradas no programa (pinos de
  alinhamento) são salvas inline.
- **Furação por broca** (`cam/drilling.py`): `drill()` sempre devolve UM job; `drill_split()` devolve um job por
  broca (após o mapeamento) + um para os furos fresados; o registro usa `drill_auto` (lista quando
  `split_jobs="per_bit"` e `bit_filter==0`). Cada job separado guarda `bit_filter` (Ø da broca, ou `-1` =
  fresados) nas settings e é regenerado igual ao abrir o `.bcam`. A numeração T é a da sequência completa
  (mesmo filtrada) e o nome da ferramenta traz os T originais do arquivo (`Broca 0.8 mm (arquivo T2)`).
  `settings["bits"]` descreve cada broca (Ø, T de origem, furos, `rank`). `run_operation` e `generate_path`
  aceitam lista → `beginMacro("Furação por broca")`. Cores por diâmetro: `theme.drill_color(rank)` (rank =
  posição do Ø em ordem crescente na camada inteira), usadas por `LayerItem`/`JobItem` (um `QPainterPath`
  por grupo) e pela `DrillTable` do inspetor. A associação `group` considera a camada inteira (mesmo com
  `tool_select`), então *Gerar só este diâmetro* usa a mesma broca do programa completo; `bit_assignment()`
  devolve essa broca por T e alimenta a coluna *Broca* da tabela (só visível quando difere do furo).
  Nomes de job repetidos ganham " (2)" (`MainWindow._unique_job_name`). O comentário do G-code é
  `(Op Tn da maquina … : Furos broca Ø… - arquivo Tm)`.
- **Furação — broca escolhida à mão** (`mapping="manual"`): `bit_map` = `"0,79=0,8; 1,02=1; 3,2=fresa"`
  (`parse_bit_map`/`format_bit_map`, casamento por Ø com tolerância 0,001 mm). Diâmetros sem escolha seguem
  `available_drills` + `library_rule`; escolhidos à mão nunca são fresados automaticamente (só com `fresa`).
  Na UI: `DrillTable(multi=True)` + `OperationPanel._assign_bits` (botão *Atribuir broca*); ao sair de outro modo
  só as linhas cuja broca mudaria são fixadas (as demais continuam "automáticas", sem ✎).
- **Importar G-code** (`gcode/importer.py`): blocos por troca de ferramenta / comentário `(Op …)`; corte = avanço
  com Z ≤ `cut_z_threshold`; descida+subida sem XY = furo; passadas repetidas (inclusive em zigue-zague) =
  multiprofundidade (`depth_per_pass` = maior passo entre níveis); profundidades diferentes = ops separadas com a
  mesma ferramenta. O job sai com `settings["edited"]=True, "imported"=True` (salvo com os caminhos no `.bcam`),
  `job.gcode` = texto original (enviado sem mudanças até editar; `settings["regenerated"]` após o pós) e
  `source_id` = a `GeometryLayer` criada (linhas + círculos dos furos; oculta se `make_geometry=False`).
  `open_files` com `.nc` → `import_gcode_files`; `view_gcode_dialog` mantém o "só visualizar/enviar".
  Inspetor: tabela *Ferramentas e cortes* (`_add_imported_params`, uma coluna por op) → `editOpsRequested` →
  `EditJobOpsCmd`. `open_project` aceita job com `ops` e `source=-1`.
- **Aparência:** `MainWindow._build_appearance_menu` (Exibir › Aparência e botão da faixa) grava `theme`, `accent`,
  `accent_custom`, `canvas_style`, `corners`, `density`, `colored_icons` (grupo `ui`) e chama
  `theme.manager().configure(**_style_kwargs())`. Use `palette().accent_fg` (não `accent`) quando o destaque for COR
  DE TEXTO; ícone sobre botão `primary` sempre com `icon(nome, "accent_text")`. O degradê do canvas é fixo na tela
  (`drawBackground` + `scrollContentsBy`). Em QSS, subcontrole antes do estado (`QCheckBox::indicator:focus`).
- **Nomes de arquivo G-code:** sempre por `main_window.safe_gcode_filename` (ASCII, vírgula decimal → `-`).
  `save_all_gcode` (Ctrl+Shift+E) grava um `.nc` por caminho numa pasta, perguntando antes de sobrescrever.
- **Deslocar/espelhar camada sem grupo** (`Project.set_layer_transform`) também move os caminhos dela
  (mesma lógica de `transform_layers`), para ficarem iguais aos regenerados ao reabrir o `.bcam`.
- **Troca de ferramenta no G-code:** identidade = (número, diâmetro, formato). Nunca só pelo número
  (isolamento T1 V-bit → furação T1 broca precisa pausar).
- **Fresa em V = Z automático e travado:** com `tool_shape="vbit"` (sem laser) o usuário informa a
  *Largura do sulco* (`vbit_width`) e o `cut_z` é derivado:
  `-clamp((largura−ponta)/(2·tan(ângulo/2)), 0,04, 10)` (`cam/tools.py: vbit_auto_values`). O
  mecanismo é genérico: `ParamSpec.locked_if/locked_note` (campo visível, somente leitura) +
  `ParamGroup.derive` aplicado em `validate` (vale para UI, CAM, presets e `.bcam`). Ligado por
  `enable_vbit_auto_z(grupo)` em isolamento/NCC e na biblioteca (`TOOL_ENTRY`, `LibraryTool.vbit_width`).
  Dados antigos sem `vbit_width` preservam o Z salvo (a largura equivalente é deduzida dele) —
  por isso `settings.group_values` e `_apply_preset` passam os dados *crus* ao `validate`.
  No formulário, não use `NoButtons` no spinbox travado (com o QSS a área de texto encolhe a 1 px).
- **Heightmap:** alturas em coordenadas de trabalho, aplicadas relativas ao zero XY; leia o desvio
  de trabalho com `work_offset(fresh=True)` (o WCO do status pode estar desatualizado após G10).
- **Homing:** `GrblController.home()` faz pré-teste (`homing_preflight`) e recusa se algum fim de
  curso aparece acionado em repouso. `limit_guard` para a máquina se uma chave antes solta for
  acionada durante Run/Jog (reforço; a proteção real é `$21` na placa).
- **Serial:** abrir com DTR/RTS desligados (senão o ESP32 da DLC32 fica preso em reset); “Acesso
  negado” = porta usada por outro programa (o dono também usa o **Candle**).

## 6. Armadilhas do ambiente (já custaram tempo)

- O caminho tem acento: `C:\Users\Usuário\...`. No Bash use `"/c/Users/Usuário/..."` entre aspas;
  o scratchpad aparece como `C:\Users\USURIO~2\...`.
- **Heredoc no Bash com aspas tipográficas/acentos às vezes quebra** (“unexpected EOF”). Para patches
  grandes, escreva um script `.py` com a ferramenta de escrita e rode com `.venv/Scripts/python.exe -I`.
  Cuidado com `\n` dentro de strings de patch (pode virar quebra de linha real no código).
- No console do Windows, `print` de acentos pode falhar: use `PYTHONIOENCODING=utf-8`.
- Não rode o script de captura/testes da UI de dentro de uma pasta que contenha `shapely/` ou `lib/`
  do FlatCAM antigo (`..\`): o Python importaria o módulo errado. Rode a partir de `BrokCAM/`.
- Um `pythonw` (app aberto) segura a COM19; feche o app antes de testar a serial por script.

## 7. Estado atual e pendências conhecidas

Feito e testado: parsers, CAM completo, pós-processador GRBL (+ offset Z), preview/simulação,
biblioteca de ferramentas e presets, edição manual de caminhos, posicionar/girar/dupla face,
grupos, árvore por categorias, G-code como item removível, controlador GRBL (streaming por
contagem de caracteres, overrides, jog, sonda, heightmap, macros, fins de curso), config da DLC32,
conexão TCP, faixa de comandos e espaços de trabalho.

Pendências / ideias (confirme prioridade com o dono):

- **Máquina do dono:** a placa está com `$5=0` e as chaves leem acionadas em repouso (`Pn:XYZ`) —
  o dono foi orientado a gravar `$5=1` pela aba *Fins de curso e homing*. Não grave por conta própria.
- Nada foi validado em hardware real além de conexão/leitura (`$I`, `$$`, status). Heightmap,
  sonda e envio de programa só foram testados no simulador.
- Importar **DXF/SVG/PDF** (o FlatCAM tinha; `_reference/appParsers/` ajuda).
- Furos fresados são círculos em várias profundidades, não hélice (`PathOp.paths` é 2D).
- Instalador/`.exe` (PyInstaller) para rodar sem o `.venv`.
- O log em arquivo não registrou uma sessão do dono (08:22 de 15/10); não reproduzido — investigar
  se voltar a acontecer.
- Desfazer não cobre mudança de parâmetros de formulário; G-code não é salvo no `.bcam`.
- Existe uma pasta vazia `C:\USURIO~2` criada por engano numa sessão anterior (o dono pode apagar).

## 8. Como entregar uma tarefa

1. Leia os arquivos envolvidos (eles têm docstrings em PT-BR explicando decisões).
2. Implemente seguindo a seção 4; acrescente testes em `tests/` (use o simulador para máquina).
3. Rode `pyflakes` e a suíte completa; para mudanças visuais, gere e **inspecione** screenshots.
4. Abra o app de verdade por alguns segundos (`run.py`) para garantir que inicia.
5. Atualize README/atalhos (`ui/help_dialogs.py`) e este arquivo se mudar algo estrutural.
6. Registre a mudança em `CHANGELOG.md` (seção *Não lançado*). O projeto é versionado com Git
   (repositório `joao-goncalves-eng/BrokCAM`, ramo `main`); versão e regras de release em
   `docs/VERSIONAMENTO.md`. Não crie commits, tags nem faça push sem o dono pedir.
