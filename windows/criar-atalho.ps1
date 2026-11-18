# Cria o atalho "Drone Manager.lnk" na Área de Trabalho do Windows com o ícone oficial do app
$scriptDir = $PSScriptRoot
$launcher = Join-Path $scriptDir 'DroneManager.ps1'
$iconSrc = Join-Path $scriptDir 'drone.ico'

# Copia o ícone para uma pasta local do Windows (Explorer muitas vezes não carrega .ico direto de \\wsl.localhost\)
$localDir = Join-Path $env:LOCALAPPDATA 'DroneManager'
if (-not (Test-Path $localDir)) {
    New-Item -ItemType Directory -Path $localDir -Force | Out-Null
}
$iconLocal = Join-Path $localDir 'drone.ico'
if (Test-Path $iconSrc) {
    Copy-Item -Path $iconSrc -Destination $iconLocal -Force
}

$desktop = [Environment]::GetFolderPath('Desktop')
$shortcutPath = Join-Path $desktop 'Drone Manager.lnk'

$shell = New-Object -ComObject WScript.Shell
$shortcut = $shell.CreateShortcut($shortcutPath)
$shortcut.TargetPath = 'powershell.exe'
$shortcut.Arguments = "-NoProfile -ExecutionPolicy Bypass -WindowStyle Hidden -File `"$launcher`""
$shortcut.WorkingDirectory = $scriptDir
if (Test-Path $iconLocal) {
    $shortcut.IconLocation = "$iconLocal,0"
}
$shortcut.Description = 'Iniciar e abrir o Drone Manager'
$shortcut.Save()

# Atualiza o cache de ícones do Windows Explorer
Start-Process -FilePath 'ie4uinit.exe' -ArgumentList '-show' -WindowStyle Hidden -ErrorAction SilentlyContinue

Write-Host "Atalho atualizado com ícone em: $shortcutPath"
Write-Host "Ícone salvo localmente em: $iconLocal"
