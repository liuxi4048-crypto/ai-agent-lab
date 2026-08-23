# AI Agent Lab の停止。start.ps1 が起動したサーバを止める。
# Ollama は他のツール(vault-search 等)も使うので既定では止めない。
#   powershell -ExecutionPolicy Bypass -File stop.ps1            # サーバのみ停止
#   powershell -ExecutionPolicy Bypass -File stop.ps1 -WithOllama # Ollama も停止
param([switch]$WithOllama)

$Root = Split-Path -Parent $MyInvocation.MyCommand.Path
$PidPath = Join-Path $Root '.server.pid'

if (Test-Path $PidPath) {
    $serverPid = (Get-Content $PidPath -Raw).Trim()
    $proc = Get-Process -Id $serverPid -ErrorAction SilentlyContinue
    # PID は再利用されるため、python プロセスであることを確かめてから止める
    if ($proc -and $proc.ProcessName -like 'python*') {
        Stop-Process -Id $serverPid -Force
        Write-Host "サーバ(PID $serverPid)を停止しました"
    } else {
        Write-Host 'サーバは既に停止しています'
    }
    Remove-Item $PidPath -ErrorAction SilentlyContinue
} else {
    Write-Host '起動記録(.server.pid)がありません。start.ps1 以外で起動した可能性があります'
}

if ($WithOllama) {
    Get-Process ollama -ErrorAction SilentlyContinue | Stop-Process -Force
    Write-Host 'Ollama を停止しました'
}
