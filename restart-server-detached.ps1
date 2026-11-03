# ai-agent-lab サーバーを SSH セッションから完全に切り離して再起動する。
# start.ps1 -Restart で起動したサーバーがセッション終了に巻き込まれて
# 落ちる事象 (2026-10-28) への対策として、WMI の Win32_Process.Create
# で起動し、.server.pid には実際の pythonw の PID を記録する。
$ProgressPreference = 'SilentlyContinue'
Set-Location 'C:\project\ai-agent-lab'

Get-CimInstance Win32_Process |
  Where-Object { $_.CommandLine -like '*server.py*' -and $_.Name -like 'python*' } |
  ForEach-Object { Stop-Process -Id $_.ProcessId -Force -ErrorAction SilentlyContinue }
Start-Sleep -Seconds 3

$cmd = 'cmd /c cd /d C:\project\ai-agent-lab && "C:\Users\PC_User\AppData\Local\Programs\Python\Python312\pythonw.exe" server.py >> server.log 2>> server.err.log'
$r = Invoke-CimMethod -ClassName Win32_Process -MethodName Create -Arguments @{ CommandLine = $cmd }
if ($r.ReturnValue -ne 0) { throw "Win32_Process.Create failed: $($r.ReturnValue)" }

$ok = $false
for ($i = 0; $i -lt 30; $i++) {
  Start-Sleep -Seconds 4
  try {
    $h = Invoke-RestMethod -Uri 'http://127.0.0.1:8765/health' -TimeoutSec 6
    $ok = $true
    break
  } catch { }
}
if (-not $ok) { throw 'health check did not pass within ~120s' }

$sv = Get-CimInstance Win32_Process |
  Where-Object { $_.CommandLine -like '*server.py*' -and $_.Name -like 'python*' } |
  Select-Object -First 1
if ($sv) { $sv.ProcessId | Out-File '.\.server.pid' -Encoding ascii }
"restarted: server pid=$($sv.ProcessId) health OK"
