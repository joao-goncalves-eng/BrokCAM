# Cria atalhos do BrokCAM (Área de Trabalho e Menu Iniciar) com o ícone do aplicativo.
# Usa o executável (dist\BrokCAM\BrokCAM.exe) se ele existir; senão o Python do .venv.
# Uso: clique duas vezes em CriarAtalho.bat (na pasta do BrokCAM). Rode de novo se mover a pasta.
$ErrorActionPreference = "Stop"
$root = Split-Path -Parent $PSScriptRoot
$exe = Join-Path $root "dist\BrokCAM\BrokCAM.exe"      # executável (GerarExecutavel.bat), se existir
$pyw = Join-Path $root ".venv\Scripts\pythonw.exe"
$ico = Join-Path $root "brokcam\ui\assets\brokcam.ico"
if (Test-Path $exe) {
    $target = $exe; $args_ = ""; $workdir = Split-Path -Parent $exe
} elseif (Test-Path $pyw) {
    $target = $pyw; $args_ = "`"$(Join-Path $root 'run.py')`""; $workdir = $root
} else { throw "Não encontrei dist\BrokCAM\BrokCAM.exe nem $pyw. Gere o executável (GerarExecutavel.bat)." }
$shell = New-Object -ComObject WScript.Shell
$destinos = @(
    [Environment]::GetFolderPath("Desktop"),
    (Join-Path ([Environment]::GetFolderPath("Programs")) "")
)
foreach ($pasta in $destinos) {
    $lnk = $shell.CreateShortcut((Join-Path $pasta "BrokCAM.lnk"))
    $lnk.TargetPath = $target
    $lnk.Arguments = $args_
    $lnk.WorkingDirectory = $workdir
    $lnk.IconLocation = "$ico,0"
    $lnk.Description = "BrokCAM — CAM para placas de circuito impresso com GRBL"
    $lnk.Save()
    Write-Host "Atalho criado: $(Join-Path $pasta 'BrokCAM.lnk') -> $target"
}
