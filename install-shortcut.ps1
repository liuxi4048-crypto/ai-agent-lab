# デスクトップに「AI Agent Lab」ショートカットを作る(ワンボタン起動の入口)。
# 実体は start.vbs → start.ps1(Ollama → サーバ → ブラウザ)。何度実行してもよい。
#   powershell -ExecutionPolicy Bypass -File install-shortcut.ps1

$Root = Split-Path -Parent $MyInvocation.MyCommand.Path
$LinkPath = Join-Path ([Environment]::GetFolderPath('Desktop')) 'AI Agent Lab.lnk'

$shell = New-Object -ComObject WScript.Shell
$sc = $shell.CreateShortcut($LinkPath)
$sc.TargetPath = Join-Path $Root 'start.vbs'
$sc.WorkingDirectory = $Root
$sc.IconLocation = (Join-Path $Root 'app.ico') + ',0'
$sc.Description = 'Ollama とサーバを起動してブラウザでダッシュボードを開く'
$sc.Save()

Write-Host "作成しました: $LinkPath"
