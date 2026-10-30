# BrokCAM — Arquitetura

Reimplementação moderna do fluxo do FlatCAM (Gerber/Excellon → isolamento, furação,
recorte, limpeza de cobre → G-code GRBL → envio para a máquina), com foco em
desempenho, acessibilidade e todas as opções explicadas.

Referência de engenharia reversa: `_reference/` contém os módulos do FlatCAM 8.994
decompilados (bytecode Python 3.5) e os pré-processadores originais. Essa pasta existe só na
cópia local de trabalho e **não é versionada** (está no `.gitignore`). Use-os para
entender comportamento e opções — **não copie a arquitetura** (o FlatCAM mistura Qt
com lógica de CAM em classes gigantes; aqui as camadas são separadas).

## Stack
Python 3.11 · PySide6 (Qt 6) · shapely 2 (vetorizado, GEOS) · numpy · pyserial · pytest.
Ambiente: `BrokCAM/.venv` (`.venv\Scripts\python.exe`).

## Camadas (dependências só fluem para baixo)

```
brokcam/ui/        PySide6: janela, canvas, painéis, tema, console GRBL   (único que importa Qt)
brokcam/machine/   comunicação serial GRBL (pyserial, thread própria, callbacks puros)
brokcam/gcode/     pós-processador GRBL (Job -> texto), parser de G-code p/ preview
brokcam/cam/       isolamento, furação, recorte, NCC, ordenação de caminhos
brokcam/parsers/   Gerber RS-274X/X2, Excellon
brokcam/core/      model.py (dataclasses), params.py (ParamSpec explicados), units
```

## Contratos
* `core/model.py` — `GerberLayer`, `DrillLayer`, `GeometryLayer`, `Tool`, `CutParams`,
  `PathOp`, `Job`. Unidade interna **mm**.
* `core/params.py` — toda opção é um `ParamSpec` com `help` em PT-BR. Cada operação
  declara seu `ParamGroup` com `register(...)`. A UI gera formulários a partir disso.
* Funções de CAM são puras: `func(layer, settings: dict) -> Job`, sem Qt, sem estado
  global, com `progress: Callable[[float, str], None] | None` e
  `cancel: Callable[[], bool] | None` opcionais. A UI as executa em `QThreadPool`.

## Desempenho
* shapely 2: operações vetorizadas (`shapely.buffer(array)`, `union_all`), sem loops
  Python por primitiva quando possível.
* Ordenação de caminhos: vizinho-mais-próximo com KD-grid em numpy + 2-opt limitado.
* Canvas: `QGraphicsView` com viewport OpenGL opcional, um `QPainterPath` em cache por
  camada, canetas cosméticas, BSP index, sem re-tesselar ao dar zoom/pan.
* Todo processamento pesado fora da thread da GUI.

## Idioma
Interface, ajuda e mensagens em português do Brasil.
