# Histórico de versões

Todas as mudanças relevantes do BrokCAM ficam registradas aqui.
O formato segue o [Keep a Changelog](https://keepachangelog.com/pt-BR/1.1.0/) e o projeto
usa [Versionamento Semântico](https://semver.org/lang/pt-BR/) — veja
[docs/VERSIONAMENTO.md](docs/VERSIONAMENTO.md).

## [Não lançado]

### Adicionado
- **Furação — escolher a broca de cada diâmetro** (fluxo do FlatCAM): nova *Associação de
  diâmetros → Escolher a broca de cada diâmetro*. Na tabela *Diâmetros de furo* selecione vários
  diâmetros (Ctrl/Shift+clique) e use *Atribuir broca aos selecionados* (brocas da lista, outro
  diâmetro, o próprio diâmetro, fresar com a fresa de topo ou automático). *Gerar caminho* faz
  tudo num só G-code, com uma pausa de troca por broca. A escolha fica em `bit_map`
  (`0,79=0,8; 1,02=1; 3,2=fresa`), marcada com ✎ na coluna *Broca*; diâmetros sem escolha seguem
  a regra da lista de brocas. *Gerar os N diâmetros num G-code* gera só os selecionados.
- **Abrir G-code como caminhos editáveis** (*Arquivo → Importar G-code como caminhos…*,
  Ctrl+Shift+O; também ao abrir/arrastar `.nc`): cada arquivo vira um caminho + uma camada de
  geometria (vários arquivos = um grupo). Reconhece trocas de ferramenta, furos (inclusive em
  bicadas e ciclos G81/G83), passadas repetidas (viram multiprofundidade), arcos, polegadas e
  laser; lê Ø/formato da ferramenta dos comentários do BrokCAM/FlatCAM. Sem editar, o envio usa o
  texto original; a tabela *Ferramentas e cortes* do inspetor muda Ø, Z, avanços e rotação
  (Ctrl+Z desfaz) e o G-code passa a ser gerado pelo pós-processador. Salvo no `.bcam`.
  Opções em *Preferências → Importar G-code*. O antigo "só visualizar/enviar" continua em
  *Arquivo → Abrir G-code só para visualizar/enviar…* e no botão *Abrir…* do painel G-code.
- **Interface estilo CAD (Fusion 360 / Inventor):** temas *Grafite*, *Azul-aço* e *Meia-noite*
  além de Claro/Escuro (cinzas neutros redesenhados) e Alto contraste; **cor de destaque**
  (11 cores ou personalizada, sempre ajustada para AA); **fundo em degradê** no canvas e na tela
  inicial; **ícones coloridos**; **cantos** (suaves/arredondados/retos) e **densidade**
  (confortável/compacta). Menu *Exibir → Aparência* e botão de aparência na faixa de comandos.
- Seletor *Preparar/Máquina* no início da faixa (como o seletor de espaço do Fusion) e títulos
  dos painéis em versalete.

### Corrigido
- Caixas de seleção dos formulários apareciam com uma borda de foco em volta do campo inteiro.
- Ícones de botões principais (*Abrir arquivos…*, *Conectar*, *Iniciar*) sem a cor de texto do
  botão.

## [0.3.0] — 2026-10-30

Primeira versão publicada no GitHub.

### Adicionado
- **Furação por diâmetro:** cada diâmetro de furo ganha cor própria no canvas (furo desenhado
  no tamanho real) e uma tabela *Diâmetros de furo* no inspetor (cor · Ø · ferramenta de origem ·
  nº de furos). Clicar numa linha destaca só aqueles furos.
- *Programas gerados → Um trabalho separado por broca*: um caminho e um G-code sem pausas de
  troca por broca; botões *Gerar um G-code por broca* e *Gerar só este diâmetro*.
- *Arquivo → Salvar G-codes de vários caminhos…* (Ctrl+Shift+E): grava um `.nc` por caminho
  com nomes seguros para cartão SD/Candle (ex.: `Furacao_0-8mm_T2_18_furos.nc`).
- Repositório Git, licença MIT, este histórico e o guia de versionamento.

### Alterado
- **Fresa em V:** o Z de corte passa a ser calculado automaticamente a partir da *Largura do
  sulco*, `Z = −(largura − ponta) / (2·tan(ângulo/2))`, e fica travado no Isolamento, na
  Limpeza de cobre e na Biblioteca de ferramentas. Ao voltar para fresa de topo o Z volta a ser
  editável.

### Corrigido
- Z da fresa em V sem limite inferior (podia atravessar a placa): agora limitado entre −0,04 mm
  (atravessa os 35 µm de cobre) e −10 mm, com aviso acima de 0,3 mm.
- Presets e ferramentas salvos antes da mudança perdiam o Z; agora a largura equivalente é
  deduzida do Z antigo (erro < 1 µm).

## [0.2.0] — 2026-10-15

### Adicionado
- **Assistente de configuração da máquina:** passo a passo com teste ao vivo dos fins de curso
  (polaridade, fios trocados, ruído), direção dos eixos e homing, com cópia de segurança do `$$`
  e relatório final.
- Simulador com modelo físico das chaves (NA/NF, polaridade do GRBL AVR × Grbl_Esp32, `$5`),
  homing real com recuo `$27` e alarmes 1, 8 e 9.

### Alterado
- Interface reorganizada (faixa de comandos, árvore do projeto por categorias, inspetor de
  propriedades) com tema inspirado no GitHub — escuro, claro e alto contraste.

### Corrigido
- Homing bloqueado para sempre quando uma única chave estava encostada; agora só bloqueia quando
  todas as chaves leem acionadas em repouso (assinatura de polaridade invertida).
- Leitura do campo `Pn` do Grbl_Esp32, que some do status quando nenhum pino está ativo.
- Botão *Parar* também disponível durante jog, homing, sonda e macros.

## [0.1.0] — 2026-10

### Adicionado
- Leitura de Gerber RS-274X/X2 e Excellon, com detecção automática do papel de cada arquivo.
- Operações de CAM: isolamento, limpeza de cobre (NCC), recorte com pontes, furação e
  painelização, com otimização de percurso.
- Pós-processador GRBL 1.1 (fresadora, laser `$32=1`, sem troca de ferramenta) e preview com
  estimativa de tempo.
- Controle da máquina por USB ou WiFi (Telnet): streaming por contagem de caracteres, jog,
  overrides, sonda Z, heightmap (autolevel), macros e explicação de erros e alarmes.
- Biblioteca de ferramentas, presets, edição manual de caminhos, posicionamento e dupla face.

[Não lançado]: https://github.com/joao-goncalves-eng/BrokCAM/compare/v0.3.0...HEAD
[0.3.0]: https://github.com/joao-goncalves-eng/BrokCAM/releases/tag/v0.3.0
