# Launcher inteligente do Drone Manager (Windows 11 + WSL2)
$port = 8000
$url = "http://localhost:$port"

function Test-DronePort {
    try {
        $client = New-Object System.Net.Sockets.TcpClient
        $ar = $client.BeginConnect('127.0.0.1', $port, $null, $null)
        $ok = $ar.AsyncWaitHandle.WaitOne(300, $false)
        if ($ok -and $client.Connected) {
            $client.EndConnect($ar)
            $client.Close()
            return $true
        }
        $client.Close()
    } catch {}
    return $false
}

# Se o servidor já estiver rodando, apenas abre o navegador
if (Test-DronePort) {
    Start-Process $url
    exit 0
}

# Sobe o servidor no WSL2 em janela minimizada
Start-Process -FilePath 'wsl.exe' -ArgumentList @('-e', 'bash', '-lc', '~/drone-manager/start.sh') -WindowStyle Minimized

# Aguarda a porta 8000 responder (até 30s) antes de abrir o navegador
for ($i = 0; $i -lt 60; $i++) {
    Start-Sleep -Milliseconds 500
    if (Test-DronePort) {
        Start-Process $url
        exit 0
    }
}

# Fallback: abre mesmo assim caso o WSL demore mais que 30s para iniciar
Start-Process $url
