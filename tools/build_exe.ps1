# Gera o executável do BrokCAM (dist\BrokCAM\BrokCAM.exe), testa e empacota num .zip.
# Uso: clique duas vezes em GerarExecutavel.bat (na pasta do BrokCAM).
$ErrorActionPreference = "Stop"
$root = Split-Path -Parent $PSScriptRoot
Set-Location $root
$py = Join-Path $root ".venv\Scripts\python.exe"
if (-not (Test-Path $py)) { throw "Não encontrei $py. Crie o ambiente: py -3.11 -m venv .venv" }
$version = (Select-String -Path "brokcam\__init__.py" -Pattern '__version__\s*=\s*"([^"]+)"').Matches[0].Groups[1].Value
Write-Host "== BrokCAM $version =="

& $py -m PyInstaller --version *> $null
if ($LASTEXITCODE -ne 0) {
    Write-Host "Instalando o PyInstaller no .venv..."
    & $py -m pip install --disable-pip-version-check -q -r requirements-build.txt
    if ($LASTEXITCODE -ne 0) { throw "Falha ao instalar o PyInstaller" }
}

Write-Host "Gerando o executável (1-3 min)..."
& $py -m PyInstaller --noconfirm --clean --log-level WARN BrokCAM.spec
if ($LASTEXITCODE -ne 0) { throw "PyInstaller falhou" }
$exe = Join-Path $root "dist\BrokCAM\BrokCAM.exe"

Write-Host "Testando o executável (abre o exemplo e fecha)..."
$env:QT_QPA_PLATFORM = "offscreen"
$p = Start-Process -FilePath $exe -ArgumentList "--smoke-test" -Wait -PassThru
Remove-Item Env:QT_QPA_PLATFORM
if ($p.ExitCode -ne 0) { throw "O executável falhou no teste (código $($p.ExitCode)). Abra dist\BrokCAM\BrokCAM.exe para ver o erro." }
Write-Host "Teste ok."

$zip = Join-Path $root "dist\BrokCAM-$version-windows-x64.zip"
if (Test-Path $zip) { Remove-Item $zip }
Compress-Archive -Path (Join-Path $root "dist\BrokCAM") -DestinationPath $zip
$mb = [math]::Round((Get-Item $zip).Length / 1MB, 1)
Write-Host ""
Write-Host "Pronto:"
Write-Host "  Executável: $exe"
Write-Host "  Pacote:     $zip ($mb MB)"
