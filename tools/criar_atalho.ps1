# Cria atalhos do BrokCAM (Área de Trabalho e Menu Iniciar) com o ícone do aplicativo.
# Uso: clique duas vezes em CriarAtalho.bat (na pasta do BrokCAM). Rode de novo se mover a pasta.
$ErrorActionPreference = "Stop"
$root = Split-Path -Parent $PSScriptRoot
$pyw = Join-Path $root ".venv\Scripts\pythonw.exe"
$ico = Join-Path $root "brokcam\ui\assets\brokcam.ico"
if (-not (Test-Path $pyw)) { throw "Não encontrei $pyw. Crie o ambiente: py -3.11 -m venv .venv" }
$shell = New-Object -ComObject WScript.Shell
$destinos = @(
    [Environment]::GetFolderPath("Desktop"),
    (Join-Path ([Environment]::GetFolderPath("Programs")) "")
)
foreach ($pasta in $destinos) {
    $lnk = $shell.CreateShortcut((Join-Path $pasta "BrokCAM.lnk"))
    $lnk.TargetPath = $pyw
    $lnk.Arguments = "`"$(Join-Path $root 'run.py')`""
    $lnk.WorkingDirectory = $root
    $lnk.IconLocation = "$ico,0"
    $lnk.Description = "BrokCAM — CAM para placas de circuito impresso com GRBL"
    $lnk.Save()
    Write-Host "Atalho criado: $(Join-Path $pasta 'BrokCAM.lnk')"
}
