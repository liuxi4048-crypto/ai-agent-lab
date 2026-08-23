# AI Agent Lab ワンボタン起動: Ollama → サーバ(FastAPI) → ブラウザ。
#
# 3つとも「起動していなければ起動する」冪等な作りにしてある(二重起動しない)。
# 通常はデスクトップの「AI Agent Lab」ショートカット(start.vbs)から呼ばれるが、
# 単体でも実行できる:
#   powershell -ExecutionPolicy Bypass -File start.ps1            # 通常起動
#   powershell -ExecutionPolicy Bypass -File start.ps1 -NoBrowser # ブラウザを開かない
#   powershell -ExecutionPolicy Bypass -File start.ps1 -Restart   # サーバだけ再起動(コード変更後)
#
# ログ: launcher.log(直近の起動の記録。失敗したらまずここを見る)
[CmdletBinding()]
param(
    [switch]$NoBrowser,   # ブラウザを開かない(サーバだけ立てたいとき)
    [switch]$Restart      # 既存サーバを止めてから起動し直す(コードを変更したとき)
)

$ErrorActionPreference = 'Stop'
$Root = Split-Path -Parent $MyInvocation.MyCommand.Path
$Port = 8765
$Url = "http://127.0.0.1:$Port"
$LogPath = Join-Path $Root 'launcher.log'
$PidPath = Join-Path $Root '.server.pid'

function Write-Log([string]$Message) {
    $line = "{0} {1}" -f (Get-Date -Format 'HH:mm:ss'), $Message
    Write-Host $line
    Add-Content -Path $LogPath -Value $line -Encoding utf8
}

function Test-Port([int]$TargetPort) {
    $client = New-Object Net.Sockets.TcpClient
    try {
        # ローカル接続なので 300ms 待てば十分(未起動なら即 RST が返る)
        if ($client.ConnectAsync('127.0.0.1', $TargetPort).Wait(300)) { return $client.Connected }
        return $false
    } catch { return $false } finally { $client.Dispose() }
}

function Wait-Port([int]$TargetPort, [int]$TimeoutSec, [string]$What) {
    $deadline = (Get-Date).AddSeconds($TimeoutSec)
    while ((Get-Date) -lt $deadline) {
        if (Test-Port $TargetPort) { return $true }
        Start-Sleep -Milliseconds 400
    }
    Write-Log "[NG] $What が ${TimeoutSec}秒 以内に応答しませんでした"
    return $false
}

# --- 起動記録をリセット(前回分と混ざると原因を追いにくい) ---
Set-Content -Path $LogPath -Value ("=== {0} 起動 ===" -f (Get-Date -Format 'yyyy-MM-dd HH:mm:ss')) -Encoding utf8

# --- 1. Ollama -------------------------------------------------------------
if (Test-Port 11434) {
    Write-Log '[skip] Ollama は起動済み'
} else {
    $ollama = Join-Path $env:LOCALAPPDATA 'Programs\Ollama\ollama.exe'
    if (-not (Test-Path $ollama)) {
        $cmd = Get-Command ollama -ErrorAction SilentlyContinue
        if ($cmd) { $ollama = $cmd.Source }
    }
    if (Test-Path $ollama) {
        # setx で設定した OLLAMA_* はこのプロセスに載っていないことがある。
        # ユーザー環境変数から読み直して子プロセスへ確実に引き継ぐ
        # (KV_CACHE_TYPE / NUM_PARALLEL / MAX_LOADED_MODELS / FLASH_ATTENTION)
        foreach ($entry in [Environment]::GetEnvironmentVariables('User').GetEnumerator()) {
            if ($entry.Key -like 'OLLAMA_*') {
                Set-Item -Path ("Env:" + $entry.Key) -Value $entry.Value
                Write-Log ("[env] {0}={1}" -f $entry.Key, $entry.Value)
            }
        }
        Write-Log "[run] Ollama を起動: $ollama serve"
        Start-Process -FilePath $ollama -ArgumentList 'serve' -WindowStyle Hidden
        if (-not (Wait-Port 11434 30 'Ollama')) { }
    } else {
        Write-Log '[NG] ollama.exe が見つかりません(UIには「Ollama未接続」と出ます)'
    }
}

# --- 2. サーバ(FastAPI) ----------------------------------------------------
if ($Restart -and (Test-Path $PidPath)) {
    $oldPid = (Get-Content $PidPath -Raw).Trim()
    $proc = Get-Process -Id $oldPid -ErrorAction SilentlyContinue
    # PID は再利用されるので、自分が起動した python かどうかを名前で確かめてから止める
    if ($proc -and $proc.ProcessName -like 'python*') {
        Write-Log "[run] 既存サーバ(PID $oldPid)を停止"
        Stop-Process -Id $oldPid -Force
        Start-Sleep -Milliseconds 800
    }
    Remove-Item $PidPath -ErrorAction SilentlyContinue
}

if (Test-Port $Port) {
    Write-Log "[skip] サーバは起動済み ($Url)"
} else {
    $python = (Get-Command pythonw -ErrorAction SilentlyContinue).Source
    if (-not $python) { $python = (Get-Command python -ErrorAction SilentlyContinue).Source }
    if (-not $python) {
        Write-Log '[NG] python が PATH にありません'
        exit 1
    }
    Write-Log "[run] サーバを起動: $python server.py"
    # pythonw ならコンソール窓が出ない。出力は server.log へ落として原因を追えるようにする
    $proc = Start-Process -FilePath $python -ArgumentList 'server.py' -WorkingDirectory $Root `
        -WindowStyle Hidden -PassThru `
        -RedirectStandardOutput (Join-Path $Root 'server.log') `
        -RedirectStandardError  (Join-Path $Root 'server.err.log')
    Set-Content -Path $PidPath -Value $proc.Id -Encoding ascii
    if (-not (Wait-Port $Port 40 'サーバ')) {
        Write-Log '--- server.err.log(末尾) ---'
        if (Test-Path (Join-Path $Root 'server.err.log')) {
            Get-Content (Join-Path $Root 'server.err.log') -Tail 20 | ForEach-Object { Write-Log $_ }
        }
        exit 1
    }
}

# --- 3. 健全性チェック(Ollama から見た状態も含む) --------------------------
try {
    $health = Invoke-RestMethod -Uri "$Url/health" -TimeoutSec 10
    Write-Log ("[ok] health: ollama={0} slots_free={1} node={2}" -f $health.ollama, $health.slots_free, $health.node_check)
    if (-not $health.ollama) {
        Write-Log '[warn] サーバは動いていますが Ollama に繋がっていません(モデル実行は失敗します)'
    }
} catch {
    Write-Log "[warn] /health の取得に失敗: $_"
}

# --- 4. ブラウザ -----------------------------------------------------------
if ($NoBrowser) {
    Write-Log "[skip] ブラウザは開きません ($Url)"
} else {
    Write-Log "[run] ブラウザを開く: $Url"
    Start-Process $Url
}
Write-Log '完了'
