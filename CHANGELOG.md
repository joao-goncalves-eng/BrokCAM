# Histórico de versões

Todas as mudanças relevantes do BrokCAM ficam registradas aqui.
O formato segue o [Keep a Changelog](https://keepachangelog.com/pt-BR/1.1.0/) e o projeto
usa [Versionamento Semântico](https://semver.org/lang/pt-BR/) — veja
[docs/VERSIONAMENTO.md](docs/VERSIONAMENTO.md).

## [Não lançado]

_Nada ainda._

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
