# Cria o atalho "Drone Manager.lnk" na Área de Trabalho do Windows com o ícone oficial do app
$scriptDir = $PSScriptRoot
$repoDir = (Resolve-Path (Join-Path $scriptDir '..')).Path
$launcherSrc = Join-Path $scriptDir 'DroneManager.ps1'
$iconSrc = Join-Path $scriptDir 'drone.ico'

# Descobre o caminho Linux do repositório no WSL (funciona mesmo fora de ~/drone-manager)
$repoWsl = '~/drone-manager'
if ($repoDir -match '^\\\\wsl(?:\.localhost|\$)\\[^\\]+(\\.*)$') {
    $repoWsl = ($Matches[1] -replace '\\', '/')
} else {
    $detected = (wsl.exe wslpath -a ($repoDir -replace '\\', '/')) 2>$null
    if ($detected) { $repoWsl = $detected.Trim() }
}

# Copia launcher e ícone para pasta local do Windows (evita travamentos com caminhos \\wsl.localhost\)
$localDir = Join-Path $env:LOCALAPPDATA 'DroneManager'
if (-not (Test-Path $localDir)) {
    New-Item -ItemType Directory -Path $localDir -Force | Out-Null
}
$launcherLocal = Join-Path $localDir 'DroneManager.ps1'
$iconLocal = Join-Path $localDir 'drone.ico'
Copy-Item -Path $launcherSrc -Destination $launcherLocal -Force
if (Test-Path $iconSrc) {
    Copy-Item -Path $iconSrc -Destination $iconLocal -Force
}

$desktop = [Environment]::GetFolderPath('Desktop')
$shortcutPath = Join-Path $desktop 'Drone Manager.lnk'

$shell = New-Object -ComObject WScript.Shell
$shortcut = $shell.CreateShortcut($shortcutPath)
$shortcut.TargetPath = 'conhost.exe'
$shortcut.Arguments = "powershell.exe -NoProfile -ExecutionPolicy Bypass -WindowStyle Hidden -File `"$launcherLocal`" -RepoPath `"$repoWsl`""
$shortcut.WorkingDirectory = $localDir
if (Test-Path $iconLocal) {
    $shortcut.IconLocation = "$iconLocal,0"
}
$shortcut.Description = 'Iniciar e abrir o Drone Manager'
$shortcut.Save()

Start-Process -FilePath 'ie4uinit.exe' -ArgumentList '-show' -WindowStyle Hidden -ErrorAction SilentlyContinue

Write-Host "Atalho atualizado em: $shortcutPath"
Write-Host "Repositório WSL configurado: $repoWsl"
