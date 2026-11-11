# Versionamento do BrokCAM

## Esquema: `MAIOR.MENOR.CORREÇÃO` (SemVer)

| Muda o número… | Quando | Exemplo |
|---|---|---|
| **CORREÇÃO** `0.3.0 → 0.3.1` | só correções de bug, nada novo para o usuário | Z da V-bit errado em polegadas |
| **MENOR** `0.3.1 → 0.4.0` | recurso novo, projetos `.bcam` antigos continuam abrindo | importar DXF/SVG |
| **MAIOR** `0.x → 1.0.0` | quebra compatibilidade (projeto, presets, biblioteca) ou marco de estabilidade | primeira versão validada em hardware real |

Enquanto estiver em `0.x`, o software é considerado em desenvolvimento. A **1.0.0** fica reservada
para quando o fluxo completo for validado cortando placas reais na máquina.

## Onde a versão mora

Fonte única: `brokcam/__init__.py` → `__version__ = "X.Y.Z"`.
Ela aparece em *Ajuda → Sobre* e nos metadados do aplicativo. Não escreva o número em outro lugar.

## Ramos

- `main` — sempre estável: a suíte de testes passa e o app abre.
- `feat/<nome>` — recurso novo (ex.: `feat/importar-dxf`).
- `fix/<nome>` — correção (ex.: `fix/z-vbit-polegadas`).

Trabalhe no ramo, rode os testes e junte em `main` (merge ou Pull Request no GitHub).

## Mensagens de commit (Conventional Commits)

```
feat: gera um G-code por diâmetro de furo
fix: limita o Z da fresa em V a -10 mm
docs: atualiza capturas de tela do README
test: cobre homing com chave encostada
refactor: separa o parser de aberturas do Gerber
chore: atualiza dependências
```

`feat` costuma virar versão MENOR; `fix`, CORREÇÃO; `BREAKING CHANGE:` no corpo, MAIOR.

## Como lançar uma versão (checklist)

```powershell
# 1. Testes
$env:QT_QPA_PLATFORM="offscreen"
.venv\Scripts\python.exe -m pytest -q --ignore=tests/test_ui_screenshots.py

# 2. Atualize brokcam/__init__.py e mova os itens de "Não lançado" no CHANGELOG.md
#    para uma seção nova "## [X.Y.Z] — AAAA-MM-DD" (e atualize os links no fim do arquivo)

# 3. Commit e tag anotada
git add -A
git commit -m "release: vX.Y.Z"
git tag -a vX.Y.Z -m "BrokCAM vX.Y.Z"

# 4. Gere o executável (testa sozinho e cria dist\BrokCAM-X.Y.Z-windows-x64.zip)
.\GerarExecutavel.bat

# 5. Envie para o GitHub
git push origin main --tags
```

Depois, no GitHub: *Releases → Draft a new release*, escolha a tag, cole a seção do CHANGELOG e
anexe o `.zip` de `dist\`. (A pasta `dist\` não vai para o Git.)
